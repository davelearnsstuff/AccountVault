# -----------------------------------------------------------------------------
# ui/main_window.py
# The application window.
#
# What is left here after the split is the coordinator: the widget tree, the
# account form's lifecycle, the unsaved-changes guard, the dashboard toggle,
# and the handful of things that genuinely span the other three — a save has to
# write through the service, reload the list, and put the selection back.
#
# The three mixins are the rest of it, and each says at the top which slice of
# this window's state it owns:
#
#   ui/actions.py        commands, shortcuts, the vault menu, appearance
#   ui/vault_session.py  which vault, locked or not, and everything on a clock
#   ui/account_list.py   the list, the tab bar, search, sort, bulk actions
#
# They are mixins rather than collaborators, which is a real limit on what this
# buys — see the note in ui/actions.py.
# -----------------------------------------------------------------------------

from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QComboBox, QDialog, QFrame, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QMainWindow, QMessageBox, QPushButton, QScrollArea,
    QSizePolicy, QSplitter, QStackedWidget, QVBoxLayout, QWidget,
)

import preferences
from categories import attention_counts, get_config, normalize_category
from encryption import VaultLockedError
from services import accounts as accounts_service
from ui import dialogs
from ui.account_form import AccountForm
from ui.account_list import (
    AGGREGATE_TABS, ALL, SIDEBAR_MAX, SIDEBAR_MIN, SORT_OPTIONS, AccountListMixin,
)
from ui.actions import _FAILED, ActionsMixin
from ui.auth_screens import ChooserScreen, LoginScreen, SetupScreen
from ui.dashboard_view import DashboardView
from ui.detail_view import (
    DETAIL_MIN_WIDTH, BulkView, DetailView, PlaceholderView,
)
from ui.vault_session import VaultSessionMixin, _ActivityFilter
from ui.widgets import FlowLayout, IconButton, NavChip, ThemedIcon, Toast

# Nav icon sizes. The bar is a fixed 56px tall, so these do not scale with the
# font setting — a larger icon would have nowhere to grow into.
NAV_ICON_PX = 20
NAV_BRAND_PX = 22

# How long a deleted account can be brought back. Long enough to notice the
# toast and reach for it, short enough that the window has closed by the time
# the user has moved on to something else — see _delete_account.
UNDO_WINDOW_MS = 10_000


class MainWindow(VaultSessionMixin, AccountListMixin, ActionsMixin, QMainWindow):

    # -----------------------------------------------------------------
    # Window state
    # -----------------------------------------------------------------
    def __init__(self):
        super().__init__()
        self.setWindowTitle('AccountVault')
        self.setMinimumSize(900, 600)
        self._restore_geometry()

        # --- session state ---
        self.settings = preferences.get_settings()
        self.current_category = ALL
        self.accounts: list[dict] = []
        self.selected_id = None
        self.selected_category = None
        self.category_counts: dict[str, int] = {}
        self._category_tabs: dict[str, QPushButton] = {}
        # Tab keys in bar order, so Ctrl+<n> can resolve a digit to a tab.
        self._tab_order: list[str] = []
        self.total_count = 0
        self.open_count = 0
        self.closed_count = 0
        self.favorite_count = 0
        self.all_categories: set[str] = set()
        # Every key attention_counts returns, so a reader before the first
        # load sees zeros rather than a KeyError. 'lapsed' was missing, and
        # _attention_hint reads it.
        self.attention = attention_counts([])
        self.sort_key = 'name'
        self.sort_descending = False
        self.search_query = ''
        self.form: AccountForm | None = None
        self._pending_navigation = None
        self._sidebar_fitted = False
        # Whether the sidebar width on screen is one the user chose, and so
        # worth remembering. An auto-fitted width is not: it was measured from
        # whatever rows happened to be loaded, and storing it would freeze the
        # fit at that moment and stop it ever tracking the vault again.
        self._sidebar_user_set = False
        self._warned_of_lock = False
        self._copied_value = None
        # The last deleted account, still openable, for as long as the undo
        # toast is up. Ciphertext rather than decrypted fields — see
        # services.accounts.delete_account — and dropped by close_vault and
        # lock_vault, because rows encrypted under one vault's key must never
        # be offered for restore into another's.
        self._undo_delete = None
        # The day the scheduled backup was last considered, so the session tick
        # can notice the date turning over without asking once a second.
        self._backup_checked_on = None

        # Before the screens, because _build_main_screen puts the vault menu
        # together and that menu shows these very actions rather than copies.
        self._build_actions()

        # --- screens ---
        self.chooser_screen = ChooserScreen()
        self.chooser_screen.open_requested.connect(self._open_vault_dialog)
        self.chooser_screen.create_requested.connect(self._create_vault_dialog)
        self.chooser_screen.recent_activated.connect(self._open_vault_path)
        self.chooser_screen.forget_requested.connect(self._forget_vault)
        self.chooser_screen.clear_requested.connect(self._clear_recent)
        self.chooser_screen.remember_toggled.connect(self._set_remembering)
        self.chooser_screen.quit_requested.connect(self.close)

        self.setup_screen = SetupScreen()
        self.setup_screen.submitted.connect(self._handle_setup)

        self.login_screen = LoginScreen()
        self.login_screen.submitted.connect(self._handle_login)
        self.login_screen.switch_requested.connect(self._switch_vault)
        self.login_screen.quit_requested.connect(self.close)

        # Held by name rather than reached for by index. The stack used to be
        # addressed as widget(2), which was one screen away from being wrong the
        # moment a fourth was added — and adding the chooser is exactly that.
        self.main_screen = self._build_main_screen()

        self.screens = QStackedWidget()
        self.screens.addWidget(self.login_screen)
        self.screens.addWidget(self.setup_screen)
        self.screens.addWidget(self.main_screen)
        self.screens.addWidget(self.chooser_screen)
        self.setCentralWidget(self.screens)

        self.toast = Toast(self)

        # --- timers ---
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.timeout.connect(self._run_search)

        self._session_timer = QTimer(self)
        self._session_timer.timeout.connect(self._check_session)

        self._clipboard_timer = QTimer(self)
        self._clipboard_timer.setSingleShot(True)
        self._clipboard_timer.timeout.connect(self._clear_clipboard)

        self._shutdown_timer = QTimer(self)
        self._shutdown_timer.setSingleShot(True)
        self._shutdown_timer.timeout.connect(self.close)

        self.apply_appearance(self.settings['theme'], self.settings['font_size'])
        self._build_shortcuts()
        QApplication.instance().installEventFilter(_ActivityFilter(self))

    def _restore_geometry(self):
        """Puts the window back where it was last closed, falling back to a
        sensible default size the first time — and any time the stored blob is
        one Qt declines (a screen that has since been unplugged, most often)."""
        stored = preferences.get_window_geometry()
        if stored and self.restoreGeometry(stored):
            return
        self.resize(1180, 780)

    def _save_window_state(self):
        """Records the window and sidebar geometry for the next launch. Called
        from closeEvent, which is the one moment both are final."""
        preferences.save_window_geometry(bytes(self.saveGeometry()))
        # Only a width the user chose. An auto-fitted one is left unstored so
        # the fit runs again next launch and keeps tracking the vault as its
        # rows get longer, rather than being frozen at today's measurement.
        if self._sidebar_user_set:
            preferences.save_sidebar_width(self.splitter.sizes()[0])

    # -----------------------------------------------------------------
    # Layout
    # -----------------------------------------------------------------
    def _build_main_screen(self) -> QWidget:
        screen = QWidget()

        layout = QVBoxLayout(screen)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._build_nav())
        layout.addWidget(self._build_category_bar())

        self.dashboard_view = DashboardView(self.settings['theme'])
        self.dashboard_view.category_activated.connect(self._open_category_from_dashboard)
        self.dashboard_panel = self._wrap_panel(self.dashboard_view, 'Dashboard')
        self.dashboard_panel.hide()

        self.content_area = self._build_content_area()

        body = QVBoxLayout()
        body.setContentsMargins(12, 12, 12, 12)
        body.setSpacing(0)
        body.addWidget(self.dashboard_panel)
        body.addWidget(self.content_area)
        layout.addLayout(body, 1)

        return screen

    def _build_nav(self) -> QWidget:
        nav = QFrame()
        nav.setObjectName('TopNav')
        nav.setFixedHeight(56)

        # The brand mark is the same padlock as the window icon, so the taskbar
        # and the nav bar agree on what this app looks like. Repainted by
        # apply_appearance, since a pixmap cannot follow the theme on its own.
        self._brand_icon = ThemedIcon('lock', role='accent', size=NAV_BRAND_PX)

        brand_text = QLabel('AccountVault')
        brand_text.setObjectName('NavBrand')

        brand = QHBoxLayout()
        brand.setSpacing(9)
        brand.addWidget(self._brand_icon)
        brand.addWidget(brand_text)

        # Countdown chips. Both stay hidden until they have something to report,
        # so the bar looks exactly as it did until a promise is actually running.
        self.clipboard_chip = NavChip('info')
        self.clipboard_chip.clicked.connect(self._clear_clipboard_now)

        self.lock_chip = NavChip('alert')
        self.lock_chip.clicked.connect(self._extend_session)

        manage_button = IconButton(
            'gear', 'Manage vault — settings, backups, password',
            size=NAV_ICON_PX, label='Manage vault',
        )
        manage_button.setObjectName('NavIcon')
        manage_button.setFixedWidth(40)
        # A menu, not a dialog. Every one of these actions used to cost two
        # steps — open a modal, pick from seven buttons, watch it close, wait
        # for the real dialog — and the menu also gives the shortcuts somewhere
        # to advertise themselves, which a dialog full of buttons never could.
        manage_button.setMenu(self._build_manage_menu())

        lock_button = IconButton('lock', 'Lock vault  (Ctrl+L)', size=NAV_ICON_PX,
                                 label='Lock vault')
        lock_button.setObjectName('NavIcon')
        lock_button.setFixedWidth(40)
        lock_button.clicked.connect(self.lock_vault)

        quit_button = IconButton('power', 'Quit  (Ctrl+Q)', role='danger',
                                 size=NAV_ICON_PX, label='Quit AccountVault')
        quit_button.setObjectName('NavDanger')
        quit_button.setFixedWidth(40)
        quit_button.clicked.connect(self.close)

        layout = QHBoxLayout(nav)
        layout.setContentsMargins(16, 8, 16, 8)
        layout.setSpacing(10)
        layout.addLayout(brand)
        layout.addStretch(1)
        layout.addWidget(self.clipboard_chip)
        layout.addWidget(self.lock_chip)
        layout.addSpacing(4)
        layout.addWidget(manage_button)
        layout.addWidget(lock_button)
        layout.addWidget(quit_button)
        return nav

    def _build_category_bar(self) -> QWidget:
        """The view switcher: a Dashboard tab followed by one tab per category
        that has accounts. Exactly one is checked, and it is always the view on
        screen — that is the only indication of where you are, so every path
        that changes the view has to keep it honest."""
        bar = QFrame()
        bar.setObjectName('CategoryBar')
        # Height comes from how many rows the tabs wrap onto at the current width.
        policy = QSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        policy.setHeightForWidth(True)
        bar.setSizePolicy(policy)

        self.category_buttons = QButtonGroup(self)
        self.category_buttons.setExclusive(True)

        # Flow, not a plain row: with every category populated the tabs are wider
        # than the minimum window, and they need to wrap rather than clip.
        self._category_row = FlowLayout(bar, spacing=4)
        self._category_row.setContentsMargins(16, 3, 16, 3)
        bar.hide()  # Stays hidden until a vault load fills it in.
        return bar

    def _build_content_area(self) -> QWidget:
        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.addWidget(self._build_list_panel())
        self.splitter.addWidget(self._build_detail_panel())
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        # Replaced by _fit_sidebar() once there are rows to measure.
        self.splitter.setSizes([SIDEBAR_MIN, 860])
        self.splitter.setChildrenCollapsible(False)
        self.splitter.splitterMoved.connect(self._on_splitter_moved)
        return self.splitter

    def _on_splitter_moved(self, *_args):
        """A drag of the divider is the user choosing a width, which is the only
        kind worth carrying to the next launch."""
        self._sidebar_user_set = True

    def _build_list_panel(self) -> QWidget:
        panel = self.list_panel = QFrame()
        panel.setObjectName('ListPanel')
        panel.setMinimumWidth(SIDEBAR_MIN)
        panel.setMaximumWidth(SIDEBAR_MAX)

        self.list_title = QLabel('All Accounts')
        self.list_title.setObjectName('ListTitle')

        self.add_button = QPushButton('+ Add New')
        self.add_button.setObjectName('Primary')
        self.add_button.setAccessibleName('Add new account')
        self.add_button.clicked.connect(self._start_add)

        header = QHBoxLayout()
        header.addWidget(self.list_title, 1)
        header.addWidget(self.add_button)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText('Search')
        self.search_input.setClearButtonEnabled(True)
        self.search_input.setAccessibleName('Search accounts')
        self.search_input.setAccessibleDescription(
            'Matches names, usernames, websites and notes. Secrets are excluded.'
        )
        self.search_input.textChanged.connect(self._on_search_typed)

        self.sort_combo = QComboBox()
        for key, label in SORT_OPTIONS:
            self.sort_combo.addItem(label, key)
        self.sort_combo.setToolTip('Sort order')
        self.sort_combo.setAccessibleName('Sort order')
        self.sort_combo.currentIndexChanged.connect(self._on_sort_changed)

        # Each sort used to run one hardcoded direction — A–Z could not be
        # reversed, and Last Updated could only ever show the newest first.
        self.sort_direction = IconButton('arrow-down', size=14, label='Sort direction')
        self.sort_direction.setObjectName('Small')
        self.sort_direction.setFixedWidth(30)
        self.sort_direction.clicked.connect(self._toggle_sort_direction)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        toolbar.addWidget(self.search_input, 1)
        toolbar.addWidget(self.sort_combo)
        toolbar.addWidget(self.sort_direction)

        self.account_list = QListWidget()
        self.account_list.setAccessibleName('Accounts')
        # Shift/Ctrl-click to pick several, for the bulk actions in BulkView.
        # Rubber-band drag selection is deliberately not enabled: rows are tall
        # and a stray drag would silently reselect half the list.
        self.account_list.setSelectionMode(QListWidget.ExtendedSelection)
        # Rows never scroll sideways. A compact row's meta line can outrun a
        # sidebar the user has dragged narrow, or one already at SIDEBAR_MAX,
        # and the honest answer there is to let the tail clip: it is a summary,
        # the full values are one click away in the detail pane, and a
        # horizontal scrollbar under a vertical list is a control nobody goes
        # looking for.
        self.account_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.account_list.itemSelectionChanged.connect(self._on_selection_changed)

        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(10)
        layout.addLayout(header)
        layout.addLayout(toolbar)
        layout.addWidget(self.account_list, 1)
        return panel

    def _build_detail_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName('DetailPanel')
        # Keeps the splitter — auto-fit or hand-dragged — from ever shrinking
        # a pinned field value's grid column below what DetailView assumes.
        panel.setMinimumWidth(DETAIL_MIN_WIDTH)

        self.placeholder = PlaceholderView()
        self.placeholder.add_requested.connect(self._start_add)

        self.detail_view = DetailView(self.settings['theme'])
        self.detail_view.edit_requested.connect(self._start_edit)
        self.detail_view.delete_requested.connect(self._delete_account)
        self.detail_view.favorite_toggled.connect(self._toggle_favorite)
        self.detail_view.quick_update_requested.connect(self._quick_update)
        self.detail_view.touch_requested.connect(self._touch_account)
        self.detail_view.copy_requested.connect(self.copy_to_clipboard)
        self.detail_view.history_changed.connect(self._on_history_changed)
        self.detail_view.error.connect(self.show_toast)
        self.detail_view.notice.connect(self.show_toast)
        self.detail_view.locked.connect(self._handle_vault_locked)

        self._detail_scroll = self._scrolled(self.detail_view)

        self.bulk_view = BulkView()
        self.bulk_view.close_requested.connect(self._bulk_set_closed)
        self.bulk_view.category_requested.connect(self._bulk_set_category)
        self.bulk_view.clear_requested.connect(self._clear_selection)

        self.detail_stack = QStackedWidget()
        self.detail_stack.addWidget(self.placeholder)
        self.detail_stack.addWidget(self._detail_scroll)
        self.detail_stack.addWidget(self._scrolled(self.bulk_view))

        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.detail_stack)
        return panel

    def _wrap_panel(self, widget: QWidget, title: str) -> QWidget:
        """Titled panel. Leaving it is the tab bar's job, so there is no close
        button here — two ways out would be two things to keep in sync."""
        panel = QFrame()
        panel.setObjectName('DashboardPanel')

        heading = QLabel(title)
        heading.setObjectName('PanelTitle')

        header = QHBoxLayout()
        header.setContentsMargins(16, 12, 16, 8)
        header.addWidget(heading, 1)

        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(header)
        layout.addWidget(widget, 1)
        return panel

    @staticmethod
    def _scrolled(widget: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(widget)
        return scroll

    def eventFilter(self, obj, event):
        """Down-arrow out of the search box and into the list."""
        if (obj is self.search_input and event.type() == QEvent.KeyPress
                and event.key() == Qt.Key_Down):
            self.account_list.setFocus()
            if self.account_list.currentRow() < 0:
                self._select_first_row()
            return True
        return super().eventFilter(obj, event)

    # -----------------------------------------------------------------
    # Add / edit / delete
    # -----------------------------------------------------------------
    def _start_add(self):
        if not self._confirm_leaving_form(('add', None, None)):
            return
        category = None if self.current_category in AGGREGATE_TABS else self.current_category
        self._open_form(None, category)

    def _start_edit(self, account: dict):
        category = account.get('_category') or account.get('category')
        # Guarded here rather than in _edit_selected so the Ctrl+E shortcut and
        # the detail pane's Edit button are both covered by the one check.
        # Ctrl+E fires whatever is on screen, so it could land on a dirty form.
        if not self._confirm_leaving_form(('edit', account['id'], category)):
            return
        self._open_form(account, category)

    def _open_form(self, account: dict | None, category: str | None):
        self._discard_form()
        self.form = AccountForm(account, category)
        self.form.saved.connect(self._save_account)
        self.form.cancelled.connect(self._cancel_form)

        self._form_page = self._scrolled(self.form)
        self.detail_stack.addWidget(self._form_page)
        self.detail_stack.setCurrentWidget(self._form_page)

    def _discard_form(self):
        if self.form is None:
            return
        page = getattr(self, '_form_page', None)
        self.form = None
        if page is not None:
            self.detail_stack.removeWidget(page)
            page.deleteLater()
            self._form_page = None

    def _cancel_form(self):
        account_id, category = self.selected_id, self.selected_category
        self._discard_form()
        if account_id is not None:
            self._select_account(account_id, category)
        else:
            self._show_placeholder()

    def _save_account(self, values: dict):
        form = self.form
        account_id = values.pop('id', None)
        try:
            if account_id:
                saved = accounts_service.update_account(account_id, values)
            else:
                saved = accounts_service.create_account(values)
        except VaultLockedError:
            self._handle_vault_locked()
            return
        except Exception as exc:
            if form is not None:
                form.show_error(str(exc))
            return

        if form is not None:
            form.clear_dirty()
        self._discard_form()
        self.show_toast('Account updated.' if account_id else 'Account added.')

        target = normalize_category(saved['category'])
        # The saved row is named before the reload, not highlighted after it.
        # Every branch below ends in a render, and re-applying the selection
        # afterwards meant building every row a second time to do it.
        if self.search_query:
            # Stay in the results rather than dropping back to a category list.
            self.selected_id = saved['id']
            self.selected_category = target
            self._run_search(keep_selection=True)
        elif self.current_category not in AGGREGATE_TABS and target != self.current_category:
            # Adding or re-categorising can move a record out of the current
            # view; follow it so the user keeps seeing what they just saved.
            self.load_category(target, select_first=False, select=(saved['id'], target))
        else:
            self.load_category(self.current_category, select_first=False,
                               select=(saved['id'], target))

        if any(a['id'] == saved['id'] for a in self.accounts):
            self._select_account(saved['id'], target)
        else:
            # The edit moved it out of the list on screen — most often a rename
            # that no longer matches the active search.
            self.selected_id = None
            self._show_placeholder()

        if self._pending_navigation:
            self._run_pending_navigation()

    def _delete_account(self, account: dict):
        """Deletes an account, and offers it back for ten seconds.

        The confirmation stays. It is not made redundant by the undo — the two
        catch different mistakes. The dialog catches the click that was never
        meant; the undo catches the one that was meant and turns out to have
        been wrong, which no amount of confirming prevents because the user
        already believed they were sure.

        What changed is the wording. 'This cannot be undone' was true when
        there was nothing to undo it with, and repeating it next to an Undo
        button would be the app lying about its own behaviour. It says what is
        actually on offer instead: the window, and what happens when it closes.
        """
        config = get_config(account.get('_category') or self.current_category)
        name = account.get(config['name_field']) or f"this {config['item_label'].lower()}"

        confirmed = QMessageBox.question(
            self, 'Delete Account',
            f"Delete {name}?\n\nIt and its balance history go immediately. There "
            f"is a {UNDO_WINDOW_MS // 1000}-second window to undo it, and after "
            f"that only a backup can bring it back.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if confirmed != QMessageBox.Yes:
            return

        token = self._attempt(accounts_service.delete_account, account['id'])
        if token is _FAILED:
            return

        # Held rather than passed into the closure alone, so lock_vault and
        # close_vault have something to drop. A token is rows encrypted under
        # this vault's key; restoring one into a different vault would write
        # rows nothing there can open.
        self._undo_delete = token
        self.selected_id = None
        self.selected_category = None
        self._show_placeholder()
        self._reload_view()

        self.toast.show_message(
            f'{name} deleted.', UNDO_WINDOW_MS,
            action_text='Undo', on_action=lambda: self._undo_last_delete(token),
        )

    def _undo_last_delete(self, token: dict):
        """Puts back the account the toast is still offering.

        The token is compared against the one being held rather than simply
        used. The toast drops its callback when it expires, so this should be
        unreachable afterwards — but a lock in the middle of the window clears
        the held token without the toast knowing, and that is the case this
        check is for: the restore must not run against a vault that is no
        longer the one the rows came from.
        """
        if token is not self._undo_delete:
            self.show_toast('That delete can no longer be undone.', 3000)
            return
        self._undo_delete = None

        account_id = self._attempt(accounts_service.restore_account, token,
                                   prefix='Could not restore')
        if account_id is _FAILED:
            return

        self.show_toast('Account restored.')
        # Reloading first, then selecting out of what came back: the restored
        # row keeps its original id, so this lands on the same account rather
        # than on whatever now occupies its place in the list.
        self._reload_view()
        restored = next((a for a in self.accounts if a['id'] == account_id), None)
        if restored is not None:
            self._select_account(account_id, restored.get('_category'))

    def _toggle_favorite(self, account: dict, favorite: bool):
        """Pins or unpins the account on screen.

        A full reload rather than only redrawing the pane, because the pin is
        visible in three places at once: the star in the header, the star on
        the row, and the Favorites tab, which appears with the first pin and
        goes with the last.
        """
        if self._attempt(accounts_service.set_favorite, account['id'], favorite,
                         prefix='Could not change this pin') is _FAILED:
            return

        self.show_toast('Pinned to Favorites.' if favorite else 'Unpinned.')
        # Held across the reload below, which rebuilds self.accounts and so
        # invalidates the dict this was handed.
        account_id = account['id']
        category = account.get('_category')
        self._reload_view()
        # Unpinning from inside the Favorites view moves the row out of the
        # list it was selected in, which is the one case there is nothing left
        # to re-select.
        if any(a['id'] == account_id for a in self.accounts):
            self._select_account(account_id, category)
        else:
            self.selected_id = None
            self.selected_category = None
            self._show_placeholder()

    def _touch_account(self, account: dict):
        if self._attempt(accounts_service.touch_account, account['id'],
                         prefix='Failed') is _FAILED:
            return
        self.show_toast('Updated date refreshed.')
        self._refresh_selected(account)

    def _quick_update(self, account: dict):
        dialog = dialogs.QuickUpdateDialog(self, account)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        changes = dialog.changes()
        updated = dict(account)
        updated.update(changes)

        # The 'as of' date only describes an amount, so it is only meaningful
        # when one was actually entered. update_account decides from it whether
        # the figure is current or history alone, and writes both in one go.
        as_of = dialog.balance_date() if 'current_balance' in changes else None
        if self._attempt(lambda: accounts_service.update_account(
                account['id'], updated, balance_date=as_of)) is _FAILED:
            return
        self._refresh_selected(account)

    def _refresh_selected(self, account: dict):
        """Re-reads one account and refreshes both panes without losing the selection."""
        fresh = self._attempt(accounts_service.get_account, account['id'])
        if fresh is _FAILED:
            return
        fresh = self._flatten(fresh)

        for index, existing in enumerate(self.accounts):
            if existing['id'] == fresh['id']:
                self.accounts[index] = fresh
                break

        # Where the user was reading, kept across the rebuild below. Quick
        # Update, No Change and every balance-history edit land here, and all
        # three are things you do partway down a long account — being thrown
        # back to the top afterwards is the app losing your place on your
        # behalf. Deliberately does not restore which secrets were revealed:
        # re-masking on a refresh is the safer default, and nothing here is
        # worth weakening it for.
        scrollbar = self._detail_scroll.verticalScrollBar()
        offset = scrollbar.value() if self.detail_stack.currentIndex() == 1 else 0

        self.render_list()
        self.detail_view.show_account(fresh)
        self.detail_stack.setCurrentIndex(1)

        if offset:
            # Deferred: the new content has not been laid out yet, so the
            # scrollbar's range is still the old one and a value past it would
            # clamp to whatever the previous account happened to be tall.
            QTimer.singleShot(0, lambda: scrollbar.setValue(offset))

    def _on_history_changed(self):
        """A history edit can move the account's balance and last_updated — the
        newest entry is what the account currently holds — so the account is
        re-read rather than the in-memory copy being redrawn from stale values."""
        if self.selected_id is None:
            return
        account = next((a for a in self.accounts if a['id'] == self.selected_id), None)
        if account:
            self._refresh_selected(account)

    # -----------------------------------------------------------------
    # Unsaved-changes guard
    # -----------------------------------------------------------------
    def _confirm_leaving_form(self, pending) -> bool:
        """True when navigation may go ahead now.

        False means a dirty form is in the way: `pending` has been parked and
        the unsaved-changes prompt is up, and whichever button the user picks
        either runs that navigation or drops it.

        Every path that replaces the form goes through here. The check used to
        be spelled out at three of the five, and the two without it — Ctrl+E and
        extending the list selection — reached _discard_form with unsaved edits
        behind them and no prompt at all."""
        if self.form is None or not self.form.is_dirty():
            return True
        self._pending_navigation = pending
        self._show_dirty_prompt()
        return False

    def _show_dirty_prompt(self):
        box = QMessageBox(self)
        box.setWindowTitle('Unsaved Changes')
        box.setText('You have unsaved changes.')
        save = box.addButton('Save', QMessageBox.AcceptRole)
        discard = box.addButton('Discard', QMessageBox.DestructiveRole)
        box.addButton('Keep Editing', QMessageBox.RejectRole)
        box.exec()

        clicked = box.clickedButton()
        if clicked is save:
            # _save_account runs the pending navigation once the write succeeds.
            # Guarded like the Discard branch below: exec() above spins the event
            # loop, and an idle lock landing in it discards the form.
            if self.form is not None:
                self.form.submit()
        elif clicked is discard:
            if self.form is not None:
                self.form.clear_dirty()
            self._run_pending_navigation()
        else:
            self._pending_navigation = None
            self._restore_selection()

    def _run_pending_navigation(self):
        pending, self._pending_navigation = self._pending_navigation, None
        if not pending:
            return
        kind, value, category = pending

        # Every destination replaces the form, and the user has already chosen
        # Save or Discard by the time this runs — drop it here so no branch can
        # leave the resolved form mounted and have it reappear later.
        self._discard_form()

        if kind == 'select':
            self._select_account(value, category)
        elif kind == 'bulk':
            self._show_bulk(value)
        elif kind == 'edit':
            # Re-found rather than carried: choosing Save on the prompt rebuilt
            # self.accounts, so the dict captured before it is stale.
            account = next((a for a in self.accounts if a['id'] == value), None)
            if account is not None:
                self._start_edit(account)
        elif kind == 'category':
            self._hide_dashboard()
            self.load_category(value)
        elif kind == 'dashboard':
            self._show_dashboard()
        elif kind == 'add':
            self._start_add()

    # -----------------------------------------------------------------
    # Dashboard
    # -----------------------------------------------------------------
    def _dashboard_open(self) -> bool:
        # isVisible() is false for the whole main screen while the login screen
        # is up, so ask the panel whether it is the chosen view, not whether it
        # is currently painted.
        return not self.dashboard_panel.isHidden()

    def _show_dashboard(self):
        self.content_area.hide()
        self.dashboard_panel.show()
        self._check_current_tab()
        try:
            self.dashboard_view.reload()
        except VaultLockedError:
            self._handle_vault_locked()

    def _hide_dashboard(self):
        self.dashboard_panel.hide()
        self.content_area.show()
        self._check_current_tab()

    def _open_category_from_dashboard(self, category: str):
        """Clicking a category card leaves the dashboard for that category.

        Routed through _on_tab_selected rather than load_category so it takes
        the same path a tab click does — including the unsaved-form guard, and
        the tab-highlight bookkeeping that keeps the bar honest about which view
        is on screen. A category with a card but no tab (every one of its
        accounts closed since) is ignored rather than loading a view no tab
        stands for."""
        if category in self._category_tabs:
            self._on_tab_selected(category)

    # -----------------------------------------------------------------
    # Misc
    # -----------------------------------------------------------------
    def show_toast(self, message: str, duration_ms: int = 1800):
        self.toast.show_message(message, duration_ms)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.toast.isVisible():
            self.toast.reposition()

    def closeEvent(self, event):
        # close_vault runs the same sequence a vault switch does, in the same
        # order and for the same reasons — the window geometry is written back
        # before lock_app, which is where the vault stops being writable.
        #
        # Deliberately no backup here. A scheduled copy to a share that has
        # gone away can block for as long as the network stack takes to give
        # up, and a quit that will not quit is a worse failure than a backup
        # taken at the next unlock instead.
        self.close_vault()
        self._clear_clipboard()
        super().closeEvent(event)

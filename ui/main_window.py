# -----------------------------------------------------------------------------
# ui/main_window.py
# The application window.
#
# The coordinator: the widget tree, the account form's lifecycle, the
# unsaved-changes guard, the dashboard toggle, and the things that span the
# mixins — a save writes through the service, reloads the list, and puts the
# selection back. The mixins are the rest, each stating which slice of the
# window's state it owns:
#
#   ui/actions.py        commands, shortcuts, the vault menu, appearance
#   ui/vault_session.py  which vault, locked or not, and everything on a clock
#   ui/account_list.py   the list, the tab bar, search, sort, bulk actions
# -----------------------------------------------------------------------------

from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QComboBox, QDialog, QFrame, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QMainWindow, QMessageBox, QPushButton, QScrollArea,
    QSizePolicy, QSplitter, QStackedWidget, QVBoxLayout, QWidget,
)

from core import preferences
from core.categories import attention_counts, get_config, normalize_category
from core.encryption import VaultLockedError
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
from ui.vault_session import (
    VaultSessionMixin, _ActivityFilter, is_session_lock, unwatch_session_lock,
    watch_session_lock,
)
from ui.widgets import (
    FlowLayout, IconButton, NavChip, ThemedIcon, Toast, plain_label, plain_text,
)

# Nav icon sizes. The bar is a fixed 56px tall, so these do not scale with the
# font setting.
NAV_ICON_PX = 20
NAV_BRAND_PX = 22

# How long a deleted account can be brought back.
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
        # Every key attention_counts returns, so a reader before the first load
        # sees zeros rather than a KeyError.
        self.attention = attention_counts([])
        self.sort_key = 'name'
        self.sort_descending = False
        self.search_query = ''
        self.form: AccountForm | None = None
        self._pending_navigation = None
        self._sidebar_fitted = False
        # Whether the sidebar width on screen is one the user chose. An
        # auto-fitted width is not stored, so the fit keeps tracking the vault.
        self._sidebar_user_set = False
        self._warned_of_lock = False
        self._copied_value = None
        # The last deleted account, still restorable while the undo toast is up.
        # Ciphertext, and dropped by close_vault and lock_vault.
        self._undo_delete = None
        # The day the scheduled backup was last considered.
        self._backup_checked_on = None
        # The window handle registered for Windows session-lock notifications:
        # None until first shown, 0 if registration was not possible.
        self._session_watch = None

        # Before the screens: the vault menu shows these very actions.
        self._build_actions()

        # --- screens ---
        self.chooser_screen = ChooserScreen()
        self.chooser_screen.open_requested.connect(self._open_vault_dialog)
        self.chooser_screen.create_requested.connect(self._create_vault_dialog)
        self.chooser_screen.demo_requested.connect(self._create_demo_vault_dialog)
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
        """Puts the window back where it was last closed, or a default size when
        there is no stored geometry or Qt declines it (an unplugged screen)."""
        stored = preferences.get_window_geometry()
        if stored and self.restoreGeometry(stored):
            return
        self.resize(1180, 780)

    def _save_window_state(self):
        """Records the window and sidebar geometry for the next launch."""
        preferences.save_window_geometry(bytes(self.saveGeometry()))
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
        self.dashboard_view.account_activated.connect(self._open_account_from_dashboard)
        self.dashboard_panel = self._wrap_panel(
            self.dashboard_view, 'Dashboard', self.dashboard_view.section_links)
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

        # The same padlock as the window icon. Repainted by apply_appearance.
        self._brand_icon = ThemedIcon('lock', role='accent', size=NAV_BRAND_PX)

        brand_text = QLabel('AccountVault')
        brand_text.setObjectName('NavBrand')

        brand = QHBoxLayout()
        brand.setSpacing(9)
        brand.addWidget(self._brand_icon)
        brand.addWidget(brand_text)

        # Countdown chips, hidden until they have something to report.
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
        screen."""
        bar = QFrame()
        bar.setObjectName('CategoryBar')
        # Height comes from how many rows the tabs wrap onto at the current width.
        policy = QSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        policy.setHeightForWidth(True)
        bar.setSizePolicy(policy)

        self.category_buttons = QButtonGroup(self)
        self.category_buttons.setExclusive(True)

        # Flow, so the tabs wrap rather than clip in a narrow window.
        self._category_row = FlowLayout(bar, spacing=4)
        self._category_row.setContentsMargins(16, 3, 16, 3)
        bar.hide()  # Until a vault load fills it in.
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
        """A drag of the divider is the user choosing a width."""
        self._sidebar_user_set = True

    def _build_list_panel(self) -> QWidget:
        panel = self.list_panel = QFrame()
        panel.setObjectName('ListPanel')
        panel.setMinimumWidth(SIDEBAR_MIN)
        panel.setMaximumWidth(SIDEBAR_MAX)

        # A custom category's name is vault content.
        self.list_title = plain_label('All Accounts')
        self.list_title.setObjectName('ListTitle')

        self.add_button = QPushButton('+ Add New')
        self.add_button.setObjectName('Primary')
        self.add_button.setAccessibleName('Add new account')
        self.add_button.clicked.connect(self._start_add)

        # Needs Attention only: load_category shows it there.
        self.refresh_button = IconButton(
            'refresh', 'Refresh — drop accounts that no longer need attention',
            size=14, label='Refresh Needs Attention',
        )
        self.refresh_button.setObjectName('Small')
        self.refresh_button.setFixedWidth(30)
        self.refresh_button.clicked.connect(self._refresh_attention)
        self.refresh_button.hide()

        header = QHBoxLayout()
        header.addWidget(self.list_title)
        header.addWidget(self.refresh_button)
        header.addStretch(1)
        header.addWidget(self.add_button)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText('Search')
        self.search_input.setClearButtonEnabled(True)
        self.search_input.setAccessibleName('Search accounts')
        self.search_input.setAccessibleDescription(
            'Matches names, usernames and websites. Secrets and notes are excluded.'
        )
        self.search_input.textChanged.connect(self._on_search_typed)

        self.sort_combo = QComboBox()
        for key, label in SORT_OPTIONS:
            self.sort_combo.addItem(label, key)
        self.sort_combo.setToolTip('Sort order')
        self.sort_combo.setAccessibleName('Sort order')
        self.sort_combo.currentIndexChanged.connect(self._on_sort_changed)

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
        # Shift/Ctrl-click to pick several for the bulk actions. No rubber-band
        # drag: rows are tall and a stray drag would reselect half the list.
        self.account_list.setSelectionMode(QListWidget.ExtendedSelection)
        # Rows clip rather than scroll sideways: the full values are one click
        # away in the detail pane.
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
        # Keeps the splitter from shrinking a field value's column below what
        # DetailView assumes.
        panel.setMinimumWidth(DETAIL_MIN_WIDTH)

        self.placeholder = PlaceholderView()
        self.placeholder.add_requested.connect(self._start_add)

        self.detail_view = DetailView(self.settings['theme'])
        self.detail_view.edit_requested.connect(self._start_edit)
        self.detail_view.delete_requested.connect(self._delete_account)
        self.detail_view.favorite_toggled.connect(self._toggle_favorite)
        self.detail_view.quick_update_requested.connect(self._quick_update)
        self.detail_view.touch_requested.connect(self._touch_account)
        self.detail_view.navigate_requested.connect(self._step_account)
        self.detail_view.set_navigator(self._navigation_state)
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
        self._bulk_scroll = self._scrolled(self.bulk_view)

        self.detail_stack = QStackedWidget()
        self.detail_stack.addWidget(self.placeholder)
        self.detail_stack.addWidget(self._detail_scroll)
        self.detail_stack.addWidget(self._bulk_scroll)

        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.detail_stack)
        return panel

    def _wrap_panel(self, widget: QWidget, title: str,
                    controls: QWidget | None = None) -> QWidget:
        """Titled panel, with any controls beside the title. Leaving it is the
        tab bar's job, so there is no close button."""
        panel = QFrame()
        panel.setObjectName('DashboardPanel')

        heading = QLabel(title)
        heading.setObjectName('PanelTitle')

        header = QHBoxLayout()
        header.setContentsMargins(16, 12, 16, 8)
        header.setSpacing(16)
        header.addWidget(heading)
        if controls is not None:
            header.addWidget(controls)
        header.addStretch(1)

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
        # Guarded here so Ctrl+E and the Edit button share the one check.
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
        # The saved row is named before the reload, so a single render
        # highlights it.
        if self.search_query:
            self.selected_id = saved['id']
            self.selected_category = target
            self._run_search(keep_selection=True)
        elif self.current_category not in AGGREGATE_TABS and target != self.current_category:
            # Adding or recategorising moved the record out of this view; follow it.
            self.load_category(target, select_first=False, select=(saved['id'], target))
        else:
            self.load_category(self.current_category, select_first=False,
                               select=(saved['id'], target))

        if any(a['id'] == saved['id'] for a in self.accounts):
            self._select_account(saved['id'], target)
        else:
            # The edit moved it out of the list — a rename that no longer
            # matches the search, say.
            self.selected_id = None
            self._show_placeholder()

        if self._pending_navigation:
            self._run_pending_navigation()

    def _delete_account(self, account: dict):
        """Deletes an account after confirmation, and offers it back for ten
        seconds. The confirmation catches the click that was never meant; the
        undo catches the one that was meant and turned out wrong."""
        config = get_config(account.get('_category') or self.current_category)
        name = account.get(config['name_field']) or f"this {config['item_label'].lower()}"

        confirmed = QMessageBox.question(
            self, 'Delete Account',
            plain_text(f"Delete {name}?\n\nIt and its balance history go immediately. "
                       f"There is a {UNDO_WINDOW_MS // 1000}-second window to undo it, "
                       f"and after that only a backup can bring it back."),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if confirmed != QMessageBox.Yes:
            return

        token = self._attempt(accounts_service.delete_account, account['id'])
        if token is _FAILED:
            return

        # Held on the window as well as in the closure, so lock_vault and
        # close_vault have something to drop.
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

        The token is compared with the one held: a lock during the window clears
        the held token without the toast knowing, and the restore must not run
        against a vault the rows did not come from."""
        if token is not self._undo_delete:
            self.show_toast('That delete can no longer be undone.', 3000)
            return
        self._undo_delete = None

        account_id = self._attempt(accounts_service.restore_account, token,
                                   prefix='Could not restore')
        if account_id is _FAILED:
            return

        self.show_toast('Account restored.')
        # The restored row keeps its original id.
        self._reload_view()
        restored = next((a for a in self.accounts if a['id'] == account_id), None)
        if restored is not None:
            self._select_account(account_id, restored.get('_category'))

    def _toggle_favorite(self, account: dict, favorite: bool):
        """Pins or unpins the account on screen. A full reload, because the pin
        shows on the header, the row, and the Favorites tab."""
        if self._attempt(accounts_service.set_favorite, account['id'], favorite,
                         prefix='Could not change this pin') is _FAILED:
            return

        self.show_toast('Pinned to Favorites.' if favorite else 'Unpinned.')
        # Held across the reload, which replaces the dict this was handed.
        account_id = account['id']
        category = account.get('_category')
        self._reload_view()
        # Unpinning from inside Favorites moves the row out of the list.
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

        # The 'as of' date only describes an amount, so it applies only when
        # one was entered.
        as_of = dialog.balance_date() if 'current_balance' in changes else None
        if self._attempt(lambda: accounts_service.update_account(
                account['id'], updated, balance_date=as_of)) is _FAILED:
            return
        self._refresh_selected(account)

    def _refresh_selected(self, account: dict):
        """Re-reads one account and refreshes both panes without losing the
        selection or the reading position."""
        fresh = self._attempt(accounts_service.get_account, account['id'])
        if fresh is _FAILED:
            return
        fresh = self._flatten(fresh)

        for index, existing in enumerate(self.accounts):
            if existing['id'] == fresh['id']:
                self.accounts[index] = fresh
                break

        # Revealed secrets are deliberately re-masked by the rebuild; only the
        # scroll position is kept.
        scrollbar = self._detail_scroll.verticalScrollBar()
        offset = (scrollbar.value()
                  if self.detail_stack.currentWidget() is self._detail_scroll else 0)

        self.render_list()
        self.detail_view.show_account(fresh)
        self.detail_stack.setCurrentWidget(self._detail_scroll)

        if offset:
            # Deferred until the new content has a scroll range to hold it.
            QTimer.singleShot(0, lambda: scrollbar.setValue(offset))

    def _on_history_changed(self):
        """A history edit can move the account's balance and last_updated, so
        the account is re-read rather than redrawn."""
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
        the unsaved-changes prompt is up, and the user's answer either runs that
        navigation or drops it. Every path that replaces the form, or closes
        the window, goes through here."""
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
            # exec() spins the event loop, and an idle lock there discards the
            # form, hence the checks.
            if self.form is not None:
                self.form.submit()
            if self.form is not None:
                # The save was refused and the form is still up with its error.
                # Drop the navigation rather than run it after some later save.
                self._pending_navigation = None
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

        # Save or Discard has been chosen by now, so the form goes either way.
        self._discard_form()

        if kind == 'select':
            self._select_account(value, category)
        elif kind == 'bulk':
            self._show_bulk(value)
        elif kind == 'edit':
            # Re-found: a Save on the prompt rebuilt self.accounts.
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
        elif kind == 'quit':
            # Deferred: this can run from inside closeEvent.
            QTimer.singleShot(0, self.close)

    # -----------------------------------------------------------------
    # Dashboard
    # -----------------------------------------------------------------
    def _dashboard_open(self) -> bool:
        # Whether the panel is the chosen view, not whether it is painted —
        # the whole main screen is hidden while the login screen is up.
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
        """Clicking a category card goes to that category, the same way a tab
        click does. A category with a card but no tab is ignored."""
        if category in self._category_tabs:
            self._on_tab_selected(category)

    def _open_account_from_dashboard(self, account_id: int, category: str):
        """Clicking a name in the investing section opens that account in its
        category's list, with the row highlighted and its details showing."""
        if category not in self._category_tabs:
            return
        # Reaching the dashboard already settled any dirty form, so this only
        # ever prompts if something reopened one since.
        if not self._confirm_leaving_form(('category', category, None)):
            return
        self._hide_dashboard()
        self.load_category(category, select_first=False, select=(account_id, category))
        if any(a['id'] == account_id for a in self.accounts):
            self._select_account(account_id, category)
        else:
            # Gone since the dashboard was drawn: show the category as a tab
            # click would.
            self.selected_id = None
            self.selected_category = None
            self._select_first_row()

    # -----------------------------------------------------------------
    # Misc
    # -----------------------------------------------------------------
    def show_toast(self, message: str, duration_ms: int = 1800):
        self.toast.show_message(message, duration_ms)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.toast.isVisible():
            self.toast.reposition()

    def showEvent(self, event):
        super().showEvent(event)
        if self._session_watch is None:
            hwnd = int(self.winId())
            self._session_watch = hwnd if watch_session_lock(hwnd) else 0

    def nativeEvent(self, event_type, message):
        if is_session_lock(event_type, message):
            # Deferred: the lock tears down widgets, which is not something to
            # do inside native message dispatch.
            QTimer.singleShot(0, self._on_session_locked)
        return super().nativeEvent(event_type, message)

    def closeEvent(self, event):
        # A quit with unsaved edits asks first — only while the window is on
        # screen, since a hidden window has nobody to ask. The auto-shutdown
        # only runs after a lock, which has already discarded the form.
        if self.isVisible() and not self._confirm_leaving_form(('quit', None, None)):
            event.ignore()
            return

        if self._session_watch:
            unwatch_session_lock(self._session_watch)
            self._session_watch = None

        # The same teardown a vault switch runs. No backup here: a copy to a
        # share that has gone away can block a quit for as long as the network
        # takes to give up.
        self.close_vault()
        self._clear_clipboard()
        super().closeEvent(event)

# -----------------------------------------------------------------------------
# ui/actions.py
# The window's command layer: the QActions and their key sequences, the vault
# menu that lists those same objects, the guard every handler runs behind, and
# the appearance settings the commands move.
#
# A mixin rather than a collaborator: these methods reach all over the window,
# and a helper object would only be handed a reference to it. What the split
# buys is a file about shortcuts, and each mixin stating which slice of the
# window's state it owns.
#
# What this one owns: self._actions.
# -----------------------------------------------------------------------------

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from PySide6.QtWidgets import QApplication, QMenu

from core import auth
from core import preferences
from core.categories import get_config
from core.encryption import VaultLockedError
from ui import dialogs
from ui.theme import FONT_SIZES, stylesheet
from ui.widgets import IconButton, NavChip, ThemedIcon

# What _attempt returns when the call failed and the user has already been told.
# An object() rather than None so a call that legitimately returns None (or 0,
# or an empty list) is not mistaken for a failure.
_FAILED = object()

# How many tabs Ctrl+<n> can reach.
_TAB_SHORTCUT_LIMIT = 9


class ActionsMixin:
    """Commands, shortcuts, the vault menu, and the appearance they move."""

    # -----------------------------------------------------------------
    # Commands
    # -----------------------------------------------------------------
    def _build_actions(self):
        """The window's commands, one QAction each.

        One owner per key sequence: Qt answers two candidates for one sequence
        by dispatching to neither, so the menu shows these same objects rather
        than copies. Every slot is guarded so a shortcut is inert off the main
        screen — Ctrl+N on the login screen must not build a form against a
        locked vault.

        Where a command lists several sequences, the first is the one the menu
        shows and the rest are the same physical key as Qt actually reports it:
        on a US layout "Ctrl and +" arrives as Key_Plus carrying Shift, and the
        keypad's keys carry KeypadModifier."""
        commands = [
            ('add',       'Add Account',   ['Ctrl+N'], self._start_add,
             'Create a new account in the current category'),
            ('settings',  'Settings…',     ['Ctrl+,'], self._open_settings,
             'Theme, font size, list density, and the lock and clipboard timers'),
            ('theme',     'Dark Mode',     [], self._toggle_theme,
             'Switch between the dark and light palettes'),
            ('font_up',   'Larger Text',
             ['Ctrl++', 'Ctrl+=',
              QKeySequence(Qt.ControlModifier | Qt.ShiftModifier | Qt.Key_Plus),
              QKeySequence(Qt.ControlModifier | Qt.KeypadModifier | Qt.Key_Plus)],
             lambda: self._step_font(1), 'One step up the font-size ladder'),
            ('font_down', 'Smaller Text',
             ['Ctrl+-', 'Ctrl+_',
              QKeySequence(Qt.ControlModifier | Qt.KeypadModifier | Qt.Key_Minus)],
             lambda: self._step_font(-1), 'One step down the font-size ladder'),
            ('lock',      'Lock Vault',    ['Ctrl+L'], self.lock_vault,
             'Release the encryption key and return to the login screen'),
            ('quit',      'Quit',          ['Ctrl+Q'], self.close, ''),
            # No menu entry of their own; they act on the list selection.
            ('find',      'Find',          ['Ctrl+F'], self._focus_search, ''),
            ('edit',      'Edit Account',  ['Ctrl+E'], self._edit_selected, ''),
            ('copy_user', 'Copy Username', ['Ctrl+B'],
             lambda: self._copy_field('username'), ''),
            ('copy_pass', 'Copy Password', ['Ctrl+Shift+C'],
             lambda: self._copy_field('password'), ''),
        ]

        self._actions: dict[str, QAction] = {}
        for key, label, sequences, slot, tooltip in commands:
            action = QAction(label, self)
            action.setShortcuts([QKeySequence(sequence) for sequence in sequences])
            action.setShortcutContext(Qt.WindowShortcut)
            action.setShortcutVisibleInContextMenu(True)
            if tooltip:
                action.setToolTip(tooltip)
            action.triggered.connect(self._guarded(slot))
            # The window is what gives an action's sequences somewhere to match.
            self.addAction(action)
            self._actions[key] = action

        # The one command with a state to show, so it carries a tick, kept in
        # step by apply_appearance.
        theme_action = self._actions['theme']
        theme_action.setCheckable(True)
        theme_action.setChecked(self.settings.get('theme') == 'dark')

    def _build_shortcuts(self):
        """The bindings with no command object of their own: the tab digits and
        Escape."""
        # Ctrl+1..9 select the nth tab as the bar actually reads.
        for digit in range(1, _TAB_SHORTCUT_LIMIT + 1):
            shortcut = QShortcut(QKeySequence(f'Ctrl+{digit}'), self)
            shortcut.activated.connect(self._guarded(
                lambda index=digit - 1: self._select_tab_by_index(index)
            ))

        # Escape clears an active search, from anywhere in the list. It does
        # nothing when the box is empty, leaving Escape to any dialog.
        escape = QShortcut(QKeySequence(Qt.Key_Escape), self)
        escape.activated.connect(self._guarded(self._clear_search))

        # Down from the search box moves into the results.
        self.search_input.installEventFilter(self)

    def _build_manage_menu(self) -> QMenu:
        """The vault menu on the nav bar's gear button. Entries named by a
        string are the shared QActions from _build_actions; the descriptions
        are tooltips."""
        menu = QMenu(self)
        # Off by default in Qt; without it the descriptions never show.
        menu.setToolTipsVisible(True)

        entries = [
            'add',
            None,
            'settings',
            ('Warnings…',             self._open_warnings,
             'When due dates and stale balances turn orange and red, per category'),
            'theme',
            'font_up',
            'font_down',
            ('Change Master Password…', self._change_password,
             'Re-encrypts the whole vault under a new password'),
            None,
            ('Save a Copy…',          self._save_copy,
             'Write a backup copy of this vault — opens with the same master password'),
            ('Move Vault…',           self._move_vault,
             'Move this vault to another folder and keep working there'),
            ('Open Vault…',           self._open_from_menu,
             'Lock this vault and open a different one'),
            ('New Vault…',            self._create_from_menu,
             'Lock this vault and create a new, separate one'),
            None,
            ('Import Accounts…',      self._import_accounts,
             "Read accounts in from a CSV — this app's own export, another "
             "password manager's, or a spreadsheet"),
            ('Export CSV…',           self._export_csv,
             'Save accounts as a plaintext spreadsheet — unencrypted'),
            None,
            ('Readme',                lambda: dialogs.ReadmeDialog(self).exec(),
             'Features, security details, and usage notes'),
            ('Design Document',       lambda: dialogs.DesignDocumentDialog(self).exec(),
             'Why the app is built the way it is'),
            ('About',                 lambda: dialogs.AboutDialog(self).exec(),
             'Version and encryption information'),
            None,
            'lock',
            'quit',
        ]

        for entry in entries:
            if entry is None:
                menu.addSeparator()
                continue
            if isinstance(entry, str):
                menu.addAction(self._actions[entry])
                continue
            label, slot, tooltip = entry
            action = menu.addAction(label)
            action.triggered.connect(slot)
            if tooltip:
                action.setToolTip(tooltip)
        return menu

    # -----------------------------------------------------------------
    # Guards
    # -----------------------------------------------------------------
    def _on_main_screen(self) -> bool:
        """True when the vault is open and its browser is the visible screen."""
        return self.screens.currentWidget() is self.main_screen

    def _guarded(self, slot):
        """Wraps a shortcut handler so it is inert off the main screen. Takes and
        drops any arguments, so it serves QAction.triggered and
        QShortcut.activated alike."""
        def run(*_args):
            if self._on_main_screen():
                slot()
        return run

    def _attempt(self, call, *args, prefix: str = '', dwell: int = 1800,
                 report=None):
        """Runs a vault call, reporting any failure and returning _FAILED.

        A vault found locked underneath goes to _handle_vault_locked; anything
        else is shown — in a toast, or through `report` (a form's error line,
        say). `prefix` heads the message and `dwell` holds the toast longer.
        Calls needing keyword arguments pass a lambda."""
        try:
            return call(*args)
        except VaultLockedError:
            self._handle_vault_locked()
        except Exception as exc:
            message = f"{prefix}: {exc}" if prefix else str(exc)
            if report is not None:
                report(message)
            else:
                self.show_toast(message, dwell)
        return _FAILED

    # -----------------------------------------------------------------
    # Appearance
    # -----------------------------------------------------------------
    def apply_appearance(self, theme: str, font_size: str,
                         list_density: str | None = None):
        """Puts the three appearance settings on screen, previewed or saved.
        list_density defaults to whatever is already in force."""
        if list_density is None:
            list_density = self.settings.get('list_density', 'compact')

        # Captured before self.settings is updated below.
        font_changed = font_size != self.settings.get('font_size')
        density_changed = list_density != self.settings.get('list_density')

        app = QApplication.instance()
        app.setStyleSheet(stylesheet(theme, font_size))

        font = app.font()
        font.setPointSize(FONT_SIZES.get(font_size, FONT_SIZES['default']))
        app.setFont(font)

        self.settings['theme'] = theme
        self.settings['font_size'] = font_size
        self.settings['list_density'] = list_density
        # setChecked emits toggled, not triggered, so this cannot loop back.
        self._actions['theme'].setChecked(theme == 'dark')
        self.detail_view.set_theme(theme)
        self.dashboard_view.set_theme(theme)

        # Icons are pixmaps with their colour baked in; redraw every one.
        for widget in (self.findChildren(IconButton) + self.findChildren(ThemedIcon)
                       + self.findChildren(NavChip)):
            widget.apply_theme(theme)

        # Category tabs fix their width, and list rows store a size hint, from
        # the font in force when they were built — re-measure both after a font
        # change. Guarded on there being tabs, which only exist once a vault is
        # loaded; a theme-only change moves no metrics.
        if font_changed and self._category_tabs:
            self._rebuild_category_bar()
            self._check_current_tab()

        if (font_changed or density_changed) and self._category_tabs:
            # The sidebar was fitted to the old metrics; fit again unless the
            # user dragged the divider themselves.
            if not self._sidebar_user_set:
                self._sidebar_fitted = False
            self.render_list()

        # Chart colours are baked in when the cards are built, so the dashboard
        # is rebuilt — only when it is on screen, since that decrypts every
        # balance entry.
        if self._dashboard_open():
            try:
                self.dashboard_view.reload()
            except VaultLockedError:
                self._handle_vault_locked()

    def _apply_saved_appearance(self):
        """Re-applies the appearance settings from self.settings — settling a
        Settings preview, or putting back what a failed save left."""
        self.apply_appearance(self.settings['theme'], self.settings['font_size'],
                              self.settings.get('list_density'))

    def _toggle_theme(self):
        """Flips between the two palettes from the vault menu, and saves it."""
        theme = 'light' if self.settings.get('theme') == 'dark' else 'dark'
        try:
            preferences.save_settings(**{**self.settings, 'theme': theme})
            auth.apply_saved_settings()
        except Exception as exc:
            self.show_toast(str(exc))
            return

        self.apply_appearance(theme, self.settings['font_size'])
        self.show_toast(f"{theme.capitalize()} mode.")

    def _step_font(self, step: int):
        """Moves the font one place along preferences.FONT_SIZE_CHOICES and saves it.

        self.settings['font_size'] must not be updated here: apply_appearance
        compares against it to decide whether to re-measure tabs and rows."""
        order = list(preferences.VALID_FONT_SIZES)
        labels = dict(preferences.FONT_SIZE_CHOICES)
        current = self.settings.get('font_size', 'default')
        index = order.index(current) if current in order else order.index('default')
        target = index + step

        if not 0 <= target < len(order):
            edge = 'largest' if step > 0 else 'smallest'
            self.show_toast(f"Text is already at its {edge} ({labels[order[index]]}).")
            return

        key = order[target]
        try:
            preferences.save_settings(**{**self.settings, 'font_size': key})
            auth.apply_saved_settings()
        except Exception as exc:
            self.show_toast(str(exc))
            return

        self.apply_appearance(self.settings['theme'], key)
        self.show_toast(f"Text size: {labels[key]}.")

    # -----------------------------------------------------------------
    # Handlers with no view of their own
    # -----------------------------------------------------------------
    def _focus_search(self):
        self.search_input.setFocus()
        self.search_input.selectAll()

    def _clear_search(self):
        if not self.search_input.text():
            return
        self.search_input.clear()
        # Straight to the reload rather than through the search debounce: this
        # is one deliberate action, not a run of keystrokes.
        self._search_timer.stop()
        self._run_search()
        self.account_list.setFocus()

    def _edit_selected(self):
        account = self._selected_account()
        if account is not None:
            self._start_edit(account)

    def _selected_account(self) -> dict | None:
        if self.selected_id is None:
            return None
        return next((a for a in self.accounts if a['id'] == self.selected_id), None)

    def _copy_field(self, key: str):
        """Copies one field of the selected account to the clipboard, through the
        same copy_to_clipboard the detail view's buttons use."""
        account = self._selected_account()
        if account is None:
            self.show_toast('Select an account first.')
            return

        config = get_config(account.get('_category') or self.current_category)
        field = next((f for f in config['fields'] if f['key'] == key), None)
        if field is None:
            self.show_toast(f"This {config['item_label'].lower()} has no {key}.")
            return
        self.copy_to_clipboard(str(account.get(key) or ''), field['label'])

    def _select_tab_by_index(self, index: int):
        if 0 <= index < len(self._tab_order):
            self._on_tab_selected(self._tab_order[index])

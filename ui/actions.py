# -----------------------------------------------------------------------------
# ui/actions.py
# The window's command layer.
#
# Everything the window can be *asked* to do, as opposed to what it draws or
# what it holds: the QActions and their key sequences, the vault menu those
# same objects are listed in, the guard every handler runs behind, and the
# three appearance settings the commands move.
#
# A mixin rather than a helper object, and that is a deliberate limit on what
# this split claims to have done. These methods reach all over the window —
# self.settings, self.detail_view, self._category_tabs — and turning them into
# a collaborator would mean handing it a reference to the window and inverting
# nothing. What the split buys is that the file you open to change a shortcut
# is 400 lines about shortcuts rather than 2,900 lines about everything, and
# that each of the four now states which slice of the window's state it owns.
#
# What this one owns: self._actions.
# -----------------------------------------------------------------------------

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from PySide6.QtWidgets import QApplication, QMenu

import auth
import preferences
from categories import get_config
from encryption import VaultLockedError
from ui import dialogs
from ui.theme import FONT_SIZES, stylesheet
from ui.widgets import IconButton, NavChip, ThemedIcon

# What _attempt returns when the call failed and the user has already been told.
# An object() rather than None so a call that legitimately returns None (or 0,
# or an empty list) is not mistaken for a failure.
_FAILED = object()

# The font-size ladder, smallest first, labelled as Settings labels it. Ctrl+=
# and Ctrl+- step along this list, so its order is what "one size up" means and
# it has to stay in step with preferences.VALID_FONT_SIZES.
FONT_SIZE_STEPS = [
    ('small', 'Small'), ('default', 'Default'), ('large', 'Large'), ('xl', 'XL'),
]

# How many category tabs Ctrl+<n> can reach. Past nine the digits run out, and
# a tab that far along is quicker to click than to count to.
_TAB_SHORTCUT_LIMIT = 9


class ActionsMixin:
    """Commands, shortcuts, the vault menu, and the appearance they move."""

    # -----------------------------------------------------------------
    # Commands
    # -----------------------------------------------------------------
    def _build_actions(self):
        """The window's commands, one QAction each.

        Nearly all of them own a key sequence; the theme toggle is here because
        it needs the same thing the keyed ones need — a single object the menu
        can show and the window can keep in step — rather than because it has a
        shortcut.

        One owner per sequence, and that is the whole point. These used to be
        QShortcuts here plus a separate menu QAction repeating the same key, and
        Qt's shortcut map answers two candidates for one sequence by dispatching
        to neither: Ctrl+N, Ctrl+,, Ctrl+L and Ctrl+Q were advertised in the menu
        and did nothing when pressed, while every sequence with no menu entry
        behind it (Ctrl+F, Ctrl+E, Ctrl+B, Ctrl+1..9) kept working. Narrowing the
        menu copy to WidgetShortcut was meant to break that tie; measured against
        real key events it does not, because the action stays in contention
        either way. The menu now shows these objects instead of copying them, so
        there is one handler per key and the hint in the menu is rendered from
        the handler itself and cannot drift from it.

        Every slot goes through _on_main_screen first. An action lives on the
        window rather than on a screen within it, so without that guard Ctrl+N on
        the login screen would start building an account form against a locked
        vault.

        Where a command lists several sequences, the first is the one the menu
        shows and the rest are the same physical key as Qt actually reports it.
        The font pair needs all of them: on a US layout "Ctrl and +" is really
        Ctrl+Shift+= , which Qt delivers as Key_Plus *carrying* Shift, so plain
        Ctrl++ never matches it; Ctrl+= is that key without reaching for Shift;
        and the keypad's own +/- arrive with KeypadModifier set, which likewise
        matches none of the others. Each was checked against real key events
        rather than assumed — Ctrl+Shift+= , the obvious spelling, matches
        nothing, because by then the key is no longer '='.
        """
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
            # Without this the action is only a menu entry; the window is what
            # gives its sequences somewhere to be matched.
            self.addAction(action)
            self._actions[key] = action

        # The one command with a state to show rather than only an effect, so it
        # is the one that carries a tick. apply_appearance sets that tick, and
        # every theme change in the app goes through there — this entry, the
        # Settings swatches and a cancelled preview cannot end up disagreeing.
        theme_action = self._actions['theme']
        theme_action.setCheckable(True)
        theme_action.setChecked(self.settings.get('theme') == 'dark')

    def _build_shortcuts(self):
        """The bindings with no command object of their own: the tab digits and
        Escape. Everything that also appears in a menu is a QAction instead —
        see _build_actions."""
        # Ctrl+1..9 select the nth tab as the bar actually reads, Dashboard
        # included — the bar is the map, so the digits have to follow it rather
        # than a fixed category order the user cannot see.
        for digit in range(1, _TAB_SHORTCUT_LIMIT + 1):
            shortcut = QShortcut(QKeySequence(f'Ctrl+{digit}'), self)
            shortcut.activated.connect(self._guarded(
                lambda index=digit - 1: self._select_tab_by_index(index)
            ))

        # Escape clears an active search. Bound to the window rather than the
        # search box so it works from anywhere in the list, and it deliberately
        # does nothing when the box is empty — swallowing Escape when there is
        # nothing to clear would take it from any dialog that wanted it.
        escape = QShortcut(QKeySequence(Qt.Key_Escape), self)
        escape.activated.connect(self._guarded(self._clear_search))

        # Down from the search box moves into the results without reaching for
        # the mouse, which is the whole point of Ctrl+F.
        self.search_input.installEventFilter(self)

    def _build_manage_menu(self) -> QMenu:
        """The vault menu, replacing the old Manage dialog.

        The one-line descriptions that dialog showed under each action are kept
        as tooltips rather than dropped — they explain the difference between an
        encrypted backup and a plaintext CSV, which is the single most important
        thing a user can misunderstand here.

        Entries named by a string are the shared QAction objects from
        _build_actions rather than new ones repeating their sequence; see there
        for why a second copy stopped the key working at all. Their labels and
        tooltips travel with the action, so only the shortcut-less entries carry
        their own here."""
        menu = QMenu(self)
        # Off by default in Qt; without it the descriptions below never show.
        menu.setToolTipsVisible(True)

        entries = [
            'add',
            None,
            'settings',
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
        return self.screens.currentIndex() == 2

    def _guarded(self, slot):
        """Wraps a shortcut handler so it is inert off the main screen.

        Takes and drops any arguments: QAction.triggered hands its slot a
        checked flag, QShortcut.activated hands it nothing, and the same wrapper
        serves both."""
        def run(*_args):
            if self._on_main_screen():
                slot()
        return run

    def _attempt(self, call, *args, prefix: str = '', dwell: int = 1800,
                 report=None):
        """Runs a vault call, reporting any failure and returning _FAILED.

        The shape nearly every service call in this window shares: a vault found
        locked underneath goes to _handle_vault_locked, anything else is shown
        to the user, and either way the caller stops. Callers test the result
        against _FAILED rather than catching.

        `prefix` heads the message ('Export failed: ...'), `dwell` holds the
        toast longer than usual, and `report` sends the message somewhere other
        than a toast — a form's own error line, say. Calls needing keyword
        arguments pass a lambda instead of args."""
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

        list_density defaults to whatever is already in force, so the callers
        that only ever move theme or font — startup, unlock, the Ctrl+/Ctrl-
        pair — can keep passing two arguments and leave density alone.
        """
        if list_density is None:
            list_density = self.settings.get('list_density', 'compact')

        # Captured before the settings dict is updated below, since that is what
        # says which size and density we are coming from.
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
        # Set rather than toggled, so the tick tracks the theme that actually
        # landed — including one arriving from the Settings swatches or from a
        # preview being reverted, neither of which went through the menu.
        # setChecked emits toggled, not triggered, so this cannot loop back into
        # _toggle_theme.
        self._actions['theme'].setChecked(theme == 'dark')
        self.detail_view.set_theme(theme)
        self.dashboard_view.set_theme(theme)

        # Icons are rendered pixmaps with their colour already baked in, and no
        # style sheet can recolour one — every icon on screen has to be redrawn
        # against the new palette by hand.
        for widget in (self.findChildren(IconButton) + self.findChildren(ThemedIcon)
                       + self.findChildren(NavChip)):
            widget.apply_theme(theme)

        # Two things measure themselves against the font in force when they are
        # built and then hold that number: a category tab's setFixedWidth, and
        # the size hint render_list() stores on every row. Neither notices the
        # font moving underneath it, so after a change in Settings the tabs kept
        # the old size while everything around them grew, and the rows clipped
        # their own text. Both are re-measured below.
        #
        # All of it is guarded on there being tabs, which only exist once a
        # vault has been loaded — this also runs before login, where there is
        # nothing to measure — and skipped for a theme-only change, which moves
        # no metrics and would otherwise rebuild every row for nothing.
        #
        # Only the font reaches the tabs; density is a property of a list row
        # and leaves the bar alone.
        if font_changed and self._category_tabs:
            self._rebuild_category_bar()
            self._check_current_tab()

        # A row carries both the font metrics and the density layout, so either
        # one moving means every row has to be built again. This is what makes
        # density preview live from Settings rather than appear only on save.
        if (font_changed or density_changed) and self._category_tabs:
            # The sidebar was fitted to the old metrics and every row has just
            # changed size underneath it, so the fit is stale — at XL a compact
            # row's meta line ran off the edge of a sidebar sized for 10pt. Fit
            # again from the new measurements, unless the user has dragged the
            # divider themselves, in which case their width stands.
            if not self._sidebar_user_set:
                self._sidebar_fitted = False
            self.render_list()

        # Chart colours are baked in when the cards are built, so the dashboard
        # only picks up a theme change by being rebuilt. Guarded on it being the
        # visible view: rebuilding one nobody is looking at costs a decrypt of
        # every balance entry, and this also runs once before login, where
        # reload() would hit a locked vault.
        if self._dashboard_open():
            try:
                self.dashboard_view.reload()
            except VaultLockedError:
                self._handle_vault_locked()

    def _apply_saved_appearance(self):
        """Re-applies the three appearance settings from self.settings.

        On the way out of the Settings dialog this is what turns a preview into
        the settled state, and on a save failure it is what puts back whatever
        self.settings still holds."""
        self.apply_appearance(self.settings['theme'], self.settings['font_size'],
                              self.settings.get('list_density'))

    def _toggle_theme(self):
        """Flips between the two palettes straight from the vault menu.

        Saved rather than held for the session, for the same reason the font
        keys save: a palette picked here and one picked in Settings are the same
        setting, and it would be strange for only one of them to survive a
        restart. apply_appearance is what puts the new tick on the menu entry.
        """
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
        """Moves the font one place along FONT_SIZE_STEPS and saves it there.

        Written through preferences.save_settings rather than held in memory, so a size
        reached by keyboard survives a restart exactly as one picked in Settings
        does, and Settings opens showing the size actually on screen.

        apply_appearance is what updates self.settings, and it does so *after*
        comparing the size it was handed against the one still recorded there —
        that comparison is how it decides whether to re-measure the category
        tabs and list rows that cache metrics from the old font. So nothing here
        may write self.settings['font_size'] first: doing so would make the test
        always false and leave both stale. The Settings dialog previews through
        the same call for the same reason.
        """
        order = [key for key, _ in FONT_SIZE_STEPS]
        current = self.settings.get('font_size', 'default')
        index = order.index(current) if current in order else order.index('default')
        target = index + step

        if not 0 <= target < len(order):
            edge = 'largest' if step > 0 else 'smallest'
            self.show_toast(f"Text is already at its {edge} ({FONT_SIZE_STEPS[index][1]}).")
            return

        key, label = FONT_SIZE_STEPS[target]
        try:
            preferences.save_settings(**{**self.settings, 'font_size': key})
            auth.apply_saved_settings()
        except Exception as exc:
            self.show_toast(str(exc))
            return

        self.apply_appearance(self.settings['theme'], key)
        self.show_toast(f"Text size: {label}.")

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
        # Straight to the reload rather than through the 250ms debounce the
        # clear() above just restarted. That delay is there to keep a run of
        # keystrokes from decrypting the vault once per letter; an explicit
        # Escape is one deliberate action, and waiting it out makes the key
        # feel broken.
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
        """Copies one field of the selected account straight to the clipboard.

        Goes through the same copy_to_clipboard the detail view's buttons use,
        so the clipboard-history opt-out and the auto-clear timer apply exactly
        as they do to a clicked copy."""
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

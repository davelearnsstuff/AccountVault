# -----------------------------------------------------------------------------
# ui/vault_session.py
# Which vault is open, whether it is unlocked, and everything on a clock.
#
# The screens before the browser (chooser, setup, login), opening and creating
# and moving vaults, the unlock and the lock, the idle timer and the two
# countdowns it drives, the clipboard and its auto-clear, the scheduled backup,
# and the vault-level entries in the Manage menu.
#
# These belong together because they share one invariant that nothing else in
# the window does: at any moment either a key is held or it is not, and
# everything here either establishes that state, ends it, or has to be torn
# down when it ends. close_vault and lock_vault are the two places that list
# the teardown, and a new piece of per-vault state that is not dropped in both
# is a piece that follows the user into the next vault.
#
# See ui/actions.py for why these are mixins.
#
# What this one owns: self.settings, the four timers, self._copied_value,
# self._warned_of_lock, self._backup_checked_on.
# -----------------------------------------------------------------------------

import datetime
from pathlib import Path

from PySide6.QtCore import QEvent, QMimeData, QObject
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QApplication, QDialog, QFileDialog, QMessageBox,
)

import auth
import database
import preferences
import vaultfile
from encryption import VaultLockedError
from services import accounts as accounts_service
from services import backups as backups_service
from services import csv_export
from services import export_io
from ui import dialogs
from ui.account_list import ALL
from ui.actions import _FAILED
from ui.widgets import export_save_path

# How often the idle timer checks the session, and how long before the lock the
# user gets a warning. The tick is a second because it also drives the nav
# countdowns, which have to move visibly or they read as frozen.
_SESSION_TICK_MS = 1_000
_WARN_BEFORE_SECONDS = 120


def _secret_mime(value: str) -> QMimeData:
    """Clipboard payload for a secret, opted out of Windows clipboard history
    and cloud sync.

    Clearing the clipboard on a timer does not reach either of those: without
    these markers a copied password survives in the Win+V history pane after
    the timer fires, and syncs to other machines when cloud clipboard is on.
    Windows reads the three format names below; every other platform ignores
    unknown formats, so this is inert off Windows.

    The two boolean-ish formats want a little-endian DWORD 0, not an empty
    payload — Windows treats a missing or malformed value as "allowed"."""
    mime = QMimeData()
    mime.setText(value)
    mime.setData('ExcludeClipboardContentFromMonitorProcessing', b'\x00\x00\x00\x00')
    mime.setData('CanIncludeInClipboardHistory', b'\x00\x00\x00\x00')
    mime.setData('CanUploadToCloudClipboard', b'\x00\x00\x00\x00')
    return mime


class VaultSessionMixin:
    """The vault lifecycle: which one is open, whether it is unlocked, and the
    timers that end that state."""

    # -----------------------------------------------------------------
    # Choosing which screen is up
    # -----------------------------------------------------------------
    def start(self, notice: str = ''):
        """Chooses the opening screen.

        Three states, not two. No vault open means the chooser — a first-ever
        launch, or one where the recorded vault has been moved out from under
        us, which is what `notice` explains. An open vault with no master hash
        is one that was created and not finished, so setup picks up where it
        left off. Otherwise: unlock.
        """
        if database.DB_PATH is None:
            self._show_chooser(notice)
            return

        if auth.is_first_launch():
            self.setup_screen.set_vault(database.DB_PATH)
            self.screens.setCurrentWidget(self.setup_screen)
            self.setup_screen.reset()
        else:
            self._show_login()
            if notice:
                self.login_screen.show_error(notice)

    def _show_chooser(self, notice: str = ''):
        self._session_timer.stop()
        self._shutdown_timer.stop()
        self.screens.setCurrentWidget(self.chooser_screen)
        self.chooser_screen.reset(notice)

    def _show_login(self):
        self.screens.setCurrentWidget(self.login_screen)
        self.login_screen.set_vault(database.DB_PATH)
        self.login_screen.reset()
        self._session_timer.stop()
        self._start_shutdown_timer()

    # -----------------------------------------------------------------
    # Choosing, creating and switching vaults
    # -----------------------------------------------------------------
    def _open_vault_dialog(self):
        """Picks an existing vault and opens it."""
        path, _ = QFileDialog.getOpenFileName(
            self, 'Open Vault', str(self._vault_browse_dir()),
            f'AccountVault (*{database.VAULT_SUFFIX});;All Files (*)'
        )
        if path:
            self._open_vault_path(path)

    def _open_vault_path(self, path):
        """Opens a vault, having checked it really is one.

        The check is the point. is_first_launch means "no master hash in this
        database", so without it a file the user picked by mistake would be
        handed to the setup screen, and the next thing they did would be to put
        a master password on somebody else's spreadsheet-turned-database."""
        status = database.inspect_vault(path)
        if status != 'ok':
            self._show_chooser(vaultfile.describe(status, path))
            return

        version = database.format_of(path)
        if version is not None and version > auth.VAULT_FORMAT:
            self._show_chooser(vaultfile.describe('newer', path))
            return

        if database.DB_PATH is not None and vaultfile.same_file(path, database.DB_PATH):
            self._show_login()
            return

        self.close_vault()
        try:
            database.open_vault(path, create=False)
        except Exception as exc:
            self._show_chooser(f"{Path(path).name} could not be opened:\n{exc}")
            return

        vaultfile.remember(path)
        self._adopt_vault_settings()
        self.start()

    def _create_vault_dialog(self):
        """Names a new vault and creates it.

        getSaveFileName offers "overwrite?" as an ordinary confirmation, and the
        file behind that click could be a vault holding everything somebody has.
        So the answer is never taken: an existing path is turned into an offer
        to open it, and nothing here can truncate a file.
        """
        path, _ = QFileDialog.getSaveFileName(
            self, 'Create New Vault',
            str(self._vault_browse_dir() / vaultfile.suggested_name()),
            f'AccountVault (*{database.VAULT_SUFFIX})'
        )
        if not path:
            return

        path = Path(path)
        if not path.suffix:
            path = path.with_suffix(database.VAULT_SUFFIX)

        if path.exists():
            if database.inspect_vault(path) == 'ok':
                answer = QMessageBox.question(
                    self, 'Vault Already Exists',
                    f"{path.name} is already a vault.\n\nOpen it instead?",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
                )
                if answer == QMessageBox.Yes:
                    self._open_vault_path(path)
                return
            QMessageBox.warning(
                self, 'AccountVault',
                f"{path.name} already exists.\n\nChoose a name that is not in use — "
                f"creating a vault will not overwrite a file."
            )
            return

        self.close_vault()
        try:
            database.open_vault(path, create=True)
        except Exception as exc:
            self._show_chooser(f"{path.name} could not be created:\n{exc}")
            return

        vaultfile.remember(path)
        self._adopt_vault_settings()
        self.start()

    def _switch_vault(self):
        """Back to the chooser from the login screen."""
        self.close_vault()
        self._show_chooser()

    def _forget_vault(self, path):
        """Drops a vault from the recent list. The file is left where it is."""
        vaultfile.forget(path)
        self.chooser_screen.reset()

    def _clear_recent(self):
        vaultfile.clear()
        self.chooser_screen.reset()

    def _set_remembering(self, enabled: bool):
        """Turns the recent list on or off. Turning it off clears it too.

        No confirmation: what is lost is a list of paths, the vaults themselves
        are untouched, and the switch is one click away from being put back."""
        vaultfile.set_remembering(enabled)
        self.chooser_screen.reset()

    def close_vault(self):
        """Closes the open vault, leaving nothing of it behind.

        The same sequence closeEvent runs, minus the quitting. Locking is what
        does most of the work — it releases the key, which fires the listeners
        that drop the decrypted account cache — and the rest is the per-vault
        state that had no reason to be reset while the vault could never change:

          * the in-memory lockout counters, or vault A's failed logins would
            follow the user into vault B and lock them out of a vault they have
            not touched;
          * the settings and the appearance they drive, which live in the
            vault's own app_config;
          * the window geometry, written back to the vault it came from before
            the path moves on.
        """
        if database.DB_PATH is None:
            return
        self._close_modal_dialogs()
        self._save_window_state()
        auth.lock_app()
        self._discard_form()
        self._clipboard_timer.stop()
        self._clear_clipboard()
        self._session_timer.stop()
        self._shutdown_timer.stop()

        self.accounts = []
        self.selected_id = None
        self.selected_category = None
        self._drop_undo()
        self._hide_dashboard()
        self.detail_view.clear()
        self.account_list.clear()
        self.lock_chip.hide()
        self.clipboard_chip.hide()

        auth._lockout_deadline_monotonic = 0.0
        auth._lockout_count = 0

        database.close_vault()

    def _adopt_vault_settings(self):
        """Applies the newly opened vault's own look before anything is drawn.

        Theme and font size are per-vault, so a switch that skipped this would
        leave the incoming vault wearing the outgoing one's appearance until
        something happened to reload it."""
        self.settings = preferences.get_settings()
        self.apply_appearance(self.settings['theme'], self.settings['font_size'])
        self._restore_geometry()

    @staticmethod
    def _vault_browse_dir() -> Path:
        """Where a vault file dialog opens.

        Beside the current vault when there is one, since a second vault most
        often lives with the first. Documents otherwise — never the working
        directory, which for a shortcut launch is the application folder."""
        if database.DB_PATH is not None:
            return Path(database.DB_PATH).parent
        return Path(export_save_path('')) or Path.home()

    # -----------------------------------------------------------------
    # Unlocking and locking
    # -----------------------------------------------------------------
    def _handle_setup(self, password: str):
        if self._attempt(auth.setup_master_password, password,
                         report=self.setup_screen.show_error) is _FAILED:
            return
        self.setup_screen.reset()
        self._enter_vault('Master password created.')

    def _handle_login(self, password: str):
        # No rate-limit check here any more: verify_master_password enforces it
        # itself and records the outcome, so there is one implementation of the
        # rule rather than one per caller.
        try:
            verified = auth.verify_master_password(password)
        except auth.LoginRateLimited as exc:
            self.login_screen.show_error(str(exc))
            self.login_screen.password.clear()
            return
        except auth.VaultConfigError as exc:
            # Not a wrong password, and it must not be reported as one: no
            # password will ever open this vault, so 'Invalid password' would
            # leave the user retyping a correct one indefinitely.
            self.login_screen.show_error(str(exc))
            self.login_screen.password.clear()
            return

        if not verified:
            self.login_screen.show_error('Invalid password.')
            self.login_screen.password.clear()
            return

        self.login_screen.reset()
        self._enter_vault('Unlocked.')

    def _enter_vault(self, message: str):
        self._shutdown_timer.stop()
        self.settings = preferences.get_settings()
        self.apply_appearance(self.settings['theme'], self.settings['font_size'])

        self.current_category = ALL
        self._apply_saved_sort()
        self._warned_of_lock = False
        self._backup_checked_on = datetime.date.today()
        # Refit for whatever this vault holds; the screen must be up first so
        # the splitter has a real width to divide.
        self._sidebar_fitted = False

        self.screens.setCurrentWidget(self.main_screen)
        self._session_timer.start(_SESSION_TICK_MS)
        self.load_category(ALL)
        self.show_toast(message)

        # Last, with the vault open and the window up. A backup is a copy of
        # the file and needs no key, but tying it to the unlock keeps two
        # properties worth having: nothing is written before somebody has
        # proved they can open this vault, and a vault nobody opens does not
        # spend its retention window on copies of a state that never changed.
        self._run_scheduled_backup()

    def lock_vault(self):
        self._close_modal_dialogs()
        auth.lock_app()
        self._discard_form()
        self.accounts = []
        self.selected_id = None
        self.selected_category = None
        self._drop_undo()
        self._hide_dashboard()
        self.detail_view.clear()
        self.account_list.clear()
        # A copied secret must not outlive the vault it came from. This used to
        # be missing entirely while the comment below claimed it had run: the
        # chip was hidden, so the countdown and the click-to-clear both vanished
        # from the nav bar, and the password stayed on the clipboard with
        # nothing on screen saying so. Stopping the timer first so the wipe
        # happens now rather than at whatever remained of its interval.
        self._clipboard_timer.stop()
        self._clear_clipboard()
        # Neither countdown means anything on the login screen: the session is
        # already over, and the clipboard has just been emptied.
        self.lock_chip.hide()
        self.clipboard_chip.hide()
        self._show_login()
        self.show_toast('Locked.')

    def _close_modal_dialogs(self):
        """Dismisses any open modal dialog before locking, so the login screen
        is not swapped in behind one still showing decrypted data.

        Rejects first: the caller blocked in exec() then takes its cancel path
        instead of acting on a half-finished edit. A dialog that declines to go
        that way is closed, and then hidden outright — an override may refuse a
        reject, but decrypted values must not survive the lock either way.
        Nested modals unwind one per pass, and the loop is bounded so a widget
        that survives all three cannot spin here. Native dialogs (the file
        pickers) are outside Qt's modal stack and are not reached."""
        for _ in range(10):
            modal = QApplication.activeModalWidget()
            if modal is None:
                return
            if isinstance(modal, QDialog):
                modal.reject()
            if QApplication.activeModalWidget() is modal:
                modal.close()
            if QApplication.activeModalWidget() is modal:
                modal.hide()
            if QApplication.activeModalWidget() is modal:
                return

    def _drop_undo(self):
        """Forgets a deleted account that could still have been restored.

        Called from both teardowns, and it is the lock that makes it necessary
        rather than tidiness. A restore token is rows still encrypted under the
        key that was live when they were deleted; offered after a lock, it
        would be restored into whatever vault is open next, writing rows no key
        there can open — a whole account rendered unreadable by an Undo button
        that looked like it was doing the opposite.

        The toast is dismissed with it, so the button goes at the same moment
        the thing behind it does."""
        self._undo_delete = None
        self.toast.dismiss()

    def _handle_vault_locked(self):
        """Called when an operation discovers the vault locked underneath it."""
        self._session_timer.stop()
        self.lock_vault()

    # -----------------------------------------------------------------
    # The session clock
    # -----------------------------------------------------------------
    def _check_session(self):
        """Idle-lock tick: warns as the timeout approaches, then locks.

        Runs once a second now rather than every ten, because it also drives the
        two nav countdowns. The work is a subtraction and a couple of label
        writes; the lock itself also becomes correspondingly more punctual."""
        if not auth.check_session():
            self._session_timer.stop()
            self.lock_vault()
            self.login_screen.show_error('Locked due to inactivity.')
            return

        remaining = auth.seconds_until_lock()
        self._update_lock_chip(remaining)
        self._update_clipboard_chip()
        self._check_backup_rollover()

        if remaining <= _WARN_BEFORE_SECONDS and not self._warned_of_lock:
            self._warned_of_lock = True
            minutes = max(1, round(remaining / 60))
            # Kept alongside the chip rather than replaced by it. The toast is
            # what catches the eye once; the chip is what stays put afterwards.
            self.show_toast(
                f"Locking in {minutes} minute{'s' if minutes != 1 else ''} due to inactivity.",
                6000,
            )

    def _update_lock_chip(self, remaining: float):
        """Shows a live countdown over the last two minutes of the idle window.

        Below the warning threshold it hides again, which happens the moment the
        user does anything at all — the activity filter resets the timer, so the
        chip disappearing is itself the confirmation that the session was
        extended."""
        if remaining > _WARN_BEFORE_SECONDS:
            self.lock_chip.hide()
            return

        seconds = max(0, int(remaining))
        self.lock_chip.update_text(
            f"Locks in {seconds // 60}:{seconds % 60:02d}",
            'Locking soon due to inactivity. Click to stay unlocked.',
        )
        # Amber only for the last thirty seconds, so the chip is informative for
        # most of its life and urgent only when it is nearly out of time.
        self.lock_chip.set_role('warn' if seconds <= 30 else 'subtle')
        self.lock_chip.show()

    def _extend_session(self):
        """Click-to-extend on the lock chip.

        The activity filter would catch the click anyway; doing it explicitly is
        what lets the chip say so out loud rather than just vanishing."""
        self.record_activity()
        self._update_lock_chip(auth.seconds_until_lock())
        self.show_toast('Still here — session extended.')

    def record_activity(self):
        auth.record_activity()
        self._warned_of_lock = False

    def _start_shutdown_timer(self):
        minutes = self.settings.get('shutdown_after_lock_minutes', 60)
        if minutes:
            self._shutdown_timer.start(minutes * 60 * 1000)

    # -----------------------------------------------------------------
    # Clipboard
    # -----------------------------------------------------------------
    def copy_to_clipboard(self, value: str, label: str):
        if not value:
            self.show_toast(f"No {label.lower()} to copy.")
            return

        QGuiApplication.clipboard().setMimeData(_secret_mime(value))
        self._copied_value = value

        seconds = self.settings.get('clipboard_clear_seconds', 30)
        self._clipboard_timer.start(seconds * 1000)
        # Straight away rather than on the next tick, so the chip appears with
        # the toast instead of up to a second behind it.
        self._update_clipboard_chip()
        self.show_toast(f"{label} copied. Clears in {seconds}s.")

    def _clear_clipboard(self):
        """Only wipes the clipboard if it still holds what we put there."""
        clipboard = QGuiApplication.clipboard()
        if self._copied_value is not None and clipboard.text() == self._copied_value:
            clipboard.clear()
        self._copied_value = None

    def _clear_clipboard_now(self):
        self._clipboard_timer.stop()
        self._clear_clipboard()
        self._update_clipboard_chip()
        self.show_toast('Clipboard cleared.')

    def _update_clipboard_chip(self):
        """Counts down the clipboard auto-clear while a secret is on it."""
        if self._copied_value is None or not self._clipboard_timer.isActive():
            self.clipboard_chip.hide()
            return

        seconds = max(0, self._clipboard_timer.remainingTime() // 1000)
        self.clipboard_chip.update_text(
            f"Clipboard {seconds}s",
            'A copied secret is on the clipboard. Click to clear it now.',
        )
        self.clipboard_chip.show()

    # -----------------------------------------------------------------
    # Vault-level actions
    # -----------------------------------------------------------------
    def _save_copy(self):
        """Writes a copy of the vault wherever the user asks.

        The copy is the vault, so it opens with the same master password — and
        only with the one this vault holds right now. A copy taken before a
        password change still wants the old password afterwards, which is the
        one thing the old encrypted-backup format did better and the reason
        _change_password offers a fresh copy on the spot.
        """
        suggested = backups_service.backup_name()
        path, _ = QFileDialog.getSaveFileName(
            self, 'Save a Copy',
            str(backups_service.default_dir() / suggested),
            f'AccountVault (*{database.VAULT_SUFFIX})'
        )
        if not path:
            return

        path = Path(path)
        if not path.suffix:
            path = path.with_suffix(database.VAULT_SUFFIX)
        if vaultfile.same_file(path, database.DB_PATH):
            self.show_toast('That is the vault you are working in — choose another name.', 4000)
            return

        try:
            backups_service.copy_vault(path)
        except backups_service.BackupError as exc:
            self.show_toast(f"Copy failed: {exc}", 5000)
            return

        self.show_toast(f"Copied to {path.name}. It opens with this vault's master password.", 6000)

    def _move_vault(self):
        """Moves the vault to another folder and carries on working there.

        Copy, verify, switch, and then say where the original still is. The
        original is deliberately not deleted: this is the only vault the user
        has until the moment the copy is proved good, and an app that removes
        the old one on their behalf is an app that can be wrong about it once.
        """
        path, _ = QFileDialog.getSaveFileName(
            self, 'Move Vault To',
            str(self._vault_browse_dir() / Path(database.DB_PATH).name),
            f'AccountVault (*{database.VAULT_SUFFIX})'
        )
        if not path:
            return

        path = Path(path)
        if not path.suffix:
            path = path.with_suffix(database.VAULT_SUFFIX)
        if vaultfile.same_file(path, database.DB_PATH):
            return
        if path.exists():
            QMessageBox.warning(
                self, 'AccountVault',
                f"{path.name} already exists.\n\nChoose a name that is not in use — "
                f"moving a vault will not overwrite a file."
            )
            return

        origin = Path(database.DB_PATH)
        try:
            backups_service.copy_vault(path)
        except backups_service.BackupError as exc:
            self.show_toast(f"Move failed: {exc}", 5000)
            return

        # Only once the copy exists and has been verified as a vault by
        # copy_vault. Until this line the app is still working in the original,
        # so a failure above costs nothing at all.
        self.close_vault()
        try:
            database.open_vault(path, create=False)
        except Exception as exc:
            self._show_chooser(f"The vault was copied to {path}, but could not be "
                               f"opened there:\n{exc}")
            return

        # The old entry is replaced rather than left alongside the new one:
        # otherwise the list offers both, and the abandoned file is a perfectly
        # valid vault holding a snapshot that stops the moment it was left.
        vaultfile.forget(origin)
        vaultfile.remember(path)
        self._adopt_vault_settings()
        self.start()
        QMessageBox.information(
            self, 'Vault Moved',
            f"You are now working in:\n{path}\n\n"
            f"The original is still at:\n{origin}\n\n"
            f"Nothing was deleted. Once you are satisfied the move worked, "
            f"delete the original yourself — it holds a copy of everything."
        )

    def _open_from_menu(self):
        """Open Vault… from inside an unlocked vault.

        Locks first, and the lock is the teardown: it drops the key, which fires
        the listeners that drop every decrypted cache. Going through the chooser
        rather than straight to a file dialog also means the recent list is
        reachable from in here, which is where somebody switching between two
        vaults actually wants it."""
        self._switch_vault()

    def _create_from_menu(self):
        self.close_vault()
        self._show_chooser()
        self._create_vault_dialog()

    def _import_accounts(self):
        """Reads a table of accounts into this vault.

        The other half of Export CSV, and deliberately next to it in the menu.
        The dialog does the parsing and the preview; this supplies what it
        needs to judge a row — everything the vault already holds, so a file
        imported twice adds nothing the second time — and writes the result.

        The write is one transaction (see create_accounts), so a file either
        lands whole or not at all. Landing on ALL afterwards rather than
        reloading the current view: the rows that just arrived are spread
        across whatever categories the file named, and a category tab is the
        one place most of them would not be visible.
        """
        existing = self._attempt(accounts_service.list_accounts,
                                 prefix='Failed to read this vault')
        if existing is _FAILED:
            return

        dialog = dialogs.ImportAccountsDialog(self, existing, self._known_categories())
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        written = self._attempt(accounts_service.create_accounts, dialog.rows(),
                                prefix='Import failed', dwell=6000)
        if written is _FAILED:
            return

        self.show_toast(
            f"{written} account{'s' if written != 1 else ''} imported. "
            f"The file they came from is still plaintext — delete it.", 7000)
        self.load_category(ALL)

    def _export_csv(self):
        if dialogs.CsvWarningDialog(self).exec() != dialogs.CsvWarningDialog.Accepted:
            return

        # Counted before the file dialog so 'no accounts to export' is answered
        # without making the user pick a path first, and counted through
        # count_accounts so that answer costs a COUNT(*) rather than decrypting
        # six fields and a JSON blob per row to take a len(). The accounts
        # themselves are still not decrypted until after the dialog — see below.
        count = self._attempt(accounts_service.count_accounts,
                              prefix='Failed to load accounts')
        if count is _FAILED:
            return
        if not count:
            self.show_toast('No accounts to export.')
            return

        path, _ = QFileDialog.getSaveFileName(
            self, 'Export CSV', export_save_path(csv_export.default_csv_name()),
            'CSV (*.csv)'
        )
        if not path:
            return

        # Rendered *after* the file dialog, not before it. A native file picker
        # is outside Qt's modal stack, so _close_modal_dialogs cannot reach one
        # and the idle lock can fire while it is open. Building the CSV first
        # meant every password in the vault sat decrypted in a local for as long
        # as the dialog stayed up, and was then written to disk after the vault
        # had locked. Decrypting here puts the work behind require_session, so a
        # lock during the dialog raises instead of writing.
        document = self._attempt(
            lambda: csv_export.export_csv(accounts_service.list_accounts()),
            prefix='Export failed',
        )
        if document is _FAILED:
            return

        try:
            restricted = export_io.write_export(path, document)
        except OSError as exc:
            self.show_toast(f"Export failed: {exc}")
            return

        self.show_toast('CSV exported.' if restricted else
                        'CSV exported, but its permissions could not be restricted — '
                        'anyone who can read that folder can read the file.', 6000)

    def _change_password(self):
        dialog = dialogs.ChangePasswordDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        try:
            changed = auth.change_master_password(dialog.current_password(), dialog.new_password())
        except auth.LoginRateLimited as exc:
            self.show_toast(str(exc))
            return
        except VaultLockedError:
            self._handle_vault_locked()
            return
        except Exception as exc:
            self.show_toast(f"Failed to change password: {exc}", 4000)
            return

        if not changed:
            self.show_toast('Current password is incorrect.')
            return

        self.show_toast('Master password changed successfully.')
        self._offer_copy_after_rekey()

    def _offer_copy_after_rekey(self):
        """Offers a fresh copy immediately after the password changes.

        This is the sharp edge of backups being copies. Re-encryption happens in
        the live vault only, so every copy that already exists is frozen under
        the old password — forever. Somebody who changes their password because
        they want to stop using the old one, and whose disk dies months later,
        finds that the only thing that opens their backups is the password they
        deliberately abandoned.

        Asked here, in the flow, rather than written down in a warning: the
        window where this is cheap to fix is about ten seconds wide, and it
        closes the moment the user moves on to something else.
        """
        answer = QMessageBox.question(
            self, 'Update Your Backups',
            "Copies of this vault made before now still open only with your "
            "previous master password — changing it re-encrypts this vault, not "
            "the copies.\n\nSave a fresh copy now?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
        )
        if answer == QMessageBox.Yes:
            self._save_copy()

    def _open_settings(self):
        dialog = dialogs.SettingsDialog(self, self.settings)
        dialog.preview_requested.connect(self.apply_appearance)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        values = dialog.values()
        if not self._confirm_retention_change(values):
            self._apply_saved_appearance()
            return

        try:
            preferences.save_settings(**values)
            auth.apply_saved_settings()
        except Exception as exc:
            self.show_toast(str(exc))
            self._apply_saved_appearance()
            return

        self.settings = preferences.get_settings()
        self._apply_saved_appearance()
        self.show_toast('Settings saved.')

    def _confirm_retention_change(self, values: dict) -> bool:
        """Asks before a shortened retention window starts deleting backups.

        Nothing is deleted at this moment — pruning only ever runs after a
        successful new backup — but the number is worth putting in front of
        somebody: going from two years to one month is a decision about roughly
        twenty files, and "how many" is not a question the combo box answers.
        """
        new = values.get('backup_retention_months')
        old = self.settings.get('backup_retention_months', 12)
        # 0 is "keep forever", so it is never a reduction whatever it replaces.
        if not new or (old and new >= old):
            return True

        state = preferences.get_backup_state()
        doomed = backups_service.expired_count(state.get('files', []), new)
        if not doomed:
            return True

        answer = QMessageBox.question(
            self, 'Shorten Backup History',
            f"{doomed} existing backup files are older than that.\n\n"
            f"They will be deleted the next time a backup runs. The most recent "
            f"backup is always kept.\n\nContinue?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        return answer == QMessageBox.Yes

    # -----------------------------------------------------------------
    # Scheduled backups
    # -----------------------------------------------------------------
    def _run_scheduled_backup(self):
        """Takes the scheduled backup if one is due.

        Reports both outcomes and blocks neither. A backup that has been failing
        quietly for four months is worse than not having one, so a failure gets
        a toast that dwells — but it is still only a toast, because the user
        came here to open their vault."""
        if not self._confirm_backup_destination():
            return

        result = backups_service.run_if_due(self.settings)
        if result is None:
            return
        if result.get('reset'):
            # Not a toast. The user set backups up and they are now off, which
            # is a thing to be told plainly rather than in something that fades.
            self.settings = preferences.get_settings()
            QMessageBox.warning(self, 'Backup Settings Reset', result['message'])
            return
        if not result['ok']:
            self.show_toast(f"Automatic backup failed: {result['message']}", 8000)
            return

        pruned = result['pruned']
        note = (f" {pruned} older backup{'s' if pruned != 1 else ''} removed."
                if pruned else '')
        # The folder, not just the filename. Where the vault is being copied to
        # is the part worth seeing: a destination that is not what the user
        # expects is invisible if the message only ever names the file.
        self.show_toast(
            f"Backup saved to {Path(result['path']).parent}: "
            f"{Path(result['path']).name}.{note}", 4000)
        if not result.get('hardened', True):
            self.show_toast(
                "That folder cannot restrict the backup to your account — "
                "anyone using this machine, or that share, can read it.", 8000)

    def _confirm_backup_destination(self) -> bool:
        """Asks once before backups start going somewhere the user chose.

        True to carry on. The default folder beside the vault is never asked
        about; a folder the user has already confirmed is not asked about
        again. Saying no turns automatic backups off rather than merely skipping
        this run, because the alternative is being asked at every unlock.

        The confirmation is recorded under the seal, so it answers "did the user
        agree to this folder" rather than "does the file say they did"."""
        if not self.settings.get('auto_backup_enabled'):
            return True
        folder = backups_service.resolve_dir(self.settings)
        if (backups_service.is_default_dir(self.settings, folder)
                or backups_service.destination_confirmed(folder)):
            return True

        warning = backups_service.destination_warning(folder)
        answer = QMessageBox.question(
            self, 'Confirm Backup Folder',
            f"Automatic backups will copy this vault to:\n\n{folder}\n\n"
            + (warning + "\n\n" if warning else "")
            + "Start backing up to this folder?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
        )
        if answer == QMessageBox.Yes:
            preferences.save_backup_confirmed_dir(str(folder))
            return True

        preferences.save_settings(**{**self.settings, 'auto_backup_enabled': 0})
        self.settings = preferences.get_settings()
        self.show_toast('Automatic backups turned off.', 4000)
        return False

    def _check_backup_rollover(self):
        """Catches the day turning over inside a session that never locks.

        Auto-shutdown closes the app an hour after a lock by default, so most
        sessions end long before this matters — but 'Never' is a setting somebody
        chooses, and without this their vault would go weeks between backups
        while the app sat open in front of them.

        Runs off the session tick, which is a second: everything before the date
        comparison is a dictionary lookup, and the comparison itself only does
        work on the one tick a day where the answer changes."""
        if not self.settings.get('auto_backup_enabled'):
            return
        today = datetime.date.today()
        if today == self._backup_checked_on:
            return
        self._backup_checked_on = today
        self._run_scheduled_backup()


class _ActivityFilter(QObject):
    """Feeds user input to the idle timer. Watching the application means one
    filter covers every widget, including dialogs.

    `window` is the MainWindow, untyped because that class now lives in the
    module that imports this one — an annotation here would be an import cycle
    for the sake of a hint."""

    _EVENTS = {
        QEvent.MouseMove, QEvent.MouseButtonPress, QEvent.KeyPress,
        QEvent.Wheel, QEvent.TouchBegin,
    }

    def __init__(self, window):
        super().__init__(window)
        self._window = window

    def eventFilter(self, obj, event):
        if event.type() in self._EVENTS:
            self._window.record_activity()
        return False

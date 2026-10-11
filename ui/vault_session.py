# -----------------------------------------------------------------------------
# ui/vault_session.py
# Which vault is open, whether it is unlocked, and everything on a clock.
#
# The screens before the browser (chooser, setup, login), opening, creating and
# moving vaults, the unlock and the lock, the idle timer and its countdowns, the
# Windows session lock, the clipboard and its auto-clear, the scheduled backup,
# and the vault-level entries in the Manage menu.
#
# They share one invariant: either a key is held or it is not. close_vault and
# lock_vault are the two places that list the teardown, and per-vault state not
# dropped in both follows the user into the next vault.
#
# See ui/actions.py for why these are mixins. What this one owns:
# self.settings, the four timers, self._copied_value, self._warned_of_lock,
# self._backup_checked_on.
# -----------------------------------------------------------------------------

import datetime
import os
from pathlib import Path

from PySide6.QtCore import QEvent, QMimeData, QObject, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QApplication, QDialog, QFileDialog, QMessageBox,
)

from core import auth
from core import database
from core import preferences
from core import vaultfile
from core.encryption import VaultLockedError
from services import accounts as accounts_service
from services import backups as backups_service
from services import csv_export
from services import export_io
from ui import dialogs
from ui.account_list import ALL
from ui.actions import _FAILED
from ui.widgets import export_save_path, plain_text

# How often the idle timer checks the session, and how long before the lock the
# user gets a warning. A second, because it also drives the nav countdowns.
_SESSION_TICK_MS = 1_000
_WARN_BEFORE_SECONDS = 120

# Windows tells a registered window when the session changes; a lock (Win+L, a
# locking screen saver, switching user) arrives as this message and code.
_WM_WTSSESSION_CHANGE = 0x02B1
_WTS_SESSION_LOCK = 0x7
_NOTIFY_FOR_THIS_SESSION = 0


def _secret_mime(value: str) -> QMimeData:
    """Clipboard payload for a secret, opted out of Windows clipboard history
    and cloud sync, which the auto-clear timer cannot reach.

    The two boolean formats want a little-endian DWORD 0; Windows treats a
    missing or malformed value as "allowed". Other platforms ignore them."""
    mime = QMimeData()
    mime.setText(value)
    mime.setData('ExcludeClipboardContentFromMonitorProcessing', b'\x00\x00\x00\x00')
    mime.setData('CanIncludeInClipboardHistory', b'\x00\x00\x00\x00')
    mime.setData('CanUploadToCloudClipboard', b'\x00\x00\x00\x00')
    return mime


def watch_session_lock(hwnd: int) -> bool:
    """Asks Windows to tell this window when the session locks. False off
    Windows, or where the request is refused."""
    if os.name != 'nt':
        return False
    try:
        import ctypes
        return bool(ctypes.windll.wtsapi32.WTSRegisterSessionNotification(
            hwnd, _NOTIFY_FOR_THIS_SESSION))
    except (AttributeError, OSError):
        return False


def unwatch_session_lock(hwnd: int):
    if os.name != 'nt':
        return
    try:
        import ctypes
        ctypes.windll.wtsapi32.WTSUnRegisterSessionNotification(hwnd)
    except (AttributeError, OSError):
        pass


def is_session_lock(event_type, message) -> bool:
    """Whether a native event is Windows reporting that the session locked."""
    if os.name != 'nt' or bytes(event_type) != b'windows_generic_MSG':
        return False
    try:
        import ctypes.wintypes
        msg = ctypes.wintypes.MSG.from_address(int(message))
    except (TypeError, ValueError, OSError):
        return False
    return msg.message == _WM_WTSSESSION_CHANGE and msg.wParam == _WTS_SESSION_LOCK


class VaultSessionMixin:
    """The vault lifecycle: which one is open, whether it is unlocked, and the
    timers that end that state."""

    # -----------------------------------------------------------------
    # Choosing which screen is up
    # -----------------------------------------------------------------
    def start(self, notice: str = ''):
        """Chooses the opening screen: the chooser when no vault is open (with
        `notice` saying why, if there is a why), setup for a vault created but
        never given a password, and otherwise the login."""
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
        """Opens a vault, having checked it really is one — otherwise a file
        picked by mistake would go to the setup screen and get a master
        password put on it."""
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

    def _new_vault_path(self, title: str, suggested: str) -> Path | None:
        """Asks where a new vault goes. None if the user cancelled or picked a
        file already there.

        Nothing here can truncate a file. Picking one that exists says so, and
        that deleting it is left to the user; an existing vault is also offered
        for opening. The save dialog's own "replace it?" is switched off, since
        answering Yes to it only led to a message saying no."""
        path, _ = QFileDialog.getSaveFileName(
            self, title, str(self._vault_browse_dir() / suggested),
            f'AccountVault (*{database.VAULT_SUFFIX})',
            options=QFileDialog.Option.DontConfirmOverwrite,
        )
        if not path:
            return None

        path = Path(path)
        if not path.suffix:
            path = path.with_suffix(database.VAULT_SUFFIX)

        if path.exists():
            refusal = (f"{path.name} already exists.\n\n"
                       f"For safety, AccountVault never overwrites an existing file. "
                       f"A vault may be the only copy of what it holds, and a "
                       f"replaced one cannot be got back. To use this name, delete "
                       f"the file yourself first, or choose a different name.")
            if database.inspect_vault(path) == 'ok':
                answer = QMessageBox.question(
                    self, 'Vault Already Exists',
                    plain_text(f"{refusal}\n\nOpen the existing vault instead?"),
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
                )
                if answer == QMessageBox.Yes:
                    self._open_vault_path(path)
                return None
            QMessageBox.warning(self, 'File Already Exists', plain_text(refusal))
            return None
        return path

    def _create_vault_dialog(self):
        """Names a new vault and creates it."""
        path = self._new_vault_path('Create New Vault', vaultfile.suggested_name())
        if path is None:
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

    def _create_demo_vault_dialog(self):
        """Builds a vault full of made-up accounts, shows its password, and
        lands on the login screen for it.

        Locked straight after the build. Setting the password hands back the
        key, and going in on that key would open the vault without anyone
        having typed the password, which is not how a vault opens."""
        # Here rather than at the top: only this button needs a few hundred
        # lines of fictional household.
        from tools import make_demo_vault

        path = self._new_vault_path('Create Demo Vault', make_demo_vault.DEFAULT_PATH.name)
        if path is None:
            return

        self.close_vault()
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            summary = make_demo_vault.build_vault(path)
        except Exception as exc:
            # A build that got as far as opening the file leaves it open, and
            # unlocked if it got as far as the password.
            existed = path.exists()
            self.close_vault()
            left = (f"\n\n{path.name} may be left half-built. Delete it before "
                    f"trying again." if existed else '')
            self._show_chooser(f"{path.name} could not be built:\n{exc}{left}")
            return
        finally:
            QApplication.restoreOverrideCursor()

        auth.lock_app()
        vaultfile.remember(path)
        self._adopt_vault_settings()
        # A copy made here is left on the clipboard for the login screen, on
        # the usual auto-clear timer.
        dialog = dialogs.DemoVaultDialog(self, path.name,
                                         sum(summary['categories'].values()),
                                         make_demo_vault.PASSWORD)
        dialog.copy_requested.connect(self.copy_to_clipboard)
        dialog.exec()
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
        """Turns the recent list on or off; off also clears it."""
        vaultfile.set_remembering(enabled)
        self.chooser_screen.reset()

    def close_vault(self):
        """Closes the open vault, leaving nothing of it behind.

        Locking does most of the work — releasing the key fires the listeners
        that drop the decrypted caches. The rest is per-vault state: the window
        geometry (written back to the vault it came from), the in-memory lockout
        (so vault A's failed logins do not lock the user out of vault B), and
        the settings and appearance."""
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

        auth.reset_rate_limit_memory()
        database.close_vault()

    def _adopt_vault_settings(self):
        """Applies the newly opened vault's own look before anything is drawn —
        theme and font size are per vault."""
        self.settings = preferences.get_settings()
        self.apply_appearance(self.settings['theme'], self.settings['font_size'])
        self._restore_geometry()

    @staticmethod
    def _vault_browse_dir() -> Path:
        """Where a vault file dialog opens: beside the current vault, else
        Documents, else home — never the working directory, which for a
        shortcut launch is the application folder."""
        if database.DB_PATH is not None:
            return Path(database.DB_PATH).parent
        return Path(export_save_path('') or Path.home())

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
        # verify_master_password enforces the rate limit and records the outcome.
        try:
            verified = auth.verify_master_password(password)
        except auth.LoginRateLimited as exc:
            self.login_screen.show_error(str(exc))
            self.login_screen.password.clear()
            return
        except auth.VaultConfigError as exc:
            # Not a wrong password, and must not be reported as one: no password
            # will open this vault.
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
        # Refit for whatever this vault holds, once the screen is up.
        self._sidebar_fitted = False

        self.screens.setCurrentWidget(self.main_screen)
        self._session_timer.start(_SESSION_TICK_MS)
        self.load_category(ALL)
        self.show_toast(message)

        # Last, with the window up. Tied to the unlock so nothing is written
        # before somebody has proved they can open this vault.
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
        # A copied secret must not outlive the vault it came from. The timer
        # stops first so the wipe happens now.
        self._clipboard_timer.stop()
        self._clear_clipboard()
        self.lock_chip.hide()
        self.clipboard_chip.hide()
        self._show_login()
        self.show_toast('Locked.')

    def _on_session_locked(self):
        """Windows was locked, so the vault locks too: whoever unlocks the
        computer next should not find it open."""
        if not auth.is_unlocked():
            return
        self._session_timer.stop()
        self.lock_vault()
        self.login_screen.show_error('Locked because Windows was locked.')

    def _close_modal_dialogs(self):
        """Dismisses any open modal dialog before locking, so the login screen
        is not swapped in behind one still showing decrypted data.

        Rejects first, so the caller blocked in exec() takes its cancel path;
        then closes, then hides, for a dialog that refuses. Bounded, so a widget
        that survives all three cannot spin here. Native file pickers are
        outside Qt's modal stack and are not reached — see _export_csv."""
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
        """Forgets a deleted account that could still have been restored. The
        token is rows encrypted under this vault's key; restored into another
        vault, they would be rows nothing there can open."""
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
        """Idle-lock tick: warns as the timeout approaches, then locks."""
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
            self.show_toast(
                f"Locking in {minutes} minute{'s' if minutes != 1 else ''} due to inactivity.",
                6000,
            )

    def _update_lock_chip(self, remaining: float):
        """Shows a live countdown over the last two minutes of the idle window.
        Any activity resets the timer, so the chip vanishing is the confirmation
        the session was extended."""
        if remaining > _WARN_BEFORE_SECONDS:
            self.lock_chip.hide()
            return

        seconds = max(0, int(remaining))
        self.lock_chip.update_text(
            f"Locks in {seconds // 60}:{seconds % 60:02d}",
            'Locking soon due to inactivity. Click to stay unlocked.',
        )
        # Amber only for the last thirty seconds.
        self.lock_chip.set_role('warn' if seconds <= 30 else 'subtle')
        self.lock_chip.show()

    def _extend_session(self):
        """Click-to-extend on the lock chip."""
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
        """Writes a copy of the vault wherever the user asks. It opens with the
        master password this vault holds right now."""
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
        """Moves the vault to another folder and carries on working there: copy,
        verify, switch, then say where the original still is. The original is
        never deleted on the user's behalf."""
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
                plain_text(f"{path.name} already exists.\n\nChoose a name that is "
                           f"not in use — moving a vault will not overwrite a file."),
            )
            return

        origin = Path(database.DB_PATH)
        try:
            backups_service.copy_vault(path)
        except backups_service.BackupError as exc:
            self.show_toast(f"Move failed: {exc}", 5000)
            return

        # Only once copy_vault has verified the copy.
        self.close_vault()
        try:
            database.open_vault(path, create=False)
        except Exception as exc:
            self._show_chooser(f"The vault was copied to {path}, but could not be "
                               f"opened there:\n{exc}")
            return

        # Replaced rather than left alongside, so the list does not offer the
        # abandoned snapshot.
        vaultfile.forget(origin)
        vaultfile.remember(path)
        self._adopt_vault_settings()
        self.start()
        QMessageBox.information(
            self, 'Vault Moved',
            plain_text(
                f"You are now working in:\n{path}\n\n"
                f"The original is still at:\n{origin}\n\n"
                f"Nothing was deleted. Once you are satisfied the move worked, "
                f"delete the original yourself — it holds a copy of everything."
            ),
        )

    def _open_from_menu(self):
        """Open Vault… from inside an unlocked vault. Goes through the chooser,
        so the recent list is reachable."""
        self._switch_vault()

    def _create_from_menu(self):
        self.close_vault()
        self._show_chooser()
        self._create_vault_dialog()

    def _import_accounts(self):
        """Reads a table of accounts into this vault. Everything the vault holds
        is passed in, so a file imported twice adds nothing the second time.
        Lands on All afterwards, since the rows are spread across categories."""
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
        if dialogs.CsvWarningDialog(self).exec() != QDialog.DialogCode.Accepted:
            return

        # Counted before the file dialog, without decrypting anything.
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

        # Decrypted only after the file dialog closes. A native picker is outside
        # Qt's modal stack, so the vault can lock while it is open; reading here
        # puts the work behind require_session, which raises instead of writing.
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
        """Offers a fresh copy straight after the password changes. Every
        existing copy still opens only with the old password, so this is the
        moment to fix that cheaply."""
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

    def _open_warnings(self):
        dialog = dialogs.WarningsDialog(self, self._known_categories())
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        try:
            preferences.save_warning_settings(dialog.values())
            auth.apply_saved_settings()
        except VaultLockedError:
            self._handle_vault_locked()
            return
        except Exception as exc:
            self.show_toast(str(exc))
            return

        # Every colour in the list, the detail pane and Needs Attention may have
        # changed, and the selection may have left the list it was in.
        account_id, category = self.selected_id, self.selected_category
        self._reload_view()
        if account_id is not None and any(a['id'] == account_id for a in self.accounts):
            self._select_account(account_id, category)
        elif account_id is not None:
            self.selected_id = None
            self.selected_category = None
            self._show_placeholder()
        self.show_toast('Warnings saved.')

    def _confirm_retention_change(self, values: dict) -> bool:
        """Asks before a shortened retention window starts deleting backups.
        Nothing is deleted now — pruning follows the next successful backup —
        but how many files will go is worth showing."""
        new = values.get('backup_retention_months')
        old = self.settings.get('backup_retention_months', 12)
        # 0 is "keep forever", so it is never a reduction.
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
        """Takes the scheduled backup if one is due, reporting either outcome
        without blocking the unlock.

        The seal is checked before anything else. The backup rows can be written
        by anyone holding the vault file, so until the seal vouches for them
        they must not be acted on — or shown to the user as a folder to approve,
        which would let a forged destination be sealed by one click."""
        if backups_service.check_seal() == 'reset':
            self.settings = preferences.get_settings()
            # Not a toast: backups the user set up are now off.
            QMessageBox.warning(self, 'Backup Settings Reset',
                                backups_service.RESET_MESSAGE)
            return

        if not self._confirm_backup_destination():
            return

        result = backups_service.run_if_due(self.settings)
        if result is None:
            return
        if result.get('reset'):
            self.settings = preferences.get_settings()
            QMessageBox.warning(self, 'Backup Settings Reset', result['message'])
            return
        if not result['ok']:
            self.show_toast(f"Automatic backup failed: {result['message']}", 8000)
            return

        pruned = result['pruned']
        note = (f" {pruned} older backup{'s' if pruned != 1 else ''} removed."
                if pruned else '')
        # The folder as well as the name: an unexpected destination is invisible
        # if only the filename is shown.
        self.show_toast(
            f"Backup saved to {Path(result['path']).parent}: "
            f"{Path(result['path']).name}.{note}", 4000)
        if not result.get('hardened', True):
            self.show_toast(
                "That folder cannot restrict the backup to your account — "
                "anyone using this machine, or that share, can read it.", 8000)

    def _confirm_backup_destination(self) -> bool:
        """Asks once before backups start going somewhere the user chose. True
        to carry on. The default folder beside the vault is never asked about.

        Saying no turns automatic backups off, rather than asking again at every
        unlock."""
        if not self.settings.get('auto_backup_enabled'):
            return True
        folder = backups_service.resolve_dir(self.settings)
        if (backups_service.is_default_dir(self.settings, folder)
                or backups_service.destination_confirmed(folder)):
            return True

        warning = backups_service.destination_warning(folder)
        answer = QMessageBox.question(
            self, 'Confirm Backup Folder',
            plain_text(f"Automatic backups will copy this vault to:\n\n{folder}\n\n"
                       + (warning + "\n\n" if warning else "")
                       + "Start backing up to this folder?"),
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
        """Catches the day turning over inside a session that never locks, so a
        vault left open with auto-shutdown set to Never still gets its backups.
        Runs off the session tick; does work only when the date changes."""
        if not self.settings.get('auto_backup_enabled'):
            return
        today = datetime.date.today()
        if today == self._backup_checked_on:
            return
        self._backup_checked_on = today
        self._run_scheduled_backup()


class _ActivityFilter(QObject):
    """Feeds user input to the idle timer. Installed on the application, so it
    covers every widget, dialogs included."""

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

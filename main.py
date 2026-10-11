# -----------------------------------------------------------------------------
# main.py
# Entry point for AccountVault.
#
# A plain desktop application: no server, no listening socket, no browser.
#
# Launch resolves which vault to open before the window is built, but is not
# allowed to fail over it: a moved vault, or a first launch with none, opens the
# chooser screen rather than an error box and a dead process.
# -----------------------------------------------------------------------------

import datetime
import os
import sys
import traceback
from pathlib import Path

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from core import auth
from core import database
from core import vaultfile
from core.version import VERSION
from ui.main_window import MainWindow
from ui.widgets import plain_text

# Anchored to this file: AccountVault_Windows.bat, a shortcut and a shell all
# launch from somewhere different.
ICON_PATH = Path(__file__).resolve().parent / 'assets' / 'accountvault.ico'

# Windows groups taskbar buttons by AppUserModelID, and a script run through
# python.exe inherits Python's — the taskbar would show the Python icon.
APP_USER_MODEL_ID = 'AccountVault.Vault.2'

ERROR_LOG_NAME = 'errors.log'
# Past this the log is started afresh rather than growing without bound.
_ERROR_LOG_MAX_BYTES = 256 * 1024

_reporting_error = False


def error_log_path() -> Path:
    return database.app_data_dir() / ERROR_LOG_NAME


def log_error(exc_type, tb) -> Path | None:
    """Appends an unexpected error to the error log. Returns its path, or None
    if it could not be written.

    Records the exception type and the stack — file, line and function — and
    deliberately not the exception's message or any frame locals. Messages can
    carry values (float('hunter2') says "could not convert string to float:
    'hunter2'"), and this file sits outside the vault in plain text."""
    path = error_log_path()
    # Frames only — not format_tb, which also prints each frame's source line.
    stack = ''.join(f'  File "{frame.filename}", line {frame.lineno}, in {frame.name}\n'
                    for frame in traceback.extract_tb(tb)) if tb is not None else ''
    entry = (f"--- {datetime.datetime.now().isoformat(timespec='seconds')} "
             f"AccountVault {VERSION}\n{stack}{exc_type.__qualname__}\n")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        created = not path.exists()
        flags = os.O_CREAT | os.O_WRONLY
        if not created and path.stat().st_size > _ERROR_LOG_MAX_BYTES:
            flags |= os.O_TRUNC
        else:
            flags |= os.O_APPEND
        fd = os.open(path, flags, 0o600)
        with os.fdopen(fd, 'a', encoding='utf-8') as handle:
            handle.write(entry)
    except OSError:
        return None
    if created:
        database.restrict_to_owner(path)
    return path


def _report_error(exc_type, log_path):
    """Tells the user something failed. Without this an error under pythonw —
    which has no console — looks like a button that does nothing."""
    global _reporting_error
    if _reporting_error or QApplication.instance() is None:
        return
    _reporting_error = True
    try:
        where = (f"\n\nDetails were written to:\n{log_path}" if log_path
                 else '')
        QMessageBox.critical(
            QApplication.activeWindow(), 'AccountVault',
            plain_text(f"Something went wrong ({exc_type.__name__}), so the last "
                       f"action may not have finished.{where}"),
        )
    except Exception:
        pass
    finally:
        _reporting_error = False


def _install_excepthook():
    """Logs and reports unexpected errors, without frame locals.

    The master password is a live local in derive_key and
    verify_master_password; owning the hook keeps a richer traceback renderer
    from ever writing it out."""
    def handle(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        # pythonw.exe runs with sys.stderr set to None.
        if sys.stderr is not None:
            sys.stderr.write(''.join(traceback.format_exception(exc_type, exc, tb)))
        _report_error(exc_type, log_error(exc_type, tb))

    sys.excepthook = handle


def _warn_if_unhardened():
    """Warns when the vault file could not be restricted to the owner — most
    likely a USB stick or network share that cannot express permissions."""
    if database.permissions_hardened:
        return
    QMessageBox.warning(
        None, 'AccountVault',
        plain_text(
            "The vault file's permissions could not be restricted to your account, "
            "so other users of this machine may be able to read it.\n\n"
            "Your data is still encrypted. This is most likely because the vault is "
            "on a drive that cannot express file permissions, such as a USB stick or "
            "a network share.\n\n"
            f"Vault location: {database.DB_PATH}"
        ),
    )


def _claim_taskbar_identity():
    """Tells Windows this process is its own app. Must run before any window
    exists; cosmetic, so a failure never blocks a launch."""
    if os.name != 'nt':
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except (AttributeError, OSError):
        pass


def _argv_path():
    """A vault named on the command line. Anything beginning with a dash is a Qt
    switch, not a filename."""
    for argument in sys.argv[1:]:
        if argument and not argument.startswith('-'):
            return argument
    return None


def _open_startup_vault():
    """Opens the vault this launch is for. Returns a notice for the chooser
    screen if it could not.

    Never falls through to a *different* vault or to creating a new one: an
    empty vault where a full one used to be is indistinguishable from having
    lost everything."""
    path = vaultfile.startup_path(_argv_path())
    if path is None:
        return None

    status = vaultfile.probe(path)
    if status == 'ok':
        version = database.format_of(path)
        if version is not None and version > auth.VAULT_FORMAT:
            return vaultfile.describe('newer', path)
        try:
            database.open_vault(path, create=False)
        except Exception as exc:
            return f"{Path(path).name} could not be opened:\n{exc}"
        vaultfile.remember(path)
        return None

    return vaultfile.describe(status, path)


def main() -> int:
    _install_excepthook()
    _claim_taskbar_identity()

    app = QApplication(sys.argv)
    app.setApplicationName('AccountVault')
    app.setApplicationDisplayName('AccountVault')
    app.setApplicationVersion(VERSION)
    # On the application, not the window, so dialogs inherit it.
    if ICON_PATH.exists():
        app.setWindowIcon(QIcon(str(ICON_PATH)))

    # After QApplication, so anything it has to say can be shown.
    notice = _open_startup_vault()
    if database.DB_PATH is not None:
        _warn_if_unhardened()

    window = MainWindow()
    window.start(notice)
    window.show()

    return app.exec()


if __name__ == '__main__':
    sys.exit(main())

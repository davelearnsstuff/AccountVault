# -----------------------------------------------------------------------------
# main.py
# Entry point for AccountVault.
#
# The vault is a plain desktop application: no server, no listening socket, and
# no browser. Everything runs in this one process, and the decrypted data never
# leaves it.
#
# Launch resolves which vault to open before the window is built, but it is not
# allowed to fail over it: a vault that has been moved, or a first-ever launch
# with no vault at all, opens the chooser screen rather than an error box and a
# dead process.
# -----------------------------------------------------------------------------

import os
import sys
import traceback
from pathlib import Path

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

import auth
import database
import vaultfile
from ui.main_window import MainWindow

# Anchored to this file rather than the working directory: start.bat, a desktop
# shortcut and a shell all launch from somewhere different.
ICON_PATH = Path(__file__).resolve().parent / 'assets' / 'accountvault.ico'

# Windows groups taskbar buttons by AppUserModelID, not by window icon, and a
# script run through python.exe inherits Python's. Without this the icon is
# correct in the title bar and Alt-Tab but still shows the Python rocket on the
# taskbar.
APP_USER_MODEL_ID = 'AccountVault.Vault.2'


def _install_excepthook():
    """Prints tracebacks without frame locals.

    The master password is a live local in derive_key, ph.hash and
    verify_master_password. Python's own hook already omits locals; owning the
    hook keeps it that way, so a crash reporter or richer traceback renderer
    cannot start writing them out."""
    def handle(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        # start.bat launches the app through pythonw.exe, which runs with no
        # console attached — CPython leaves sys.stderr as None there. Writing to
        # it unguarded meant the hook installed to report a crash raised an
        # AttributeError of its own instead, losing the traceback entirely.
        stream = sys.stderr
        if stream is None:
            return
        stream.write(''.join(traceback.format_exception(exc_type, exc, tb)))

    sys.excepthook = handle


def _warn_if_unhardened():
    """Warns when the vault file could not be restricted to the owner.

    A warning rather than a refusal to start: the encryption is what protects
    the data, but a vault readable by everyone on a shared machine is worth
    saying out loud.

    Far more likely to fire now than when every vault lived in one directory
    the app controlled — a vault the user has put on a USB stick or a network
    share is a vault on a filesystem that often cannot express this at all."""
    if database.permissions_hardened:
        return
    QMessageBox.warning(
        None, 'AccountVault',
        "The vault file's permissions could not be restricted to your account, "
        "so other users of this machine may be able to read it.\n\n"
        "Your data is still encrypted. This is most likely because the vault is "
        "on a drive that cannot express file permissions, such as a USB stick or "
        "a network share.\n\n"
        f"Vault location: {database.DB_PATH}"
    )


def _claim_taskbar_identity():
    """Tells Windows this process is its own app. Must run before any window
    exists, and is cosmetic — a failure here is never worth blocking a launch."""
    if os.name != 'nt':
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except (AttributeError, OSError):
        pass


def _argv_path():
    """A vault named on the command line.

    Anything beginning with a dash is left alone: Qt reads its own switches out
    of argv, and taking one of those for a filename would send the app looking
    for a vault called '-style'."""
    for argument in sys.argv[1:]:
        if argument and not argument.startswith('-'):
            return argument
    return None


def _open_startup_vault():
    """Opens the vault this launch is for. Returns a notice if it could not.

    The notice is text for the chooser screen, not an error: every path through
    here that fails still leaves the user somewhere they can pick a vault. The
    one thing it must never do is fall through to a *different* vault, or to
    creating a new one — from the outside, opening an empty vault where a full
    one used to be is indistinguishable from having lost everything.
    """
    path = vaultfile.startup_path(_argv_path())
    if path is None:
        return None

    status = database.inspect_vault(path)
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
    # On the application, not the window, so dialogs inherit it. QIcon accepts a
    # bad path silently and renders nothing, hence the existence check.
    if ICON_PATH.exists():
        app.setWindowIcon(QIcon(str(ICON_PATH)))
    # The window is the process, and the auto-shutdown timer ends the app by
    # calling MainWindow.close. closeEvent saves state, locks the vault and
    # clears the clipboard synchronously, so nothing races the quit.
    app.setQuitOnLastWindowClosed(True)

    # After QApplication so anything it has to say has somewhere to be shown;
    # run earlier it could only reach stderr, which from a shortcut looks like
    # nothing happening at all.
    notice = _open_startup_vault()
    if database.DB_PATH is not None:
        _warn_if_unhardened()

    window = MainWindow()
    window.start(notice)
    window.show()

    return app.exec()


if __name__ == '__main__':
    sys.exit(main())

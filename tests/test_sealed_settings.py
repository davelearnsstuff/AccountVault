# -----------------------------------------------------------------------------
# tests/test_sealed_settings.py
# The security settings are sealed with the backup rows.
#
# Settings are stored in the clear, so the window can be built before anyone
# logs in. Four of them decide how exposed an open vault is: the lock timeout,
# the clipboard clear, the shutdown after a lock, and screen capture. Anyone
# who can edit the file could weaken them, so they are sealed; a seal that no
# longer matches resets them, and until it has been checked they are not
# believed.
# -----------------------------------------------------------------------------

import pytest

from core import auth
from core import database
from core import encryption
from core import preferences
from conftest import MASTER, NEW_MASTER, _destroy
from services import backups as backups_service

WEAKER = {
    'lock_timeout_minutes': '60',
    'clipboard_clear_seconds': '120',
    'shutdown_after_lock_minutes': '0',
    'allow_screen_capture': '1',
}


def save(**changes):
    preferences.save_settings(**{**preferences.get_settings(), **changes})


def write_backup_only_seal():
    """The seal a vault carried before the security settings joined it."""
    token = encryption.encrypt(
        preferences._backup_fingerprint(preferences._BACKUP_SEALED_KEYS),
        preferences._backup_aad())
    database.write_config(preferences._BACKUP_AUTH_KEY, token)


@pytest.fixture
def window(vault, qt_app, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    from ui.main_window import MainWindow
    warned = []
    monkeypatch.setattr(QMessageBox, 'warning',
                        staticmethod(lambda parent, title, text: warned.append(title)))
    window = MainWindow()
    window.warned = warned
    yield window
    _destroy(window, qt_app)


# -- the seal ---------------------------------------------------------------------

def test_the_security_settings_are_sealed():
    assert set(WEAKER) == set(preferences.SECURITY_SETTING_KEYS)
    assert set(WEAKER) <= set(preferences._AUTHENTICATED_BACKUP_KEYS)


@pytest.mark.parametrize('key, value', WEAKER.items())
def test_a_weakened_setting_is_caught_and_reset(vault, key, value):
    backups_service.check_seal()
    database.write_config(key, value)

    assert backups_service.check_seal() == 'reset'
    assert preferences.get_settings()[key] == preferences.default_setting(key)
    assert backups_service.check_seal() == 'ok'


def test_saving_through_the_app_keeps_the_seal(vault):
    backups_service.check_seal()
    save(lock_timeout_minutes=60, allow_screen_capture=1)
    assert backups_service.check_seal() == 'ok'
    assert preferences.get_settings()['lock_timeout_minutes'] == 60


def test_a_seal_from_before_is_accepted_once_and_reissued(vault):
    """No false alarm on the first launch of this version: the user's backup
    settings and timeouts survive."""
    save(lock_timeout_minutes=30)
    write_backup_only_seal()
    assert preferences._seal_covers() == 'backup-only'

    assert backups_service.check_seal() == 'ok'

    assert preferences._seal_covers() == 'current'
    assert preferences.get_settings()['lock_timeout_minutes'] == 30


def test_a_re_key_reissues_a_seal_from_before_over_everything(vault):
    write_backup_only_seal()
    assert auth.change_master_password(MASTER, NEW_MASTER) is True
    assert preferences._seal_covers() == 'current'


def test_a_re_key_keeps_the_seal(vault):
    backups_service.check_seal()
    save(clipboard_clear_seconds=60)
    assert auth.change_master_password(MASTER, NEW_MASTER) is True
    assert backups_service.check_seal() == 'ok'


# -- the window -------------------------------------------------------------------

def test_unlocking_a_tampered_vault_resets_and_warns(window):
    backups_service.check_seal()
    database.write_config('lock_timeout_minutes', '60')

    window._enter_vault('test')

    assert window.settings['lock_timeout_minutes'] == 15
    assert auth._lock_timeout_seconds == 15 * 60
    assert backups_service.RESET_TITLE in window.warned


def test_screen_capture_is_never_allowed_on_the_login_screen(window):
    from ui import screen_capture
    save(allow_screen_capture=1)
    window._enter_vault('test')
    assert screen_capture.allowed() is True

    window.lock_vault()

    assert screen_capture.allowed() is False


def test_a_planted_choice_is_not_believed_before_unlock(window):
    """The window is built straight onto a vault nobody has unlocked yet."""
    from ui import screen_capture
    database.write_config('allow_screen_capture', '1')
    window.settings = preferences.get_settings()
    window._apply_capture_setting()
    assert screen_capture.allowed() is False


@pytest.mark.parametrize('stored, expected', [('0', 60), ('30', 30), ('60', 60),
                                              ('240', 60)])
def test_shutdown_before_unlock_is_no_laxer_than_the_default(window, stored, expected):
    database.write_config('shutdown_after_lock_minutes', stored)
    window.settings = preferences.get_settings()
    assert window._shutdown_minutes() == expected


def test_shutdown_after_a_session_uses_the_verified_choice(window):
    save(shutdown_after_lock_minutes=0)
    window._enter_vault('test')
    window.lock_vault()
    assert window._shutdown_minutes() == 0

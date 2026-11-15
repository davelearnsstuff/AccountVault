# -----------------------------------------------------------------------------
# tests/test_session_edges.py
# The ways an unlocked vault or a copied secret could outlast its welcome.
#
# The idle clock against a clock set back and a computer put to sleep, the
# system's own lock and sleep reaching the vault, and a copied secret on the
# way out of an exit that never closes the window.
# -----------------------------------------------------------------------------

import os
import time

import pytest

from core import auth
from conftest import _destroy

windows_only = pytest.mark.skipif(os.name != 'nt', reason='Windows messages')


@pytest.fixture
def window(vault, qt_app):
    from ui.main_window import MainWindow
    window = MainWindow()
    window._enter_vault('test')
    yield window
    _destroy(window, qt_app)


def last_activity(wall_ago: float, monotonic_ago: float):
    """Sets the last activity as each clock would report it, so many seconds
    back."""
    auth._last_activity = (time.time() - wall_ago, time.monotonic() - monotonic_ago)


# -- the idle clock ---------------------------------------------------------------

def test_recent_activity_keeps_the_vault_open(vault):
    last_activity(wall_ago=5, monotonic_ago=5)
    assert auth.check_session() is True


def test_setting_the_clock_back_does_not_stretch_a_session(vault):
    """The wall clock now says the last activity was an hour in the future;
    the monotonic clock still counts the real idle time."""
    timeout = auth._lock_timeout_seconds
    last_activity(wall_ago=-3600, monotonic_ago=timeout + 1)
    assert auth.check_session() is False


def test_a_night_asleep_still_counts(vault):
    """On Linux and macOS the monotonic clock stops while the computer sleeps;
    the wall clock does not."""
    timeout = auth._lock_timeout_seconds
    last_activity(wall_ago=timeout + 1, monotonic_ago=2)
    assert auth.check_session() is False


def test_the_countdown_follows_the_longer_clock(vault):
    timeout = auth._lock_timeout_seconds
    last_activity(wall_ago=10, monotonic_ago=100)
    assert timeout - 101 <= auth.seconds_until_lock() <= timeout - 99


# -- the system locking the vault -------------------------------------------------

@windows_only
def test_going_to_sleep_is_recognised():
    import ctypes
    import ctypes.wintypes

    from ui.vault_session import is_session_lock, is_suspend

    msg = ctypes.wintypes.MSG()
    msg.message = 0x0218       # WM_POWERBROADCAST
    msg.wParam = 0x4           # PBT_APMSUSPEND
    assert is_suspend(b'windows_generic_MSG', ctypes.addressof(msg))
    assert not is_session_lock(b'windows_generic_MSG', ctypes.addressof(msg))

    msg.wParam = 0x12          # PBT_APMRESUMEAUTOMATIC: waking, not sleeping
    assert not is_suspend(b'windows_generic_MSG', ctypes.addressof(msg))


def test_going_to_sleep_locks_the_vault_and_says_why(window):
    from ui.vault_session import LOCKED_BY_SLEEP
    window._on_session_locked(LOCKED_BY_SLEEP)

    assert not auth.is_unlocked()
    assert window.screens.currentWidget() is window.login_screen
    assert 'went to sleep' in window.login_screen.error.text()


@pytest.mark.parametrize('active, expected', [(True, 1), (False, 0)])
def test_a_linux_screen_lock_is_passed_on(qt_app, active, expected):
    """Driven through the slots D-Bus calls; the bus itself only exists on a
    Linux desktop."""
    from ui.vault_session import LOCKED_BY_SYSTEM, LinuxLockWatcher
    watcher = LinuxLockWatcher()
    heard = []
    watcher.locked.connect(heard.append)

    watcher._screensaver_changed(active)

    assert heard == [LOCKED_BY_SYSTEM] * expected


@pytest.mark.parametrize('sleeping, expected', [(True, 1), (False, 0)])
def test_a_linux_suspend_is_passed_on(qt_app, sleeping, expected):
    from ui.vault_session import LOCKED_BY_SLEEP, LinuxLockWatcher
    watcher = LinuxLockWatcher()
    heard = []
    watcher.locked.connect(heard.append)

    watcher._preparing_for_sleep(sleeping)

    assert heard == [LOCKED_BY_SLEEP] * expected


def test_a_linux_lock_reaches_the_vault(window):
    from ui.vault_session import LinuxLockWatcher
    watcher = LinuxLockWatcher(window)
    watcher.locked.connect(window._on_session_locked)

    watcher._screensaver_changed(True)

    assert not auth.is_unlocked()
    assert 'computer was locked' in window.login_screen.error.text()


# -- the clipboard on the way out -------------------------------------------------

def test_a_copied_secret_is_marked_for_kde(qt_app):
    from ui.vault_session import _secret_mime
    assert bytes(_secret_mime('hunter2').data('x-kde-passwordManagerHint')) == b'secret'


def test_quitting_clears_a_copied_secret(window, qt_app):
    from PySide6.QtGui import QGuiApplication
    window.copy_to_clipboard('hunter2-the-real-one', 'Password')

    qt_app.aboutToQuit.emit()

    assert QGuiApplication.clipboard().text() != 'hunter2-the-real-one'


def test_logging_off_clears_a_copied_secret(window, qt_app):
    from PySide6.QtGui import QGuiApplication
    window.copy_to_clipboard('hunter2-the-real-one', 'Password')

    window._clear_clipboard_for_logoff(None)

    assert QGuiApplication.clipboard().text() != 'hunter2-the-real-one'
    assert not window._clipboard_timer.isActive()

# -----------------------------------------------------------------------------
# tests/test_screen_capture.py
# Keeping the windows out of screenshots, and the setting that allows them.
#
# The suite runs on Qt's offscreen platform, which has no window system to ask,
# so the platform calls are replaced here and the real ones were checked by
# hand against Windows: a window and a dialog read back as excluded from their
# first show, and the setting switches both ways while they are open.
# -----------------------------------------------------------------------------

import pytest

from core import database
from core import preferences
from conftest import _destroy


@pytest.fixture
def capture(qt_app):
    from ui import screen_capture
    yield screen_capture
    screen_capture.set_allowed(False)


@pytest.fixture
def applied(capture, monkeypatch):
    """Every (window, allowed) the platform call would have received."""
    calls = []
    monkeypatch.setattr(capture, 'supported', lambda: True)
    monkeypatch.setattr(capture, '_apply_windows',
                        lambda handle, allow: calls.append(allow) or True)
    monkeypatch.setattr(capture, '_apply_macos',
                        lambda handle, allow: calls.append(allow) or True)
    from PySide6.QtGui import QGuiApplication
    monkeypatch.setattr(QGuiApplication, 'platformName', staticmethod(lambda: 'windows'))
    return calls


@pytest.fixture
def window(vault, qt_app):
    from ui.main_window import MainWindow
    window = MainWindow()
    window._enter_vault('test')
    yield window
    _destroy(window, qt_app)


# -- the setting ------------------------------------------------------------------

def test_captures_are_off_by_default(vault):
    assert preferences.get_settings()['allow_screen_capture'] == 0


def test_the_setting_is_saved(vault):
    preferences.save_settings(**{**preferences.get_settings(), 'allow_screen_capture': 1})
    assert preferences.get_settings()['allow_screen_capture'] == 1


def test_a_nonsense_stored_value_falls_back_to_off(vault):
    database.write_config('allow_screen_capture', '7')
    assert preferences.get_settings()['allow_screen_capture'] == 0


def test_a_nonsense_value_is_refused(vault):
    with pytest.raises(ValueError):
        preferences.save_settings(**{**preferences.get_settings(),
                                     'allow_screen_capture': 2})


# -- applying it ------------------------------------------------------------------

def test_nothing_is_attempted_where_it_is_not_supported(capture, qt_app):
    """The offscreen platform has no window system; a made-up handle must never
    reach a real one."""
    from PySide6.QtWidgets import QWidget
    assert capture.supported() is False
    assert capture.apply(QWidget()) is False


def test_a_window_is_protected_as_it_is_first_shown(capture, applied, qt_app):
    from PySide6.QtWidgets import QApplication, QDialog
    capture_filter = capture.CaptureFilter()
    QApplication.instance().installEventFilter(capture_filter)
    try:
        dialog = QDialog()
        dialog.show()
        assert applied == [False]
        dialog.close()
    finally:
        QApplication.instance().removeEventFilter(capture_filter)


def test_a_child_widget_is_left_to_its_window(capture, applied, qt_app):
    from PySide6.QtWidgets import QApplication, QLabel, QWidget
    capture_filter = capture.CaptureFilter()
    QApplication.instance().installEventFilter(capture_filter)
    try:
        parent = QWidget()
        parent.show()
        applied.clear()
        QLabel('child', parent).show()
        assert applied == []
        parent.close()
    finally:
        QApplication.instance().removeEventFilter(capture_filter)


def test_changing_the_choice_reaches_windows_already_open(capture, applied, qt_app):
    from PySide6.QtWidgets import QWidget
    shown = QWidget()
    shown.show()
    applied.clear()

    capture.set_allowed(True)

    assert True in applied
    shown.close()


def test_older_windows_falls_back_to_black(capture, monkeypatch):
    """EXCLUDEFROMCAPTURE needs Windows 10 2004; before that, MONITOR."""
    tried = []
    monkeypatch.setattr(capture, '_set_affinity',
                        lambda hwnd, value: tried.append(value) or value == capture._WDA_MONITOR)
    assert capture._apply_windows(1, allow=False) is True
    assert tried == [capture._WDA_EXCLUDEFROMCAPTURE, capture._WDA_MONITOR]


def test_allowing_clears_the_affinity(capture, monkeypatch):
    tried = []
    monkeypatch.setattr(capture, '_set_affinity',
                        lambda hwnd, value: tried.append(value) or True)
    capture._apply_windows(1, allow=True)
    assert tried == [capture._WDA_NONE]


# -- the window follows the vault -------------------------------------------------

def test_the_vaults_setting_is_put_in_force(window, capture):
    assert capture.allowed() is False
    preferences.save_settings(**{**preferences.get_settings(), 'allow_screen_capture': 1})
    window._reload_settings()
    window._apply_capture_setting()
    assert capture.allowed() is True


def test_closing_the_vault_goes_back_to_protected(window, capture):
    preferences.save_settings(**{**preferences.get_settings(), 'allow_screen_capture': 1})
    window._reload_settings()
    window._apply_capture_setting()

    window.close_vault()

    assert capture.allowed() is False


def test_the_settings_dialog_carries_the_choice(window):
    from ui.dialogs import SettingsDialog
    dialog = SettingsDialog(window, {**window.settings, 'allow_screen_capture': 1})
    assert dialog.values()['allow_screen_capture'] == 1
    dialog._allow_capture.setChecked(False)
    assert dialog.values()['allow_screen_capture'] == 0
    dialog.deleteLater()


def test_the_settings_dialog_says_when_it_cannot_help(window, capture):
    """The offscreen platform stands in for Linux here."""
    from ui.dialogs import SettingsDialog
    dialog = SettingsDialog(window, window.settings)
    assert dialog._allow_capture.isEnabled() is False
    dialog.deleteLater()

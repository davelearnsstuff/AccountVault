# -----------------------------------------------------------------------------
# ui/screen_capture.py
# Keeping the app's windows out of screenshots, recordings, screen sharing and
# Windows Recall.
#
# The clipboard keeps copied secrets out of its history; this does the same for
# the screen. A revealed password, the account list or a note is otherwise in
# every capture another program takes, and Recall takes one every few seconds
# and keeps a searchable history of them.
#
# Every top-level window is covered, dialogs and menus included, by one
# application-wide filter that applies the current choice as each window
# appears. Windows and macOS only: Linux has no equivalent, and Settings says
# so.
#
# On by default; Settings > Allow screenshots turns it off, per vault, for a
# bug report or a call. Before a vault is open the default holds.
# -----------------------------------------------------------------------------

import ctypes
import ctypes.util

from PySide6.QtCore import QEvent, QObject
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication

# Captures allowed. Starts False so a window is protected from its first show.
_allowed = False

# Windows display affinities. EXCLUDEFROMCAPTURE (Windows 10 2004 and later)
# leaves the window out of a capture entirely; MONITOR, the fallback, shows it
# as black.
_WDA_NONE = 0x00
_WDA_MONITOR = 0x01
_WDA_EXCLUDEFROMCAPTURE = 0x11

# NSWindowSharingType: None keeps the window out of captures, ReadOnly is the
# default every window starts with.
_NS_SHARING_NONE = 0
_NS_SHARING_READ_ONLY = 1


def supported() -> bool:
    """Whether this platform can keep a window out of captures. By Qt's
    platform name rather than the OS, so the offscreen platform the tests run
    under never hands a made-up handle to the window system."""
    return QGuiApplication.platformName() in ('windows', 'cocoa')


def allowed() -> bool:
    return _allowed


def set_allowed(value: bool):
    """Applies a new choice to every window already on screen, and to every
    window shown from now on."""
    global _allowed
    _allowed = bool(value)
    for widget in QApplication.topLevelWidgets():
        if widget.isVisible():
            apply(widget)


def apply(widget) -> bool:
    """Puts the current choice on one top-level window. True when the window
    system accepted it. Never raises: a window that cannot be protected still
    has to open."""
    if not supported():
        return False
    try:
        handle = int(widget.winId())
        if QGuiApplication.platformName() == 'windows':
            return _apply_windows(handle, _allowed)
        return _apply_macos(handle, _allowed)
    except Exception:
        return False


class CaptureFilter(QObject):
    """Applies the choice to each top-level window as it is shown. Installed
    on the application, so a new dialog needs nothing of its own."""

    def eventFilter(self, obj, event):
        if (event.type() == QEvent.Show and obj.isWidgetType() and obj.isWindow()):
            apply(obj)
        return False


# -----------------------------------------------------------------------------
# Windows
# -----------------------------------------------------------------------------

_set_affinity = None


def _apply_windows(hwnd: int, allow: bool) -> bool:
    global _set_affinity
    if _set_affinity is None:
        from ctypes import wintypes
        function = ctypes.WinDLL('user32', use_last_error=True).SetWindowDisplayAffinity
        function.argtypes = [wintypes.HWND, wintypes.DWORD]
        function.restype = wintypes.BOOL
        _set_affinity = function
    if allow:
        return bool(_set_affinity(hwnd, _WDA_NONE))
    # Older Windows refuses EXCLUDEFROMCAPTURE; black is the next best thing.
    return bool(_set_affinity(hwnd, _WDA_EXCLUDEFROMCAPTURE)
                or _set_affinity(hwnd, _WDA_MONITOR))


# -----------------------------------------------------------------------------
# macOS
#
# Through the Objective-C runtime directly, so it needs nothing beyond ctypes.
# Qt's winId() is the NSView; its window is the NSWindow that takes the
# setting. objc_msgSend has to be called through a function pointer of the
# exact signature on Apple silicon, hence one prototype per message.
# -----------------------------------------------------------------------------

_objc = None


def _objc_runtime():
    global _objc
    if _objc is None:
        library = ctypes.cdll.LoadLibrary(ctypes.util.find_library('objc'))
        library.sel_registerName.restype = ctypes.c_void_p
        library.sel_registerName.argtypes = [ctypes.c_char_p]
        send = ctypes.cast(library.objc_msgSend, ctypes.c_void_p).value
        _objc = {
            'sel': library.sel_registerName,
            'get': ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(send),
            'set': ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p,
                                    ctypes.c_ulong)(send),
        }
    return _objc


def _apply_macos(view: int, allow: bool) -> bool:
    objc = _objc_runtime()
    window = objc['get'](view, objc['sel'](b'window'))
    if not window:
        return False
    objc['set'](window, objc['sel'](b'setSharingType:'),
                _NS_SHARING_READ_ONLY if allow else _NS_SHARING_NONE)
    return True

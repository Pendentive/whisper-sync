"""Paste transcription results into the focused field."""

import platform
import threading
import time

import pyperclip

from .logger import logger

# Delay (seconds) before restoring previous clipboard contents.
# Must be long enough for the paste keystroke to land.
_RESTORE_DELAY: float = 0.8

_IS_WINDOWS = platform.system() == "Windows"

# ---------------------------------------------------------------------------
# Win32 clipboard helpers (pywin32)
# ---------------------------------------------------------------------------

_has_win32clipboard = False
if _IS_WINDOWS:
    try:
        import win32clipboard
        import win32con
        _has_win32clipboard = True
    except ImportError:
        logger.debug("pywin32 not installed, clipboard preservation limited to text only")


def _save_clipboard_win32() -> list[tuple[int, bytes | str | list]] | None:
    """Save all clipboard formats, preserving type info for proper restore.

    Returns a list of (format_id, data) tuples where data type matches
    what win32clipboard.SetClipboardData expects for that format.
    """
    try:
        win32clipboard.OpenClipboard()
    except Exception:
        return None
    try:
        formats = []
        fmt = 0
        while True:
            fmt = win32clipboard.EnumClipboardFormats(fmt)
            if fmt == 0:
                break
            try:
                data = win32clipboard.GetClipboardData(fmt)
                formats.append((fmt, data))
            except Exception:
                # Some synthesized formats can't be read directly; skip
                continue
        return formats if formats else None
    finally:
        win32clipboard.CloseClipboard()


def _restore_clipboard_win32(formats: list[tuple[int, bytes | str | list]]) -> None:
    """Restore previously saved clipboard formats.

    Data is passed back to SetClipboardData in the same type it was
    retrieved, so images, files, text, and other formats round-trip
    correctly.
    """
    try:
        win32clipboard.OpenClipboard()
    except Exception:
        return
    try:
        win32clipboard.EmptyClipboard()
        for fmt, data in formats:
            try:
                win32clipboard.SetClipboardData(fmt, data)
            except Exception:
                continue
    finally:
        win32clipboard.CloseClipboard()


def _has_focused_input() -> bool:
    """Check if there's a focused window that can accept paste.

    Returns False if the desktop or taskbar is focused.
    """
    if not _IS_WINDOWS:
        return True  # Assume yes on non-Windows
    try:
        import ctypes
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        if not hwnd:
            return False
        # Desktop window and taskbar shouldn't receive paste
        class_name = ctypes.create_unicode_buffer(256)
        ctypes.windll.user32.GetClassNameW(hwnd, class_name, 256)
        name = class_name.value
        # Shell_TrayWnd = taskbar, Progman/WorkerW = desktop
        if name in ("Shell_TrayWnd", "Progman", "WorkerW"):
            return False
        return True
    except Exception:
        return True  # Fail open


# ---------------------------------------------------------------------------
# Cross-platform save / restore wrappers
# ---------------------------------------------------------------------------


def _save_clipboard() -> list[tuple[int, bytes | str | list]] | str | None:
    """Save current clipboard contents.

    On Windows with pywin32 this preserves ALL formats (images, files,
    text, etc.). Otherwise falls back to pyperclip (text only).
    """
    try:
        if _has_win32clipboard:
            return _save_clipboard_win32()
        return pyperclip.paste()
    except Exception:
        logger.debug("Failed to save clipboard contents", exc_info=True)
        return None


def _schedule_clipboard_restore(previous: list | str | None) -> None:
    """Restore *previous* clipboard contents after a short delay.

    Runs in a daemon thread so it never blocks the caller.
    """
    if previous is None:
        return

    def _restore():
        time.sleep(_RESTORE_DELAY)
        try:
            if isinstance(previous, list) and _has_win32clipboard:
                _restore_clipboard_win32(previous)
            elif isinstance(previous, str):
                pyperclip.copy(previous)
            logger.debug("Clipboard restored to previous contents")
        except Exception:
            logger.debug("Clipboard restore failed", exc_info=True)

    threading.Thread(target=_restore, daemon=True).start()


def paste_clipboard(text: str, *, restore: bool = True) -> None:
    """Copy text to clipboard and attempt to paste into focused window.

    Step 1: Save previous clipboard (if restore=True)
    Step 2: Write dictation text to clipboard (ALWAYS)
    Step 3: If a window is focused, send Ctrl+V and schedule restore
    Step 4: If no window is focused, leave dictation text on clipboard
            (no restore, so user can manually Ctrl+V later)
    """
    previous = _save_clipboard() if restore else None

    pyperclip.copy(text)
    logger.info(f"Text in clipboard ({len(text)} chars)")

    # Only attempt auto-paste if there's a focused input window
    if not _has_focused_input():
        logger.debug("No focused input window, text left in clipboard")
        # Don't restore - user needs the dictation text for manual paste
        return

    try:
        import keyboard
        time.sleep(0.1)
        keyboard.send("ctrl+v")
    except Exception as e:
        try:
            import keyboard as _kb
            _kb.release("ctrl")
        except Exception:
            pass
        logger.warning(f"Auto-paste failed (text still in clipboard): {e}")
        # Don't restore - user needs clipboard contents to paste manually
        return

    _schedule_clipboard_restore(previous)


def paste_keystrokes(text: str, *, restore: bool = True) -> None:
    """Type text via simulated keystrokes. Falls back to clipboard if it fails."""
    previous = _save_clipboard() if restore else None

    if not _has_focused_input():
        pyperclip.copy(text)
        logger.debug("No focused input window, text copied to clipboard")
        return

    try:
        import keyboard
        keyboard.write(text, delay=0.01)
        _schedule_clipboard_restore(previous)
    except Exception:
        pyperclip.copy(text)
        logger.warning("Keystroke paste failed, text copied to clipboard")
        # Don't restore - user needs clipboard contents to paste manually


def paste(text: str, method: str = "clipboard", *, restore: bool = True) -> None:
    """Paste *text* using the given method.

    When *restore* is False (e.g. whisper/incognito mode) the clipboard
    is NOT restored after pasting, effectively clearing the user's
    previous clipboard contents.
    """
    if method == "clipboard":
        paste_clipboard(text, restore=restore)
    elif method == "keystrokes":
        paste_keystrokes(text, restore=restore)
    else:
        raise ValueError(f"Unknown paste method: {method}")

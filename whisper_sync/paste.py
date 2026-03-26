"""Paste transcription results into the focused field."""

import platform
import threading
import time

import pyperclip

from .logger import logger

# Delay (seconds) after detecting Ctrl+V before restoring clipboard.
# Gives the receiving app time to finish reading clipboard data.
_POST_PASTE_DELAY: float = 0.8

# Timeout (seconds) for waiting for manual Ctrl+V when no window is focused.
# After this, we give up waiting and restore clipboard anyway.
_MANUAL_PASTE_TIMEOUT: float = 30.0

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


def _restore_clipboard(previous: list | str | None) -> None:
    """Restore *previous* clipboard contents immediately."""
    if previous is None:
        return
    try:
        if isinstance(previous, list) and _has_win32clipboard:
            _restore_clipboard_win32(previous)
        elif isinstance(previous, str):
            pyperclip.copy(previous)
        logger.debug("Clipboard restored to previous contents")
    except Exception:
        logger.debug("Clipboard restore failed", exc_info=True)


def _wait_for_paste_then_restore(previous: list | str | None) -> None:
    """Wait for Ctrl+V, then restore previous clipboard after a delay.

    Listens for the next Ctrl+V keystroke (either our auto-paste or the
    user's manual paste). Once detected, waits _POST_PASTE_DELAY seconds
    for the receiving app to finish reading, then restores the cached
    clipboard contents.

    Runs in a daemon thread. Times out after _MANUAL_PASTE_TIMEOUT.
    """
    if previous is None:
        return

    def _wait_and_restore():
        try:
            import keyboard
            # Wait for the next Ctrl+V (blocks until pressed or timeout)
            keyboard.wait("ctrl+v", suppress=False, trigger_on_release=False)
        except Exception:
            # Timeout or error; restore anyway after a shorter delay
            logger.debug("Paste wait interrupted, restoring clipboard")
            time.sleep(1.0)
            _restore_clipboard(previous)
            return

        # Ctrl+V detected; give the receiving app time to read clipboard
        time.sleep(_POST_PASTE_DELAY)
        _restore_clipboard(previous)

    t = threading.Thread(target=_wait_and_restore, daemon=True)
    t.start()

    # Safety: if the thread is still alive after the timeout, the user
    # never pasted. Restore clipboard in a fallback thread so it's not
    # stuck forever.
    def _timeout_fallback():
        t.join(timeout=_MANUAL_PASTE_TIMEOUT)
        if t.is_alive():
            logger.debug("Paste wait timed out, restoring clipboard")
            _restore_clipboard(previous)

    threading.Thread(target=_timeout_fallback, daemon=True).start()


def paste_clipboard(text: str, *, restore: bool = True) -> None:
    """Copy text to clipboard and attempt to paste into focused window.

    Step 1: Save previous clipboard (if restore=True)
    Step 2: Write dictation text to clipboard (ALWAYS)
    Step 3: If focused window, send Ctrl+V (auto-paste)
    Step 4: Wait for Ctrl+V detection, then restore previous clipboard

    The restore fires after the paste is consumed, not on a blind timer.
    """
    previous = _save_clipboard() if restore else None

    pyperclip.copy(text)
    logger.info(f"Text in clipboard ({len(text)} chars)")

    if _has_focused_input():
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
            # Don't restore; user needs clipboard contents to paste manually
            return
    else:
        logger.debug("No focused input window, text left in clipboard for manual paste")

    # Wait for paste (auto or manual) then restore
    _wait_for_paste_then_restore(previous)


def paste_keystrokes(text: str, *, restore: bool = True) -> None:
    """Type text via simulated keystrokes. Falls back to clipboard if it fails."""
    previous = _save_clipboard() if restore else None

    if not _has_focused_input():
        pyperclip.copy(text)
        logger.debug("No focused input window, text copied to clipboard")
        _wait_for_paste_then_restore(previous)
        return

    try:
        import keyboard
        keyboard.write(text, delay=0.01)
        # Keystrokes don't use clipboard, so restore immediately
        _restore_clipboard(previous)
    except Exception:
        pyperclip.copy(text)
        logger.warning("Keystroke paste failed, text copied to clipboard")
        # Don't restore; user needs clipboard contents to paste manually


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

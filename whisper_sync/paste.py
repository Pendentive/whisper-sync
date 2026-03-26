"""Paste transcription results into the focused field."""

import platform
import sys
import threading
import time

import pyperclip

from .logger import logger

# Delay (seconds) before restoring previous clipboard contents.
# Must be long enough for the paste keystroke to land.
_RESTORE_DELAY: float = 0.5

# ---------------------------------------------------------------------------
# Win32 clipboard helpers (ctypes, no pywin32 dependency)
# ---------------------------------------------------------------------------

_IS_WINDOWS = platform.system() == "Windows"


def _save_clipboard_all_win32() -> list[tuple[int, bytes]] | None:
    """Save all clipboard formats as (format_id, raw_bytes) pairs.

    Uses the Win32 clipboard API via ctypes so images, files, and
    other non-text formats are preserved.
    """
    import ctypes

    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

    if not user32.OpenClipboard(0):
        return None
    try:
        formats: list[tuple[int, bytes]] = []
        fmt = 0
        while True:
            fmt = user32.EnumClipboardFormats(fmt)
            if fmt == 0:
                break
            handle = user32.GetClipboardData(fmt)
            if not handle:
                continue
            size = kernel32.GlobalSize(handle)
            if size == 0:
                continue
            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                continue
            try:
                data = ctypes.string_at(ptr, size)
                formats.append((fmt, data))
            finally:
                kernel32.GlobalUnlock(handle)
        return formats if formats else None
    finally:
        user32.CloseClipboard()


def _restore_clipboard_all_win32(formats: list[tuple[int, bytes]]) -> None:
    """Restore previously saved clipboard formats via the Win32 API."""
    import ctypes

    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

    if not user32.OpenClipboard(0):
        return
    try:
        user32.EmptyClipboard()
        GMEM_MOVEABLE = 0x0002
        for fmt, data in formats:
            handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
            if not handle:
                continue
            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                kernel32.GlobalFree(handle)
                continue
            try:
                ctypes.memmove(ptr, data, len(data))
            finally:
                kernel32.GlobalUnlock(handle)
            user32.SetClipboardData(fmt, handle)
    finally:
        user32.CloseClipboard()


# ---------------------------------------------------------------------------
# Cross-platform save / restore wrappers
# ---------------------------------------------------------------------------


def _save_clipboard() -> list[tuple[int, bytes]] | str | None:
    """Save current clipboard contents.

    On Windows this preserves ALL formats (images, files, text, etc.)
    via the Win32 clipboard API.  On other platforms it falls back to
    pyperclip which only handles plain text.
    """
    try:
        if _IS_WINDOWS:
            return _save_clipboard_all_win32()
        return pyperclip.paste()
    except Exception:
        logger.debug("Failed to save clipboard contents", exc_info=True)
        return None


def _schedule_clipboard_restore(previous: list[tuple[int, bytes]] | str | None) -> None:
    """Restore *previous* clipboard contents after a short delay.

    Runs in a daemon thread so it never blocks the caller.
    """
    if previous is None:
        return

    def _restore():
        time.sleep(_RESTORE_DELAY)
        try:
            if isinstance(previous, list):
                _restore_clipboard_all_win32(previous)
            else:
                pyperclip.copy(previous)
            logger.debug("Clipboard restored to previous contents")
        except Exception:
            logger.debug("Clipboard restore failed", exc_info=True)

    threading.Thread(target=_restore, daemon=True).start()


def paste_clipboard(text: str, *, restore: bool = True) -> None:
    """Copy text to clipboard and attempt to paste into focused window.

    ALWAYS puts text in clipboard first, then tries Ctrl+V.
    Text is guaranteed to be in clipboard regardless of outcome.

    When *restore* is True the previous clipboard contents are put back
    after the paste keystroke has had time to land.
    """
    previous = _save_clipboard() if restore else None

    pyperclip.copy(text)
    logger.info(f"Text in clipboard ({len(text)} chars)")
    try:
        import keyboard
        time.sleep(0.1)
        keyboard.send("ctrl+v")
    except Exception as e:
        # Release any stuck modifier keys from the failed send
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

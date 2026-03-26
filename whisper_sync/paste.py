"""Paste transcription results into the focused field."""

import platform
import threading
import time

import pyperclip

from .logger import logger

# Delay (seconds) before restoring previous clipboard contents.
# Must be long enough for the paste keystroke to land.
_RESTORE_DELAY: float = 0.5

_IS_WINDOWS = platform.system() == "Windows"

# ---------------------------------------------------------------------------
# Win32 clipboard helpers (pywin32)
# ---------------------------------------------------------------------------

_has_win32clipboard = False
if _IS_WINDOWS:
    try:
        import win32clipboard
        _has_win32clipboard = True
    except ImportError:
        logger.debug("pywin32 not installed, clipboard preservation limited to text only")


def _save_clipboard_win32() -> list[tuple[int, bytes]] | None:
    """Save all clipboard formats as (format_id, raw_bytes) pairs.

    Uses win32clipboard to preserve images, files, and all other formats.
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
                # GetClipboardData returns different types depending on format.
                # Convert to bytes for uniform storage.
                if isinstance(data, str):
                    data = data.encode("utf-8")
                elif not isinstance(data, bytes):
                    # Some formats return ints or other types; skip them
                    continue
                formats.append((fmt, data))
            except Exception:
                # Some formats can't be read (e.g., synthesized formats); skip
                continue
        return formats if formats else None
    finally:
        win32clipboard.CloseClipboard()


def _restore_clipboard_win32(formats: list[tuple[int, bytes]]) -> None:
    """Restore previously saved clipboard formats via win32clipboard."""
    try:
        win32clipboard.OpenClipboard()
    except Exception:
        return
    try:
        win32clipboard.EmptyClipboard()
        for fmt, data in formats:
            try:
                # CF_UNICODETEXT (13) was stored as utf-8 bytes; decode back
                if fmt == 13:  # CF_UNICODETEXT
                    win32clipboard.SetClipboardData(fmt, data.decode("utf-8"))
                else:
                    win32clipboard.SetClipboardData(fmt, data)
            except Exception:
                continue
    finally:
        win32clipboard.CloseClipboard()


# ---------------------------------------------------------------------------
# Cross-platform save / restore wrappers
# ---------------------------------------------------------------------------


def _save_clipboard() -> list[tuple[int, bytes]] | str | None:
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


def _schedule_clipboard_restore(previous: list[tuple[int, bytes]] | str | None) -> None:
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

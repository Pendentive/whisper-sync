"""Paste transcription results into the focused field."""

import threading
import time

import pyperclip

from .logger import logger

# Delay (seconds) before restoring previous clipboard contents.
# Must be long enough for the paste keystroke to land.
_RESTORE_DELAY: float = 0.5


def _save_clipboard() -> str | None:
    """Read the current clipboard contents, returning None on failure."""
    try:
        return pyperclip.paste()
    except Exception:
        return None


def _schedule_clipboard_restore(previous: str | None) -> None:
    """Restore *previous* clipboard contents after a short delay.

    Runs in a daemon thread so it never blocks the caller.
    """
    if previous is None:
        return

    def _restore():
        time.sleep(_RESTORE_DELAY)
        try:
            pyperclip.copy(previous)
            logger.debug("Clipboard restored to previous contents")
        except Exception:
            pass

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


def paste(text: str, method: str = "clipboard") -> None:
    # Save clipboard before any writes so we can restore afterwards
    if method == "clipboard":
        paste_clipboard(text)
    elif method == "keystrokes":
        paste_keystrokes(text)
    else:
        raise ValueError(f"Unknown paste method: {method}")

"""Paste transcription results into the focused field."""

import time

import pyperclip


def paste_clipboard(text: str) -> None:
    """Copy text to clipboard and attempt to paste into focused window.

    ALWAYS puts text in clipboard first, then tries Ctrl+V.
    Text is guaranteed to be in clipboard regardless of outcome.
    """
    pyperclip.copy(text)
    print(f"[WhisperSync] Text in clipboard ({len(text)} chars)")
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
        print(f"[WhisperSync] Auto-paste failed (text still in clipboard): {e}")


def paste_keystrokes(text: str) -> None:
    """Type text via simulated keystrokes. Falls back to clipboard if it fails."""
    try:
        import keyboard
        keyboard.write(text, delay=0.01)
    except Exception:
        pyperclip.copy(text)
        print("[WhisperSync] Keystroke paste failed, text copied to clipboard")


def paste(text: str, method: str = "clipboard") -> None:
    # Always ensure clipboard has the text as absolute fallback
    pyperclip.copy(text)
    if method == "clipboard":
        paste_clipboard(text)
    elif method == "keystrokes":
        paste_keystrokes(text)
    else:
        raise ValueError(f"Unknown paste method: {method}")

"""Windows toast notifications for WhisperSync.

Thin wrapper around `windows-toasts` with graceful fallback.
Handles AppUserModelID registration automatically.
"""

import logging
import threading

_logger = logging.getLogger("whisper_sync.notifications")

_AUMID = "Pendentive.WhisperSync"
_available = False
_has_progress_bar = False
_has_input_text_box = False

try:
    from windows_toasts import (
        InteractableWindowsToaster,
        Toast,
        ToastActivatedEventArgs,
        ToastButton,
    )

    _toaster = InteractableWindowsToaster(_AUMID)
    _available = True
except ImportError:
    _logger.warning(
        "windows-toasts not installed -- toast notifications disabled. "
        "Install with: pip install windows-toasts"
    )
except Exception as exc:
    _logger.warning(f"Toast notification init failed -- falling back to log: {exc}")

# Optional imports for progress bar and text input support
if _available:
    try:
        from windows_toasts import ToastProgressBar
        _has_progress_bar = True
    except ImportError:
        pass

    try:
        from windows_toasts import ToastInputTextBox
        _has_input_text_box = True
    except ImportError:
        pass


def notify(title: str, body: str, *, buttons=None, on_click=None):
    """Show a Windows toast notification.

    Args:
        title: Notification title (bold text).
        body: Notification body text.
        buttons: Optional list of dicts with "label" and "action" keys.
            Example: [{"label": "Merge", "action": callback}]
        on_click: Optional callback when the toast body is clicked.
    """
    if not _available:
        _logger.info(f"[toast] {title}: {body}")
        return

    try:
        toast = Toast([title, body])

        if buttons:
            for btn in buttons:
                label = btn.get("label", "")
                action = btn.get("action")
                toast_btn = ToastButton(label)
                if action:
                    # Capture action in closure for the callback.
                    # Run in a daemon thread so blocking callbacks (e.g. subprocess
                    # calls like PR merge) don't block the notification system.
                    def _make_handler(fn):
                        def _handler(event: ToastActivatedEventArgs):
                            def _run():
                                try:
                                    fn()
                                except Exception as exc:
                                    _logger.exception(f"Toast button callback error: {exc}")
                            threading.Thread(target=_run, daemon=True).start()
                        return _handler

                    toast_btn.on_activated = _make_handler(action)
                toast.AddAction(toast_btn)

        if on_click:
            def _body_handler(event: ToastActivatedEventArgs):
                def _run():
                    try:
                        on_click()
                    except Exception as exc:
                        _logger.exception(f"Toast click callback error: {exc}")
                threading.Thread(target=_run, daemon=True).start()

            toast.on_activated = _body_handler

        _toaster.show_toast(toast)
    except Exception as exc:
        _logger.debug(f"Toast notification failed: {exc}")
        _logger.info(f"[toast] {title}: {body}")


def notify_progress(title: str, caption: str, *, progress=None, progress_override=None):
    """Show a toast with a progress bar (or indeterminate spinner).

    Falls back to a plain notify() if ToastProgressBar is not available.

    Args:
        title: Notification title (bold text).
        caption: Progress bar caption (e.g. "Downloading...").
        progress: Float 0.0-1.0 for determinate, None for indeterminate.
        progress_override: Custom status text (e.g. "3 GB remaining").
    """
    if not _available or not _has_progress_bar:
        _logger.info(f"[toast] {title}: {caption}")
        return

    try:
        progress_bar = ToastProgressBar(
            caption, title,
            progress=progress,
            progress_override=progress_override,
        )
        toast = Toast(progress_bar=progress_bar)
        _toaster.show_toast(toast)
    except Exception as exc:
        _logger.debug(f"Toast progress notification failed: {exc}")
        _logger.info(f"[toast] {title}: {caption}")


def has_input_text_box():
    """Return True if ToastInputTextBox is available for speaker ID via toast."""
    return _has_input_text_box

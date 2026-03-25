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
    if not _available:
        _logger.info(f"[toast] {title}: {caption}")
        return

    if not _has_progress_bar:
        _logger.info("[toast] ToastProgressBar not available, falling back to plain toast")
        notify(title, caption)
        return

    try:
        progress_bar = ToastProgressBar(
            title, caption,
            progress=progress,
            progress_override=progress_override,
        )
        toast = Toast(progress_bar=progress_bar)
        _toaster.show_toast(toast)
    except Exception as exc:
        _logger.debug(f"Toast progress notification failed: {exc}")
        _logger.info(f"[toast] {title}: {caption}")


def notify_update(tag: str, title: str, body: str, *, progress=None):
    """Update an existing toast in-place by tag, or create if not exists.

    Toasts with the same tag+group replace each other without spawning a
    new popup.  Useful for transcription progress updates.

    Args:
        tag: Unique tag for this toast (e.g. "transcription").
        title: Notification title.
        body: Notification body text.
        progress: Optional float 0.0-1.0 for progress bar.
    """
    if not _available:
        _logger.info(f"[toast] {title}: {body}")
        return

    try:
        toast = Toast([title, body])
        toast.tag = tag
        toast.group = "whispersync"
        if progress is not None and _has_progress_bar:
            toast.progress_bar = ToastProgressBar("", "", progress=progress)
        _toaster.show_toast(toast)
    except Exception as exc:
        _logger.debug(f"Toast update failed: {exc}")
        _logger.info(f"[toast] {title}: {body}")


def has_input_text_box():
    """Return True if ToastInputTextBox is available for speaker ID via toast."""
    return _has_input_text_box


# ---------------------------------------------------------------------------
# Toast listener for StateManager integration
# ---------------------------------------------------------------------------

# Toast templates keyed by event type.  Only events listed here AND enabled
# in config["toast_events"] will trigger a toast.
TOAST_REGISTRY: dict[str, dict] = {
    "meeting_completed": {
        "title": "Meeting transcribed",
        "body": "{words} words, {speakers} speakers",
    },
    "dictation_completed": {
        "title": "Dictation complete",
        "body": "{words} words",
    },
    "error": {
        "title": "WhisperSync Error",
        "body": "{message}",
    },
    "pr_status_changed": {
        "title": "PR #{number}: {review_state}",
        "body": "{title}",
    },
}

# Default event types that trigger toasts (stored as list in config.json)
DEFAULT_TOAST_EVENTS = ["meeting_completed", "error", "pr_status_changed"]


class ToastListener:
    """State event subscriber that shows configurable Windows toasts.

    Register with StateManager.on_any() to receive all events.  Only events
    whose type is in config["toast_events"] AND has a TOAST_REGISTRY entry
    will produce a toast.
    """

    def __init__(self, config: dict):
        self._config = config

    def __call__(self, event) -> None:
        enabled = set(self._config.get("toast_events", DEFAULT_TOAST_EVENTS))
        if event.type not in enabled:
            return

        template = TOAST_REGISTRY.get(event.type)
        if not template:
            return

        try:
            title = template["title"].format(**event.data)
            body = template.get("body", "")
            if body:
                body = body.format(**event.data)
            notify(title, body)
        except (KeyError, IndexError) as exc:
            _logger.debug(f"Toast template format error for {event.type}: {exc}")

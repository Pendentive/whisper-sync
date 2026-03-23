"""Windows toast notifications for WhisperSync.

Thin wrapper around `windows-toasts` with graceful fallback.
Handles AppUserModelID registration automatically.
"""

import logging

_logger = logging.getLogger("whisper_sync.notifications")

_AUMID = "Pendentive.WhisperSync"
_available = False

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
                    # Capture action in closure for the callback
                    def _make_handler(fn):
                        def _handler(event: ToastActivatedEventArgs):
                            try:
                                fn()
                            except Exception as exc:
                                _logger.debug(f"Toast button callback error: {exc}")
                        return _handler

                    toast_btn.on_activated = _make_handler(action)
                toast.AddAction(toast_btn)

        if on_click:
            def _body_handler(event: ToastActivatedEventArgs):
                try:
                    on_click()
                except Exception as exc:
                    _logger.debug(f"Toast click callback error: {exc}")

            toast.on_activated = _body_handler

        _toaster.show_toast(toast)
    except Exception as exc:
        _logger.debug(f"Toast notification failed: {exc}")
        _logger.info(f"[toast] {title}: {body}")

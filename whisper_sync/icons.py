"""Generate tray icons programmatically (no external assets needed).

The icon system uses a declarative registry (ICON_REGISTRY) mapping state
keys to IconSpec dataclasses.  build_icon() renders any spec into a PIL
Image, with optional progress-ring support (outer ring draws as a clockwise
arc from 12 o'clock).

resolve_icon_key() maps a composite AppState to the correct registry key.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from PIL import Image, ImageDraw


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class IconSpec:
    """Declarative icon definition for a single app state."""

    outer: str
    """Hex color for the outer ring."""

    middle: str
    """Hex color for the middle filled circle."""

    inner: str | None = None
    """Optional hex color for the inner dot (dictation overlay)."""

    tooltip: str = ""
    """Tray tooltip suffix (shown as 'WhisperSync: {tooltip}')."""


# ---------------------------------------------------------------------------
# Icon registry - single source of truth for all visual states
# ---------------------------------------------------------------------------

ICON_REGISTRY: dict[str, IconSpec] = {
    # Idle
    "idle": IconSpec("#808080", "#808080", tooltip="Idle"),

    # Meeting states
    "recording.meeting":              IconSpec("#CC3333", "#FF4444", tooltip="Recording meeting..."),
    "recording.meeting.speaker_fail": IconSpec("#FFAA00", "#FF4444", tooltip="Recording (speaker issue)"),

    # Recording meeting while background transcription runs
    "recording.meeting.bg_transcribing":              IconSpec("#CC8800", "#FF4444", tooltip="Recording (bg transcribing)..."),
    "recording.meeting.bg_transcribing.speaker_fail": IconSpec("#FFAA00", "#FF4444", tooltip="Recording (bg transcribing, speaker issue)..."),

    # Dictation states
    "dictation":                              IconSpec("#CCCCCC", "#4488FF", tooltip="Dictating..."),
    "dictation.overlay.meeting":              IconSpec("#CC3333", "#FF4444", "#4488FF", tooltip="Dictating (meeting)..."),
    "dictation.overlay.meeting.speaker_fail": IconSpec("#FFAA00", "#FF4444", "#4488FF", tooltip="Dictating (meeting, speaker issue)..."),
    "dictation.overlay.transcribing":         IconSpec("#CC8800", "#FFAA00", "#4488FF", tooltip="Dictating (transcribing)..."),

    # Dictation during recording while background transcription runs
    "dictation.overlay.meeting.bg_transcribing":              IconSpec("#CC8800", "#FF4444", "#4488FF", tooltip="Dictating (meeting + bg transcribing)..."),
    "dictation.overlay.meeting.bg_transcribing.speaker_fail": IconSpec("#FFAA00", "#FF4444", "#4488FF", tooltip="Dictating (meeting + bg transcribing, speaker issue)..."),

    # Pipeline states
    "saving":        IconSpec("#CC8800", "#FFAA00", tooltip="Saving audio..."),
    "transcribing":  IconSpec("#CC8800", "#FFAA00", tooltip="Transcribing..."),
    "summarizing":   IconSpec("#9944CC", "#CC66FF", tooltip="Summarizing..."),
    "queued":        IconSpec("#CC6600", "#FF8800", tooltip="Queued..."),

    # Terminal states
    "done":  IconSpec("#44CC44", "#66FF66", tooltip="Done!"),
    "error": IconSpec("#CC44CC", "#FF66FF", tooltip="Error - check console"),

    # Animation frames
    "flash": IconSpec("#FFCC00", "#FFCC00", tooltip="Loading..."),
}


# ---------------------------------------------------------------------------
# Icon builder
# ---------------------------------------------------------------------------

def build_icon(spec: IconSpec, progress: float | None = None,
               size: int = 64) -> Image.Image:
    """Render an icon from a spec.

    Args:
        spec: IconSpec defining colors.
        progress: If 0.0-1.0, outer ring draws as a clockwise arc starting
            at 12 o'clock.  None or >= 1.0 draws the full ring.  0.0 draws
            no outer ring (just the middle circle).
        size: Canvas size in pixels.
    """
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    margin = 2
    ring_width = 3
    gap = 3
    outer_r = size - margin

    # Outer ring (full, partial arc, or absent)
    if progress is not None and 0.0 < progress < 1.0:
        # PIL angles: 0=3 o'clock, increasing counterclockwise in math coords.
        # On screen (Y-down), increasing angles render clockwise visually.
        # -90 = 12 o'clock. Adding sweep fills clockwise on screen.
        start_angle = -90
        sweep = 360 * progress
        draw.arc(
            [margin, margin, outer_r - 1, outer_r - 1],
            start_angle, start_angle + sweep,
            fill=spec.outer, width=ring_width,
        )
    elif progress is None or progress >= 1.0:
        draw.ellipse([margin, margin, outer_r - 1, outer_r - 1], fill=spec.outer)
        inner_of_ring = margin + ring_width
        draw.ellipse(
            [inner_of_ring, inner_of_ring,
             outer_r - 1 - ring_width, outer_r - 1 - ring_width],
            fill=(0, 0, 0, 0),
        )
    # else: progress == 0.0 - no outer ring drawn

    # Middle filled circle (after gap)
    mid_start = margin + ring_width + gap
    mid_end = size - margin - ring_width - gap
    if mid_end > mid_start:
        draw.ellipse([mid_start, mid_start, mid_end - 1, mid_end - 1],
                     fill=spec.middle)

    # Inner dot (overlay dictation indicator)
    if spec.inner is not None:
        dot_radius = 7
        cx, cy = size // 2, size // 2
        draw.ellipse(
            [cx - dot_radius, cy - dot_radius,
             cx + dot_radius - 1, cy + dot_radius - 1],
            fill=spec.inner,
        )

    return img


# ---------------------------------------------------------------------------
# State-to-key resolver
# ---------------------------------------------------------------------------

def resolve_icon_key(mode: str | None = None,
                     meeting_transcribing: bool = False,
                     dictation_overlay: bool = False,
                     speaker_ok: bool = True) -> str:
    """Map composite app state to an ICON_REGISTRY key.

    Accepts individual fields rather than an AppState to avoid a circular
    import (state_manager imports from icons).
    """
    if dictation_overlay:
        if mode == "meeting":
            if meeting_transcribing:
                return ("dictation.overlay.meeting.bg_transcribing.speaker_fail"
                        if not speaker_ok else "dictation.overlay.meeting.bg_transcribing")
            return ("dictation.overlay.meeting.speaker_fail"
                    if not speaker_ok else "dictation.overlay.meeting")
        if meeting_transcribing:
            return "dictation.overlay.transcribing"
        return "dictation"

    if mode == "meeting":
        if meeting_transcribing:
            return ("recording.meeting.bg_transcribing.speaker_fail"
                    if not speaker_ok else "recording.meeting.bg_transcribing")
        return "recording.meeting" if speaker_ok else "recording.meeting.speaker_fail"
    if mode:
        return mode  # "dictation", "saving", "transcribing", "done", "error"
    if meeting_transcribing:
        return "transcribing"
    return "idle"


# ---------------------------------------------------------------------------
# Icon animator
# ---------------------------------------------------------------------------

class IconAnimator:
    """Frame-by-frame icon animations in background threads."""

    def __init__(self, tray, lock=None):
        self._tray = tray
        self._lock = lock  # Optional threading.Lock for thread-safe tray updates
        self._cancel = threading.Event()

    def _set_tray(self, icon=None, title=None):
        """Set tray icon/title, using the lock if one was provided."""
        if self._lock is not None:
            with self._lock:
                if icon is not None:
                    self._tray.icon = icon
                if title is not None:
                    self._tray.title = title
        else:
            if icon is not None:
                self._tray.icon = icon
            if title is not None:
                self._tray.title = title

    def flash(self, count: int = 2, interval_ms: int = 150):
        """Yellow double-flash (universal loading/queuing signal)."""
        if self._tray is None:
            return
        self._cancel.clear()

        def _run():
            original = self._tray.icon
            flash_img = build_icon(ICON_REGISTRY["flash"])
            for _ in range(count):
                if self._cancel.is_set():
                    break
                self._set_tray(icon=flash_img)
                time.sleep(interval_ms / 1000)
                self._set_tray(icon=original)
                time.sleep(interval_ms / 1000)

        threading.Thread(target=_run, daemon=True).start()

    def flash_between(self, key_a: str, key_b: str,
                      count: int = 2, interval_ms: int = 150):
        """Alternating flash between two icon states (e.g., queued/transcribing)."""
        if self._tray is None:
            return
        self._cancel.clear()

        def _run():
            img_a = build_icon(ICON_REGISTRY[key_a])
            img_b = build_icon(ICON_REGISTRY[key_b])
            for _ in range(count):
                if self._cancel.is_set():
                    break
                self._set_tray(icon=img_a, title=f"WhisperSync: {ICON_REGISTRY[key_a].tooltip}")
                time.sleep(interval_ms / 1000)
                self._set_tray(icon=img_b, title=f"WhisperSync: {ICON_REGISTRY[key_b].tooltip}")
                time.sleep(interval_ms / 1000)

        threading.Thread(target=_run, daemon=True).start()

    def cancel(self):
        """Stop current animation."""
        self._cancel.set()


# ---------------------------------------------------------------------------
# Backward-compatible icon functions (deprecated, used during migration)
# ---------------------------------------------------------------------------

def idle_icon() -> Image.Image:
    return build_icon(ICON_REGISTRY["idle"])


def recording_icon(speaker_ok: bool = True) -> Image.Image:
    key = "recording.meeting" if speaker_ok else "recording.meeting.speaker_fail"
    return build_icon(ICON_REGISTRY[key])


def dictation_icon() -> Image.Image:
    return build_icon(ICON_REGISTRY["dictation"])


def dictation_during_recording_icon(speaker_ok: bool = True) -> Image.Image:
    key = ("dictation.overlay.meeting" if speaker_ok
           else "dictation.overlay.meeting.speaker_fail")
    return build_icon(ICON_REGISTRY[key])


def dictation_during_transcription_icon() -> Image.Image:
    return build_icon(ICON_REGISTRY["dictation.overlay.transcribing"])


def saving_icon() -> Image.Image:
    return build_icon(ICON_REGISTRY["saving"])


def transcribing_icon() -> Image.Image:
    return build_icon(ICON_REGISTRY["transcribing"])


def done_icon() -> Image.Image:
    return build_icon(ICON_REGISTRY["done"])


def summarizing_icon() -> Image.Image:
    return build_icon(ICON_REGISTRY["summarizing"])


def queued_icon() -> Image.Image:
    return build_icon(ICON_REGISTRY["queued"])


def error_icon() -> Image.Image:
    return build_icon(ICON_REGISTRY["error"])


def yellow_flash_icon(size: int = 64) -> Image.Image:
    return build_icon(ICON_REGISTRY["flash"], size=size)

"""Generate tray icons programmatically (no external assets needed)."""

from PIL import Image, ImageDraw


# --- Color constants ---

COLOR_IDLE = "#808080"
COLOR_RECORDING_OUTER = "#CC3333"       # Dark red - meeting outer ring
COLOR_RECORDING_INNER = "#FF4444"       # Lighter red - meeting inner circle
COLOR_RECORDING_FAIL = "#FFAA00"        # Yellow - channel failing during recording
COLOR_TRANSCRIBING_OUTER = "#CC8800"    # Dark amber - meeting transcribing outer
COLOR_TRANSCRIBING_INNER = "#FFAA00"    # Amber - meeting transcribing inner
COLOR_DONE_OUTER = "#44CC44"            # Dark green - done outer ring
COLOR_DONE_INNER = "#66FF66"            # Light green - done inner circle
COLOR_DICTATION_INNER = "#4488FF"       # Blue - mic active
COLOR_DICTATION_OUTER = "#CCCCCC"       # White/light gray - dictation outer ring
COLOR_SAVING_OUTER = "#CC8800"          # Dark amber - saving outer ring
COLOR_SAVING_INNER = "#FFAA00"          # Amber - saving inner circle
COLOR_SUMMARIZING_OUTER = "#9944CC"     # Dark purple - summarizing outer ring
COLOR_SUMMARIZING_INNER = "#CC66FF"     # Light purple - summarizing inner circle
COLOR_QUEUED_OUTER = "#CC6600"          # Dark orange - queued outer ring
COLOR_QUEUED_INNER = "#FF8800"          # Orange - queued inner circle
COLOR_ERROR_OUTER = "#CC44CC"           # Dark magenta - error outer ring
COLOR_ERROR_INNER = "#FF66FF"           # Light magenta - error inner circle


def _make_three_ring_icon(outer_color: str, middle_color: str,
                          inner_color: str | None = None, size: int = 64) -> Image.Image:
    """Generate icon with outer ring + middle filled circle + optional inner dot.

    - Outer ring: 3px wide, 2px margin
    - Gap: 2px transparent
    - Middle circle: filled, remaining space
    - Inner dot: ~4px radius, centered (only for overlay dictation)
    """
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    margin = 2
    ring_width = 3
    gap = 2

    # Outer ring
    outer_r = size - margin
    draw.ellipse([margin, margin, outer_r - 1, outer_r - 1], fill=outer_color)
    inner_of_ring = margin + ring_width
    draw.ellipse(
        [inner_of_ring, inner_of_ring,
         outer_r - 1 - ring_width, outer_r - 1 - ring_width],
        fill=(0, 0, 0, 0),
    )

    # Middle filled circle (after gap)
    mid_start = margin + ring_width + gap
    mid_end = size - margin - ring_width - gap
    if mid_end > mid_start:
        draw.ellipse([mid_start, mid_start, mid_end - 1, mid_end - 1], fill=middle_color)

    # Inner dot (overlay dictation indicator)
    if inner_color is not None:
        dot_radius = 4
        cx, cy = size // 2, size // 2
        draw.ellipse(
            [cx - dot_radius, cy - dot_radius, cx + dot_radius - 1, cy + dot_radius - 1],
            fill=inner_color,
        )

    return img


def _circle_icon(color: str, size: int = 64) -> Image.Image:
    """Backward-compat wrapper - both rings same color."""
    return _make_three_ring_icon(color, color, size=size)


# --- Public icon constructors ---
# Each returns a PIL Image suitable for pystray.
# Meeting icons accept speaker_ok to reflect loopback health.


def idle_icon() -> Image.Image:
    return _make_three_ring_icon(COLOR_IDLE, COLOR_IDLE)


def recording_icon(speaker_ok: bool = True) -> Image.Image:
    """Meeting recording - outer ring reflects speaker loopback status."""
    outer = COLOR_RECORDING_OUTER if speaker_ok else COLOR_RECORDING_FAIL
    return _make_three_ring_icon(outer, COLOR_RECORDING_INNER)


def dictation_icon() -> Image.Image:
    """Dictation - outer white/gray, middle blue."""
    return _make_three_ring_icon(COLOR_DICTATION_OUTER, COLOR_DICTATION_INNER)


def dictation_during_recording_icon(speaker_ok: bool = True) -> Image.Image:
    """Dictation active during meeting recording.

    Outer ring = dark red (meeting still recording), or yellow if speaker
    loopback is unhealthy.
    Middle = red (recording).
    Inner dot = blue (dictation active).
    """
    outer = COLOR_RECORDING_OUTER if speaker_ok else COLOR_RECORDING_FAIL
    return _make_three_ring_icon(outer, COLOR_RECORDING_INNER, COLOR_DICTATION_INNER)


def dictation_during_transcription_icon() -> Image.Image:
    """Dictation active during meeting transcription.

    Outer ring = dark amber (meeting transcribing).
    Middle = amber (transcribing).
    Inner dot = blue (dictation active).
    """
    return _make_three_ring_icon(COLOR_TRANSCRIBING_OUTER, COLOR_TRANSCRIBING_INNER, COLOR_DICTATION_INNER)


def saving_icon() -> Image.Image:
    return _make_three_ring_icon(COLOR_SAVING_OUTER, COLOR_SAVING_INNER)


def transcribing_icon() -> Image.Image:
    return _make_three_ring_icon(COLOR_TRANSCRIBING_OUTER, COLOR_TRANSCRIBING_INNER)


def done_icon() -> Image.Image:
    return _make_three_ring_icon(COLOR_DONE_OUTER, COLOR_DONE_INNER)


def summarizing_icon() -> Image.Image:
    return _make_three_ring_icon(COLOR_SUMMARIZING_OUTER, COLOR_SUMMARIZING_INNER)


def queued_icon() -> Image.Image:
    return _make_three_ring_icon(COLOR_QUEUED_OUTER, COLOR_QUEUED_INNER)


def error_icon() -> Image.Image:
    return _make_three_ring_icon(COLOR_ERROR_OUTER, COLOR_ERROR_INNER)


def yellow_flash_icon(size: int = 64) -> Image.Image:
    return _make_three_ring_icon("#FFCC00", "#FFCC00", size=size)

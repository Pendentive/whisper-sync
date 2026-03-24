"""Generate tray icons programmatically (no external assets needed)."""

from PIL import Image, ImageDraw


# --- Color constants ---

COLOR_IDLE = "#808080"
COLOR_RECORDING_OUTER = "#CC3333"       # Dark red - meeting outer ring
COLOR_RECORDING_INNER = "#FF4444"       # Lighter red - meeting inner circle
COLOR_RECORDING_FAIL = "#FFAA00"        # Yellow - channel failing during recording
COLOR_TRANSCRIBING_OUTER = "#CC8800"    # Dark amber - meeting transcribing outer
COLOR_TRANSCRIBING_INNER = "#FFA500"    # Lighter amber - meeting transcribing inner
COLOR_DONE = "#44FF44"
COLOR_DICTATION_INNER = "#4488FF"       # Blue - mic active
COLOR_DICTATION_OUTER = "#CCCCCC"       # White/light gray - dictation outer ring
COLOR_SAVING = "#FFB833"
COLOR_SUMMARIZING = "#AA44FF"
COLOR_QUEUED = "#FF8800"
COLOR_ERROR = "#FF44FF"


def _make_dual_icon(inner_color: str, outer_color: str, size: int = 64) -> Image.Image:
    """Generate a 64x64 icon with two concentric rings.

    - Outer ring: 3px wide, starts at 2px margin
    - Gap: 2px transparent
    - Inner circle: filled, remaining space (~24px radius)

    At 16x16 tray size the two zones remain distinguishable.
    """
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    margin = 2
    ring_width = 3
    gap = 2

    # Outer ring - draw filled ellipse then punch out interior
    outer_r = size - margin  # right/bottom edge
    draw.ellipse([margin, margin, outer_r - 1, outer_r - 1], fill=outer_color)

    # Punch out the inside of the ring (transparent)
    inner_of_ring = margin + ring_width
    draw.ellipse(
        [inner_of_ring, inner_of_ring,
         outer_r - 1 - ring_width, outer_r - 1 - ring_width],
        fill=(0, 0, 0, 0),
    )

    # Inner filled circle (after the gap)
    inner_start = margin + ring_width + gap
    inner_end = size - margin - ring_width - gap
    if inner_end > inner_start:
        draw.ellipse(
            [inner_start, inner_start, inner_end - 1, inner_end - 1],
            fill=inner_color,
        )

    return img


def _make_overlay_icon(outer_color: str, size: int = 64) -> Image.Image:
    """Generate a 64x64 icon with the outer ring in one color and a small blue dot centered.

    Used for dictation-during-meeting: outer ring shows meeting state,
    small centered blue dot shows dictation is active.

    The blue dot is ~40% the size of the normal inner circle.
    """
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    margin = 2
    ring_width = 3
    gap = 2

    # Outer ring - same as _make_dual_icon
    outer_r = size - margin
    draw.ellipse([margin, margin, outer_r - 1, outer_r - 1], fill=outer_color)

    inner_of_ring = margin + ring_width
    draw.ellipse(
        [inner_of_ring, inner_of_ring,
         outer_r - 1 - ring_width, outer_r - 1 - ring_width],
        fill=(0, 0, 0, 0),
    )

    # Small blue dot (~40% of the inner circle area)
    inner_start = margin + ring_width + gap
    inner_end = size - margin - ring_width - gap
    inner_radius = (inner_end - inner_start) / 2
    dot_radius = inner_radius * 0.4
    center = size / 2
    dot_start = int(center - dot_radius)
    dot_end = int(center + dot_radius)
    if dot_end > dot_start:
        draw.ellipse(
            [dot_start, dot_start, dot_end, dot_end],
            fill=COLOR_DICTATION_INNER,
        )

    return img


def _circle_icon(color: str, size: int = 64) -> Image.Image:
    """Legacy single-color circle - both rings same color."""
    return _make_dual_icon(color, color, size)


# --- Public icon constructors ---
# Each returns a PIL Image suitable for pystray.
# Meeting icons accept speaker_ok to reflect loopback health.


def idle_icon() -> Image.Image:
    return _make_dual_icon(COLOR_IDLE, COLOR_IDLE)


def recording_icon(speaker_ok: bool = True) -> Image.Image:
    """Meeting recording - outer ring reflects speaker loopback status."""
    outer = COLOR_RECORDING_OUTER if speaker_ok else COLOR_RECORDING_FAIL
    return _make_dual_icon(COLOR_RECORDING_INNER, outer)


def dictation_icon() -> Image.Image:
    """Dictation - mic active (inner blue), outer white/gray."""
    return _make_dual_icon(COLOR_DICTATION_INNER, COLOR_DICTATION_OUTER)


def dictation_during_recording_icon(speaker_ok: bool = True) -> Image.Image:
    """Dictation active during meeting recording.

    Outer ring = dark red (meeting still recording), or yellow if speaker
    loopback is unhealthy.
    Inner = small blue dot (dictation active).
    """
    outer = COLOR_RECORDING_OUTER if speaker_ok else COLOR_RECORDING_FAIL
    return _make_overlay_icon(outer)


def dictation_during_transcription_icon() -> Image.Image:
    """Dictation active during meeting transcription.

    Outer ring = dark amber (meeting transcribing).
    Inner = small blue dot (dictation active).
    """
    return _make_overlay_icon(COLOR_TRANSCRIBING_OUTER)


def saving_icon() -> Image.Image:
    return _make_dual_icon(COLOR_SAVING, COLOR_SAVING)


def transcribing_icon() -> Image.Image:
    return _make_dual_icon(COLOR_TRANSCRIBING_INNER, COLOR_TRANSCRIBING_OUTER)


def done_icon() -> Image.Image:
    return _make_dual_icon(COLOR_DONE, COLOR_DONE)


def summarizing_icon() -> Image.Image:
    return _make_dual_icon(COLOR_SUMMARIZING, COLOR_SUMMARIZING)


def queued_icon() -> Image.Image:
    return _make_dual_icon(COLOR_QUEUED, COLOR_QUEUED)


def error_icon() -> Image.Image:
    return _make_dual_icon(COLOR_ERROR, COLOR_ERROR)

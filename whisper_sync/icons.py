"""Generate tray icons programmatically (no external assets needed)."""

from PIL import Image, ImageDraw


# --- Color constants ---

COLOR_IDLE = "#808080"
COLOR_RECORDING = "#FF4444"
COLOR_RECORDING_FAIL = "#FFAA00"  # Channel failing during recording
COLOR_TRANSCRIBING = "#FFA500"
COLOR_DONE = "#44FF44"
COLOR_DICTATION = "#44AAFF"
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

    # Outer ring -- draw filled ellipse then punch out interior
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


def _circle_icon(color: str, size: int = 64) -> Image.Image:
    """Legacy single-color circle -- both rings same color."""
    return _make_dual_icon(color, color, size)


# --- Public icon constructors ---
# Each returns a PIL Image suitable for pystray.
# Meeting icons accept speaker_ok to reflect loopback health.


def idle_icon() -> Image.Image:
    return _make_dual_icon(COLOR_IDLE, COLOR_IDLE)


def recording_icon(speaker_ok: bool = True) -> Image.Image:
    """Meeting recording -- outer ring reflects speaker loopback status."""
    outer = COLOR_RECORDING if speaker_ok else COLOR_RECORDING_FAIL
    return _make_dual_icon(COLOR_RECORDING, outer)


def dictation_icon() -> Image.Image:
    """Dictation -- mic active (inner blue), no speaker needed (outer gray)."""
    return _make_dual_icon(COLOR_DICTATION, COLOR_IDLE)


def saving_icon() -> Image.Image:
    return _make_dual_icon(COLOR_SAVING, COLOR_SAVING)


def transcribing_icon() -> Image.Image:
    return _make_dual_icon(COLOR_TRANSCRIBING, COLOR_TRANSCRIBING)


def done_icon() -> Image.Image:
    return _make_dual_icon(COLOR_DONE, COLOR_DONE)


def summarizing_icon() -> Image.Image:
    return _make_dual_icon(COLOR_SUMMARIZING, COLOR_SUMMARIZING)


def queued_icon() -> Image.Image:
    return _make_dual_icon(COLOR_QUEUED, COLOR_QUEUED)


def error_icon() -> Image.Image:
    return _make_dual_icon(COLOR_ERROR, COLOR_ERROR)

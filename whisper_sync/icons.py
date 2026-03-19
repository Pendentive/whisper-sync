"""Generate tray icons programmatically (no external assets needed)."""

from PIL import Image, ImageDraw


def _circle_icon(color: str, size: int = 64) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    margin = 4
    draw.ellipse([margin, margin, size - margin, size - margin], fill=color)
    return img


def idle_icon() -> Image.Image:
    return _circle_icon("#888888")


def recording_icon() -> Image.Image:
    return _circle_icon("#FF4444")


def dictation_icon() -> Image.Image:
    return _circle_icon("#44AAFF")


def saving_icon() -> Image.Image:
    return _circle_icon("#FFB833")  # Orange — saving WAV


def transcribing_icon() -> Image.Image:
    return _circle_icon("#DDDD33")  # Yellow — whisperX running


def done_icon() -> Image.Image:
    return _circle_icon("#44DD44")  # Green — complete


def summarizing_icon() -> Image.Image:
    return _circle_icon("#AA44FF")  # Purple — Claude generating minutes


def queued_icon() -> Image.Image:
    return _circle_icon("#FF8800")  # Amber — dictation queued behind meeting


def error_icon() -> Image.Image:
    return _circle_icon("#FF44FF")  # Magenta — error

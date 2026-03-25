"""File-based logging for WhisperSync -- persists across crashes."""

import logging
import os
import sys
from datetime import datetime
from pathlib import Path

_LOG_DIR = Path(__file__).parent / "logs" / "app"
_LOG_DIR.mkdir(parents=True, exist_ok=True)

_log_file = _LOG_DIR / f"whisper-sync-{datetime.now():%Y-%m-%d}.log"

# Custom log level for transcription content (between DEBUG=10 and INFO=20)
TRANSCRIPT = 15
logging.addLevelName(TRANSCRIPT, "TRANSCRIPT")

# ANSI color codes
_C_RESET = "\033[0m"
_C_DIM = "\033[2m"          # dim gray for timestamps
_C_CYAN = "\033[36m"        # cyan for [WhisperSync] system tag
_C_GREEN = "\033[32m"       # green for dictation
_C_BLUE = "\033[34m"        # blue for meeting
_C_YELLOW = "\033[33m"      # yellow for warnings
_C_RED = "\033[31m"         # red for errors
_C_MAGENTA = "\033[35m"     # magenta for transcription text
_C_WHITE = "\033[37m"       # white for general info
_C_SECONDARY = "\033[95m"  # light purple for backup/secondary operations

# Enable ANSI on Windows
if sys.platform == "win32":
    os.system("")  # triggers VT100 mode


class _ColorFormatter(logging.Formatter):
    """Adds ANSI colors based on message content and level."""

    def __init__(self, base_fmt: str, datefmt: str = None, use_colors: bool = True):
        super().__init__(base_fmt, datefmt=datefmt)
        self.use_colors = use_colors

    def format(self, record: logging.LogRecord) -> str:
        msg = record.getMessage()
        if not self.use_colors:
            return super().format(record)

        # Color the timestamp portion
        ts = self.formatTime(record, self.datefmt)

        # Determine message color based on content/level
        # Red = errors, Yellow = warnings/recovery, Magenta = transcript text
        # Green = dictation results, Blue = meeting results / completed states
        # Cyan = system identity, White = in-progress actions
        if record.levelno >= logging.ERROR:
            msg_color = _C_RED
        elif record.levelno >= logging.WARNING:
            msg_color = _C_YELLOW
        elif record.levelno == TRANSCRIPT:
            msg_color = _C_MAGENTA
        elif any(kw in msg for kw in ("Recover", "recover", "Crash-recover", "crash")):
            msg_color = _C_YELLOW
        elif "Dictation:" in msg or "pasted" in msg or "clipboard" in msg:
            msg_color = _C_GREEN
        # Completed states -- blue
        elif any(kw in msg for kw in ("Loaded ", "loaded", " ready", "saved",
                                       "Saved:", "Minutes generated", "Renamed:",
                                       "Transcript saved", "WAV saved",
                                       "Speakers confirmed", "identified",
                                       "Model base", "Model large",
                                       "Worker respawned", "Worker restarted")):
            msg_color = _C_BLUE
        # In-progress actions -- white
        elif any(kw in msg for kw in ("Loading", "Transcribing", "Aligning",
                                       "Diarizing", "Generating", "Recovering",
                                       "Restarting", "Switching", "Setting")):
            msg_color = _C_WHITE
        # System identity -- cyan
        elif msg.startswith("===") or any(kw in msg for kw in (
                "starting", "Worker process spawned", "GPU:", "CPU mode",
                "batch_size", "Hotkeys", "Log file", "Right-click",
                "GitHub", "Speaker loopback", "mic +",
                "Meeting started", "Meeting stopped", "Recording")):
            msg_color = _C_CYAN
        else:
            msg_color = _C_WHITE

        # Secondary flag overrides text color (backup/overlay operations)
        # Only override INFO and DEBUG; preserve WARNING (yellow) and ERROR (red)
        if getattr(record, "secondary", False) and record.levelno <= logging.INFO:
            msg_color = _C_SECONDARY

        # Check if this is the verbose [WhisperSync] format
        if self._fmt and "WhisperSync" in self._fmt:
            colored = f"{_C_CYAN}[WhisperSync]{_C_RESET} {msg_color}{msg}{_C_RESET}"
        else:
            colored = f"{_C_DIM}[{ts}]{_C_RESET} {msg_color}{msg}{_C_RESET}"

        # Preserve exception tracebacks that super().format() would normally append
        if record.exc_info and record.exc_info[1] is not None:
            colored += "\n" + self.formatException(record.exc_info)

        return colored


# Root logger for the whisper_sync package
logger = logging.getLogger("whisper_sync")
logger.setLevel(logging.DEBUG)

# File handler -- DEBUG level, persists everything (no colors)
_fh = logging.FileHandler(str(_log_file), encoding="utf-8")
_fh.setLevel(logging.DEBUG)
_fh.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
))
logger.addHandler(_fh)

# Console handler -- level and format controlled by log_window tier
_ch = logging.StreamHandler(sys.stdout)
_ch.setLevel(logging.INFO)

# Formatters for different tiers (with colors)
_fmt_clean = _ColorFormatter("[%(asctime)s] %(message)s", datefmt="%H:%M")
_fmt_verbose = _ColorFormatter("[WhisperSync] %(message)s")

_ch.setFormatter(_fmt_verbose)
logger.addHandler(_ch)

# Current tier (module-level state)
_current_tier = "normal"


class _TierFilter(logging.Filter):
    """Filter console output based on the active log_window tier."""

    def filter(self, record: logging.LogRecord) -> bool:
        tier = _current_tier
        if tier == "off":
            return False
        if tier == "normal":
            # Show INFO and above, but not TRANSCRIPT or DEBUG
            return record.levelno >= logging.INFO
        if tier == "detailed":
            # Show TRANSCRIPT and above (INFO, WARNING, ERROR, CRITICAL)
            return record.levelno >= TRANSCRIPT
        # verbose -- show everything
        return True


_tier_filter = _TierFilter()
_ch.addFilter(_tier_filter)


def set_console_level(tier: str) -> None:
    """Set the console log window tier. Called at startup and when settings change.

    Tiers: "off", "normal", "detailed", "verbose"
    """
    global _current_tier
    tier = tier.lower() if tier else "normal"
    if tier not in ("off", "normal", "detailed", "verbose"):
        tier = "normal"
    _current_tier = tier

    # Adjust the console handler level floor so Python doesn't pre-filter
    if tier == "off":
        _ch.setLevel(logging.CRITICAL + 1)
    elif tier == "verbose":
        _ch.setLevel(logging.DEBUG)
    elif tier == "detailed":
        _ch.setLevel(TRANSCRIPT)
    else:
        _ch.setLevel(logging.INFO)

    # Switch formatter: verbose keeps prefix, others use clean timestamp
    if tier == "verbose":
        _ch.setFormatter(_fmt_verbose)
    else:
        _ch.setFormatter(_fmt_clean)


def log_dictation_result(text: str, duration: float, delivery: str, chars: int,
                         secondary: bool = False) -> None:
    """Log a dictation result at appropriate tiers.

    Normal:   [HH:MM] Dictation: 0.67s -- pasted (97 chars)
    Detailed: adds text preview
    Verbose:  adds model info (handled by caller's own debug logs)

    When *secondary* is True the log lines are tagged so the formatter
    renders them in the secondary (light-purple) color.
    """
    extra = {"secondary": True} if secondary else {}
    logger.info(f"Dictation: {duration:.2f}s -- {delivery} ({chars} chars)", extra=extra)
    if text:
        # Truncate preview to 120 chars for readability
        preview = text[:120] + ("..." if len(text) > 120 else "")
        logger.log(TRANSCRIPT, f'        "{preview}"', extra=extra)


def log_meeting_result(name: str, duration_secs: float, words: int, speakers: int, folder: str) -> None:
    """Log meeting transcription result at appropriate tiers.

    Normal:   [HH:MM] Transcribed: 4,231 words, 3 speakers
              [HH:MM] Saved: 03-w3/0320_1019_openclaw-mcp-flow/
    Detailed: adds speaker segment info (handled by caller via log_transcript_preview)
    """
    logger.info(f"Transcribed: {words:,} words, {speakers} speaker{'s' if speakers != 1 else ''}")
    logger.info(f"Saved: {folder}")


def log_transcript_preview(text: str, speakers: dict = None) -> None:
    """Log transcript content at TRANSCRIPT level (visible in detailed+verbose tiers).

    speakers: dict mapping speaker labels to list of sample utterances
              e.g. {"Colby": ["So the next improvement..."], "Dinesh": ["Yeah, the OAuth..."]}
    """
    if speakers:
        for speaker, utterances in speakers.items():
            for utt in utterances[:1]:  # First utterance per speaker
                preview = utt[:80] + ("..." if len(utt) > 80 else "")
                logger.log(TRANSCRIPT, f"        [{speaker}] {preview}")
    elif text:
        preview = text[:200] + ("..." if len(text) > 200 else "")
        logger.log(TRANSCRIPT, f"        {preview}")


def get_log_path() -> Path:
    return _log_file

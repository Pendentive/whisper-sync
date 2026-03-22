"""File-based logging for WhisperSync -- persists across crashes."""

import logging
import sys
from datetime import datetime
from pathlib import Path

_LOG_DIR = Path(__file__).parent / "logs" / "app"
_LOG_DIR.mkdir(parents=True, exist_ok=True)

_log_file = _LOG_DIR / f"whisper-sync-{datetime.now():%Y-%m-%d}.log"

# Custom log level for transcription content (between DEBUG=10 and INFO=20)
TRANSCRIPT = 15
logging.addLevelName(TRANSCRIPT, "TRANSCRIPT")

# Root logger for the whisper_sync package
logger = logging.getLogger("whisper_sync")
logger.setLevel(logging.DEBUG)

# File handler -- DEBUG level, persists everything
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

# Formatters for different tiers
_fmt_clean = logging.Formatter("[%(asctime)s] %(message)s", datefmt="%H:%M")
_fmt_verbose = logging.Formatter("[WhisperSync] %(message)s")

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


def log_dictation_result(text: str, duration: float, delivery: str, chars: int) -> None:
    """Log a dictation result at appropriate tiers.

    Normal:   [HH:MM] Dictation: 0.67s -- pasted (97 chars)
    Detailed: adds text preview
    Verbose:  adds model info (handled by caller's own debug logs)
    """
    logger.info(f"Dictation: {duration:.2f}s -- {delivery} ({chars} chars)")
    if text:
        # Truncate preview to 120 chars for readability
        preview = text[:120] + ("..." if len(text) > 120 else "")
        logger.log(TRANSCRIPT, f'        "{preview}"')


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

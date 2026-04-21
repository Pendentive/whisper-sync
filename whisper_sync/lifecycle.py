"""Process lifecycle logging.

Every process start and end should produce an identifiable log line so that
silent deaths can be distinguished from clean quits. This module owns the
startup/exit banners and the exit-reason state.

Design:
    * The first call to ``record_exit_reason`` wins. Later calls are ignored
      so that a late-firing atexit callback can't mask the real cause
      (e.g. an unhandled exception that already recorded ``exception``).
    * ``install`` wires up atexit + signal handlers. On any exit path we
      emit a single ``=== WhisperSync exiting (reason=..., ...) ===`` line
      so grep-for-gaps analysis can tell crashes from clean exits.
"""

from __future__ import annotations

import atexit
import logging
import os
import platform
import signal
import subprocess
import sys
import threading
from pathlib import Path

_state_lock = threading.Lock()
_exit_reason: str = "unknown"
_exit_extra: dict = {}
_installed: bool = False


def _reset_for_tests() -> None:
    """Reset module state. Test-only."""
    global _exit_reason, _exit_extra, _installed
    with _state_lock:
        _exit_reason = "unknown"
        _exit_extra = {}
        _installed = False


def record_exit_reason(reason: str, extra: dict | None = None) -> None:
    """Record why the process is about to exit. First caller wins."""
    global _exit_reason, _exit_extra
    with _state_lock:
        if _exit_reason != "unknown":
            return
        _exit_reason = reason
        _exit_extra = dict(extra) if extra else {}


def get_exit_reason() -> tuple[str, dict]:
    """Return the currently-recorded exit reason + extras."""
    with _state_lock:
        return _exit_reason, dict(_exit_extra)


def _git_sha() -> str:
    """Best-effort short git SHA for this checkout. 'unknown' on failure."""
    try:
        repo_root = Path(__file__).resolve().parent.parent
        result = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        sha = result.stdout.strip()
        if sha:
            return sha
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return "unknown"


def _format_extra(extra: dict) -> str:
    """Render extras as ``k1=v1 k2=v2`` for grep-friendly logs."""
    if not extra:
        return ""
    return " " + " ".join(f"{k}={v}" for k, v in extra.items())


def log_startup_banner(logger: logging.Logger) -> None:
    """Emit the startup banner with PID, Python/OS/git identifiers."""
    logger.info(
        "=== WhisperSync starting === pid=%d python=%s os=%s git=%s",
        os.getpid(),
        platform.python_version(),
        platform.platform(terse=True),
        _git_sha(),
    )


def log_exit_banner(logger: logging.Logger) -> None:
    """Emit the exit banner using the currently-recorded reason."""
    reason, extra = get_exit_reason()
    logger.info(
        "=== WhisperSync exiting === reason=%s%s",
        reason,
        _format_extra(extra),
    )
    for handler in logger.handlers:
        try:
            handler.flush()
        except Exception:
            pass


def install(logger: logging.Logger) -> None:
    """Register atexit + signal handlers that emit the exit banner.

    Safe to call multiple times; the second call is a no-op.
    """
    global _installed
    with _state_lock:
        if _installed:
            return
        _installed = True

    def _atexit():
        record_exit_reason("atexit")
        log_exit_banner(logger)

    atexit.register(_atexit)

    def _signal_handler(signum, _frame):
        try:
            name = signal.Signals(signum).name
        except ValueError:
            name = str(signum)
        # Only record the reason here. The atexit hook emits the single
        # exit banner during normal interpreter shutdown — doing logging
        # inside a signal handler is both risky (async-signal-safety) and
        # duplicates the atexit banner once sys.exit() runs.
        record_exit_reason("signal", {"signal": name})
        try:
            signal.signal(signum, signal.SIG_DFL)
        except Exception:
            pass
        sys.exit(128 + signum)

    # SIGTERM + SIGBREAK (Windows) + SIGINT (Ctrl+C) are the common paths.
    for sig_name in ("SIGTERM", "SIGBREAK", "SIGINT"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _signal_handler)
        except (ValueError, OSError):
            # Some signals are not settable on every platform/thread.
            pass

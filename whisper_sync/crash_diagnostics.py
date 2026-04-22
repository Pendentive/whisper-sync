"""Crash diagnostics - exception hooks and Windows Event Log check.

Zero runtime cost: excepthook is a passive function pointer,
Event Log is queried once at startup.
"""

import faulthandler
import logging
import subprocess
import sys
import threading
import traceback
from pathlib import Path

# Module-level reference to the faulthandler log file. Without a strong ref,
# the FileIO can be garbage-collected, its fd closes, and later native crash
# dumps are silently lost, which is exactly what produced the historic
# silent-death logs with no forensic trace. Both the main tray process and
# the worker subprocess install their faulthandler via this module so the
# fix applies uniformly.
_FAULTHANDLER_FILE = None


def _reset_faulthandler_for_tests() -> None:
    """Test-only: close and drop the retained faulthandler file."""
    global _FAULTHANDLER_FILE
    try:
        if _FAULTHANDLER_FILE is not None and not _FAULTHANDLER_FILE.closed:
            _FAULTHANDLER_FILE.close()
    except Exception:
        pass
    _FAULTHANDLER_FILE = None


def install_faulthandler(log_path) -> "object | None":
    """Enable ``faulthandler`` with a retained log file.

    Opens ``log_path`` in line-buffered append mode, stores the handle at
    module scope so CPython cannot GC it, then registers the handle with
    ``faulthandler.enable``. On any failure (path not writable, enable
    raising), closes the file, clears the retained ref, and best-effort
    falls back to the stderr default with ``all_threads=True`` so thread
    crashes are still captured.

    Returns the open file object on success, ``None`` if the file-backed
    install failed (stderr fallback may still be active).
    """
    global _FAULTHANDLER_FILE

    def _fallback_to_stderr():
        try:
            faulthandler.enable(all_threads=True)
        except Exception:
            pass

    try:
        f = open(Path(log_path), "a", buffering=1, encoding="utf-8")
    except (OSError, ValueError):
        _fallback_to_stderr()
        return None

    _FAULTHANDLER_FILE = f
    try:
        faulthandler.enable(file=f, all_threads=True)
    except Exception:
        # enable() failed with the file. Release the file and fall back
        # to stderr so thread crashes are still captured.
        try:
            f.close()
        except Exception:
            pass
        _FAULTHANDLER_FILE = None
        _fallback_to_stderr()
        return None
    return f


def install_excepthook(log: logging.Logger) -> None:
    """Register global handlers for unhandled exceptions.

    Catches both main-thread and background-thread crashes,
    logs the full traceback, releases keyboard hooks, then exits.
    """

    def _handle_exception(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        tb = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        log.critical(f"Unhandled exception:\n{tb}")
        # Release keyboard hooks to prevent stuck modifier keys
        try:
            import keyboard
            keyboard.unhook_all()
        except Exception:
            pass
        # Flush all handlers so the traceback is persisted
        for handler in log.handlers:
            handler.flush()

    def _handle_thread_exception(args):
        if issubclass(args.exc_type, KeyboardInterrupt):
            return
        tb = "".join(
            traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)
        )
        thread_name = args.thread.name if args.thread is not None else "<unknown>"
        log.critical(f"Unhandled exception in thread '{thread_name}':\n{tb}")
        for handler in log.handlers:
            handler.flush()

    sys.excepthook = _handle_exception
    threading.excepthook = _handle_thread_exception


def check_previous_crash(log: logging.Logger) -> str | None:
    """Query Windows Event Log for recent python.exe crashes.

    Looks for 'Application Error' events from the last 24 hours matching
    our venv's python.exe path. Now pulls a larger slice of each event
    message (the first ~12 lines) so the faulting module name and offset
    are surfaced — without that detail we can't tell a Tcl/Tk crash from
    a torch-DLL crash from a Python runtime crash.

    Returns the summary string if any events were found, else None.
    """
    python_exe = sys.executable.replace("\\", "\\\\")

    ps_script = (
        "Get-WinEvent -FilterHashtable @{"
        "LogName='Application';"
        "ProviderName='Application Error';"
        "StartTime=(Get-Date).AddHours(-24)"
        "} -ErrorAction SilentlyContinue "
        f"| Where-Object {{ $_.Message -like '*{python_exe}*' }} "
        "| Select-Object -First 3 "
        "| ForEach-Object { "
        "  $ts = $_.TimeCreated.ToString('yyyy-MM-dd HH:mm:ss'); "
        "  $msg = ($_.Message -split \"`n\" | Select-Object -First 12) -join ' | '; "
        "  \"$ts -- $msg\" "
        "}"
    )

    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            capture_output=True,
            text=True,
            timeout=5,
        )
        output = result.stdout.strip()
        if output:
            log.warning("Previous crash(es) in Windows Event Log:\n%s", output)
            faulting_module = _extract_faulting_module(output)
            if faulting_module:
                log.warning(
                    "Previous crash faulting module: %s (common culprits: "
                    "tcl86t.dll/tk86t.dll=Tk GUI thread issue; "
                    "torch_*.dll=CUDA/DLL load; python3*.dll=Python runtime)",
                    faulting_module,
                )
            return output
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        log.debug(f"Event Log check skipped: {e}")

    return None


def _extract_faulting_module(message: str) -> str | None:
    """Pull 'Faulting module name: X.dll' out of an Application Error message."""
    import re
    match = re.search(r"Faulting module name:\s*([^\s,|]+)", message, re.IGNORECASE)
    if match:
        return match.group(1)
    return None

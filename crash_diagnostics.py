"""Crash diagnostics — exception hooks and Windows Event Log check.

Zero runtime cost: excepthook is a passive function pointer,
Event Log is queried once at startup.
"""

import logging
import subprocess
import sys
import threading
import traceback


def install_excepthook(log: logging.Logger) -> None:
    """Register global handlers for unhandled exceptions.

    Catches both main-thread and background-thread crashes,
    logs the full traceback, then exits.
    """

    def _handle_exception(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        tb = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        log.critical(f"Unhandled exception:\n{tb}")
        # Flush all handlers so the traceback is persisted
        for handler in log.handlers:
            handler.flush()

    def _handle_thread_exception(args):
        if issubclass(args.exc_type, KeyboardInterrupt):
            return
        tb = "".join(
            traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)
        )
        log.critical(f"Unhandled exception in thread '{args.thread.name}':\n{tb}")
        for handler in log.handlers:
            handler.flush()

    sys.excepthook = _handle_exception
    threading.excepthook = _handle_thread_exception


def check_previous_crash(log: logging.Logger) -> str | None:
    """Query Windows Event Log for recent python.exe crashes.

    Looks for 'Application Error' events from the last 24 hours
    matching our venv's python.exe path. Returns a summary string
    if found, or None.
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
        "| ForEach-Object { $_.TimeCreated.ToString('HH:mm:ss') + ' | ' + "
        "($_.Message -split \"`n\" | Select-Object -First 2) -join ' ' }"
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
            log.warning(f"Previous crash detected in Windows Event Log:\n{output}")
            return output
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        log.debug(f"Event Log check skipped: {e}")

    return None

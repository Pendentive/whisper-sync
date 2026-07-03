"""Single-instance lock and orphan worker reaping.

Two protections that previously lived only in start.ps1, meaning any
launch path that bypassed the launcher (updater restart, direct
shortcut, python -m) could run a second instance - doubling VRAM and
CPU load with a second CUDA worker - and a crashed parent left its
worker subprocess holding a CUDA context until the NEXT launcher run
(2026-07-03 gpu-guard spec, B4).

- ``acquire_single_instance()``: a named Win32 mutex held for the
  process lifetime. Second acquisition anywhere on the machine fails.
- Worker PID registry: TranscriptionWorker registers each spawned
  worker pid in ``worker-pids.json`` in the data dir and unregisters on
  clean stop. ``reap_orphans()`` at startup terminates any registered
  pid that is still alive, still a python process (PID-reuse guard),
  and not owned by the current process tree.

Non-Windows platforms: the mutex degrades to always-acquired and
reaping still works (pid registry is portable; termination uses
os.kill). The app is Windows-first; this keeps imports safe elsewhere.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from .logger import logger

_MUTEX_NAME = "Global\\WhisperSyncV1Instance"
_mutex_handle = None  # held for process lifetime; never closed deliberately
_registry_lock = threading.Lock()

ERROR_ALREADY_EXISTS = 183


def acquire_single_instance(name: str = _MUTEX_NAME) -> bool:
    """Try to become the single running instance. True if we are it.

    Holds a named Win32 mutex for the life of the process. Returns True
    on non-Windows platforms (no-op degrade).
    """
    global _mutex_handle
    if os.name != "nt":
        return True
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)

    handle = kernel32.CreateMutexW(None, False, name)
    if not handle:
        # Could not even create the mutex (unexpected): fail open so a
        # guard malfunction never blocks the app itself.
        logger.warning(f"instance guard: CreateMutexW failed (err={ctypes.get_last_error()}); proceeding")
        return True
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return False
    _mutex_handle = handle
    return True


# -- worker pid registry ------------------------------------------------------


def _registry_path() -> Path:
    from .paths import get_data_dir
    return get_data_dir() / "worker-pids.json"


def _load_registry(path: Path) -> dict:
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_registry(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
    except OSError as exc:
        logger.warning(f"instance guard: could not write pid registry: {exc}")


def register_worker_pid(pid: int, kind: str = "worker") -> None:
    """Record a spawned worker subprocess pid (crash-survivable)."""
    with _registry_lock:
        path = _registry_path()
        data = _load_registry(path)
        data[str(pid)] = {"kind": kind, "parent": os.getpid()}
        _save_registry(path, data)


def unregister_worker_pid(pid: int) -> None:
    """Remove a pid after a clean worker stop."""
    with _registry_lock:
        path = _registry_path()
        data = _load_registry(path)
        if data.pop(str(pid), None) is not None:
            _save_registry(path, data)


def _is_python_process(pid: int) -> bool:
    """PID-reuse guard: only ever terminate a process whose image is python."""
    if os.name != "nt":
        return True  # rely on the registry alone off-Windows
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        ok = kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size))
        if not ok:
            return False
        name = Path(buf.value).name.lower()
        return name in ("python.exe", "pythonw.exe")
    finally:
        kernel32.CloseHandle(handle)


def _pid_alive(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        STILL_ACTIVE = 259
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _terminate(pid: int) -> bool:
    if os.name != "nt":
        try:
            import signal
            os.kill(pid, signal.SIGKILL)
            return True
        except OSError:
            return False
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    PROCESS_TERMINATE = 0x0001
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
    if not handle:
        return False
    try:
        return bool(kernel32.TerminateProcess(handle, 1))
    finally:
        kernel32.CloseHandle(handle)


def reap_orphans() -> list:
    """Terminate registered worker pids left behind by a dead parent.

    Called once at startup, after acquire_single_instance() succeeds (so
    the registry cannot belong to a healthy sibling instance). Skips
    pids whose recorded parent is the current process, pids no longer
    alive (registry entry just removed), and pids whose image is not
    python (PID reuse). Returns the list of pids terminated.
    """
    reaped = []
    with _registry_lock:
        path = _registry_path()
        data = _load_registry(path)
        if not data:
            return reaped
        remaining = {}
        for pid_str, meta in data.items():
            try:
                pid = int(pid_str)
            except ValueError:
                continue
            if meta.get("parent") == os.getpid():
                remaining[pid_str] = meta
                continue
            if not _pid_alive(pid):
                continue  # stale entry; drop
            if not _is_python_process(pid):
                logger.warning(
                    f"instance guard: pid {pid} in registry is not python (PID reuse); dropping entry"
                )
                continue
            if _terminate(pid):
                logger.info(f"instance guard: reaped orphan worker pid {pid} ({meta.get('kind', '?')})")
                reaped.append(pid)
            else:
                remaining[pid_str] = meta  # could not kill; keep for next time
        _save_registry(path, remaining)
    return reaped

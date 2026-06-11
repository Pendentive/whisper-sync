"""TranscriptionWorker — manages a long-lived subprocess for crash-safe transcription.

The worker runs whisperX/CTranslate2/CUDA in isolation. If it segfaults,
the main process detects the death, logs it, and respawns automatically.

Stability rebuild Phase 6: a single READER THREAD owns the response queue.
Previously multiple threads raced ``response_q.get()`` (``wait_ready`` vs
``_wait_response``; dictation vs meeting paths), and one consumer could
swallow another's response, wedging the second caller forever. Now every
request registers a pending-entry keyed by request_id; the reader routes
responses to waiters and fails ALL pending requests when the process dies.
``transcribe_fast`` (dictation, short audio) regains a real timeout:
on expiry the worker is killed and respawn is left to the caller's
WorkerCrashedError handling. ``transcribe`` (meetings) intentionally keeps
NO timeout — long meetings legitimately exceed any fixed cap (the old hard
timeout was removed in 4f3b307 for killing real transcriptions); worker
death is still detected immediately via the reader.
"""

import multiprocessing
import queue
import tempfile
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .logger import logger

if TYPE_CHECKING:
    import numpy as np


class WorkerCrashedError(RuntimeError):
    """Raised when the worker subprocess dies unexpectedly."""
    pass


def _reconstruct_error(response: dict) -> Exception:
    """Re-raise the correct exception type from a worker error response."""
    error_type = response.get("error_type", "Exception")
    message = response.get("message", "Unknown error")
    tb = response.get("traceback", "")
    if tb:
        logger.debug(f"Worker traceback:\n{tb}")
    if error_type == "PermissionError":
        return PermissionError(message)
    if error_type == "FileNotFoundError":
        return FileNotFoundError(message)
    return RuntimeError(f"[{error_type}] {message}")


class _PendingRequest:
    """A response slot one caller waits on; completed by the reader thread."""

    __slots__ = ("done", "response")

    def __init__(self):
        self.done = threading.Event()
        self.response: dict | None = None  # None after done => worker died


class _WorkerGeneration:
    """Per-spawn shared state, bound to exactly one reader thread.

    A restart creates a NEW generation; the old reader only ever touches
    its own generation's pending map and ready event, so a late-exiting
    old reader can never clobber the successor worker's state (Copilot
    finding on PR #146).
    """

    __slots__ = ("pending", "pending_lock", "ready_event", "ready_ok")

    def __init__(self):
        self.pending: dict[int, _PendingRequest] = {}
        self.pending_lock = threading.Lock()
        self.ready_event = threading.Event()
        self.ready_ok = False


class TranscriptionWorker:
    """Manages a long-lived transcription subprocess."""

    def __init__(self, cfg: dict, preload_model: str | None = None):
        self._cfg = cfg
        self._preload_model = preload_model
        self._process: multiprocessing.Process | None = None
        self._request_q = None
        self._response_q = None
        self._request_counter = 0
        self._lock = threading.Lock()
        self.gpu_name: str | None = None
        self.device: str | None = None
        # Reader-thread state (Phase 6): one _WorkerGeneration per spawn;
        # only that generation's bound reader completes its entries.
        self._gen = _WorkerGeneration()
        self._reader: threading.Thread | None = None

    def _next_id(self) -> int:
        with self._lock:
            self._request_counter += 1
            return self._request_counter

    def start(self) -> None:
        """Spawn the worker process and its response reader. Non-blocking."""
        # Deferred import: worker.py pulls numpy at module level, which is
        # only needed in the subprocess. Keeping it out of module scope
        # lets the protocol layer be unit-tested without heavy deps.
        from .worker import worker_main

        ctx = multiprocessing.get_context("spawn")
        self._request_q = ctx.Queue()
        self._response_q = ctx.Queue()
        # Fresh generation per spawn: the reader binds to THIS generation,
        # so a lingering previous reader cannot touch the new state.
        self._gen = _WorkerGeneration()
        # Snapshot at the pickle boundary: _cfg may be a live ConfigStore
        # (a Mapping holding a lock, which must not cross into the
        # subprocess). Snapshotting here also means a worker restart picks
        # up the latest settings, as the live-dict version did.
        cfg = self._cfg.snapshot() if hasattr(self._cfg, "snapshot") else dict(self._cfg)
        self._process = ctx.Process(
            target=worker_main,
            args=(self._request_q, self._response_q, cfg, self._preload_model),
            daemon=True,
        )
        self._process.start()
        self._reader = threading.Thread(
            target=self._reader_loop,
            args=(self._process, self._response_q, self._gen),
            daemon=True,
            name="worker-response-reader",
        )
        self._reader.start()
        logger.info(f"Worker process spawned (pid={self._process.pid})")

    # -- reader thread ------------------------------------------------------

    def _reader_loop(self, process, response_q, gen: _WorkerGeneration) -> None:
        """Single owner of the response queue for ONE worker generation.

        Routes responses to pending waiters by request_id; handles 'ready'
        and startup-error messages; on process death fails every pending
        request so no caller waits forever. All args (process, queue, and
        generation state) are bound at spawn, so a restart() creating a
        new generation never races this loop, and a late exit here can
        only touch THIS generation's state - never the successor's.
        """
        while True:
            alive = process.is_alive()
            try:
                msg = response_q.get(timeout=0.5)
            except queue.Empty:
                if not alive:
                    break  # drained after death
                continue
            except (EOFError, OSError, ValueError):
                # ValueError: multiprocessing.Queue raises it once the
                # queue is closed during shutdown/restart. Treat exactly
                # like process death so pending waiters are failed below
                # instead of stranded forever.
                break

            mtype = msg.get("type")
            if mtype == "ready":
                self.gpu_name = msg.get("gpu_name")
                self.device = msg.get("device")
                gen.ready_ok = True
                gen.ready_event.set()
                continue
            if msg.get("request_id") == "__init__":
                # Startup failure (e.g. model preload error)
                logger.error(f"Worker startup error: {msg.get('message')}")
                gen.ready_ok = False
                gen.ready_event.set()
                continue

            rid = msg.get("request_id")
            with gen.pending_lock:
                pending = gen.pending.pop(rid, None)
            if pending is not None:
                pending.response = msg
                pending.done.set()
            else:
                logger.debug("worker reader: dropping stale response id=%r", rid)

        # Process is dead and queue drained: fail everything outstanding
        # in THIS generation only.
        with gen.pending_lock:
            orphans = list(gen.pending.values())
            gen.pending.clear()
        for p in orphans:
            p.response = None
            p.done.set()
        # Unblock anyone still in wait_ready on this (dead) generation.
        gen.ready_event.set()

    # -- request plumbing ----------------------------------------------------

    def _request(self, payload: dict, timeout: float | None) -> dict:
        """Send a request and wait for its routed response.

        The caller's ``payload`` is never mutated; a copy gains the
        request_id (addressed in review: no externally visible side
        effects).

        timeout=None waits until the worker answers or dies (meeting
        path). With a timeout, expiry KILLS the worker (it is wedged but
        alive — unusable either way) and raises WorkerCrashedError so the
        caller's existing crash handling respawns it.
        """
        gen = self._gen
        request_id = self._next_id()
        pending = _PendingRequest()
        with gen.pending_lock:
            gen.pending[request_id] = pending
        # Copy: never mutate the caller's dict (it may be reused/logged).
        outgoing = {**payload, "request_id": request_id}  # caller dict untouched
        try:
            self._request_q.put(outgoing)
        except Exception:
            with gen.pending_lock:
                gen.pending.pop(request_id, None)
            raise

        if not pending.done.wait(timeout):
            with gen.pending_lock:
                gen.pending.pop(request_id, None)
            logger.error(
                "Worker request %s timed out after %ss; killing wedged worker",
                payload.get("type"), timeout,
            )
            try:
                if self._process is not None and self._process.is_alive():
                    self._process.kill()
                    # join reaps the killed child immediately (review:
                    # repeated timeouts must not accumulate zombies).
                    self._process.join(timeout=3)
            except Exception:
                pass
            raise WorkerCrashedError(
                f"Worker request timed out after {timeout}s (worker killed)"
            )

        if pending.response is None:
            raise WorkerCrashedError(
                f"Worker process died (exit code {self._exitcode()})"
            )
        return pending.response

    # -- public API -----------------------------------------------------------

    def wait_ready(self, timeout: float = 120) -> bool:
        """Block until worker reports models are loaded."""
        gen = self._gen
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if gen.ready_event.wait(timeout=1.0):
                return gen.ready_ok and self.is_alive()
            if not self.is_alive():
                logger.error(
                    f"Worker died during startup (exit code {self._exitcode()})"
                )
                return False
        logger.error("Worker startup timed out")
        return False

    def transcribe_fast(self, audio_np: "np.ndarray",
                        model_override: str | None = None,
                        timeout: float = 60) -> str:
        """Send dictation audio to worker, return transcribed text.

        Audio is transferred via a temp .npy file to avoid pickling large
        arrays. Dictations are short, so the timeout is real (Phase 6): a
        wedged worker is killed and WorkerCrashedError raised instead of
        hanging the dictation thread forever. The np.ndarray annotation
        is a TYPE_CHECKING forward reference (review: keep type safety
        without importing numpy at module scope).
        """
        import numpy as np

        # Save audio to temp file (NamedTemporaryFile avoids mktemp TOCTOU race)
        tmp_fd = tempfile.NamedTemporaryFile(suffix=".npy", prefix="ws_audio_", delete=False)
        tmp_path = Path(tmp_fd.name)
        tmp_fd.close()
        np.save(str(tmp_path), audio_np)

        try:
            response = self._request({
                "type": "transcribe_fast",
                "audio_path": str(tmp_path),
                "model": model_override,
            }, timeout=timeout)
        finally:
            tmp_path.unlink(missing_ok=True)

        if response["type"] == "error":
            raise _reconstruct_error(response)
        return response.get("text", "")

    def transcribe(self, audio_path: str, diarize: bool = False,
                   model_override: str | None = None,
                   diarize_method: str | None = None,
                   timeout: float | None = None) -> dict:
        """Send meeting audio to worker, return result dict.

        Args:
            diarize_method: Force a specific diarization method
                ("balanced_mix", "per_channel", "raw_audio"). None uses config.
            timeout: None (default) waits until the worker answers or
                dies — long meetings legitimately run for many minutes and
                a hard cap killed real transcriptions (see 4f3b307).
                Callers that know their audio is short (tests) may pass one.
        """
        response = self._request({
            "type": "transcribe",
            "audio_path": audio_path,
            "diarize": diarize,
            "model": model_override,
            "diarize_method": diarize_method,
        }, timeout=timeout)
        if response["type"] == "error":
            raise _reconstruct_error(response)
        return response.get("result", {})

    def reload_model(self, model_name: str, timeout: float = 120) -> bool:
        """Ask the worker to load a different model."""
        try:
            response = self._request({
                "type": "reload_model",
                "model": model_name,
            }, timeout=timeout)
            return response.get("type") == "model_loaded"
        except WorkerCrashedError:
            return False

    def is_alive(self) -> bool:
        return self._process is not None and self._process.is_alive()

    def is_ready(self) -> bool:
        return self._gen.ready_ok and self.is_alive()

    def update_config(self, cfg) -> None:
        """Rebind the config source used at the next worker spawn.

        Accepts the live ConfigStore (preferred - respawns then always
        see current settings; start() snapshots at the pickle boundary)
        or a plain dict to pin a frozen config, as the backup
        transcriber does.
        """
        self._cfg = cfg

    def restart(self) -> None:
        """Kill and respawn the worker (e.g., after a crash).

        Blocks until the new worker is ready. The old reader thread is
        bound to the old process/queue objects and exits on its own when
        the old process dies; pending requests against the old worker are
        failed by that reader, so no waiter leaks across the restart.
        """
        logger.info("Restarting transcription worker...")
        self.stop()
        self.start()
        if self.wait_ready(timeout=120):
            logger.info("Worker respawned and ready")
        else:
            logger.error("Worker failed to respawn")

    def stop(self) -> None:
        """Shut down the worker cleanly.

        On Windows, multiprocessing.spawn workers can survive as orphans if the
        parent dies uncleanly. We kill aggressively and wait to ensure CUDA/MKL
        memory is fully released before any respawn.
        """
        if self._process is None:
            return
        if self._process.is_alive():
            try:
                self._request_q.put({"type": "shutdown"})
                self._process.join(timeout=5)
            except Exception:
                pass
            if self._process.is_alive():
                self._process.kill()
                self._process.join(timeout=3)
        # Even if not alive, ensure the process object is reaped
        try:
            self._process.close()
        except (ValueError, AttributeError):
            pass
        self._process = None
        self._gen.ready_ok = False
        # Reader exits on its own after observing process death and
        # draining; it fails its own generation's pending waiters first
        # and cannot touch any successor generation's state.

    def _exitcode(self):
        return self._process.exitcode if self._process else None

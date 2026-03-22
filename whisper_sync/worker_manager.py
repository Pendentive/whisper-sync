"""TranscriptionWorker — manages a long-lived subprocess for crash-safe transcription.

The worker runs whisperX/CTranslate2/CUDA in isolation. If it segfaults,
the main process detects the death, logs it, and respawns automatically.
"""

import multiprocessing
import queue
import tempfile
import threading
import time
from pathlib import Path

import numpy as np

from .logger import logger
from .worker import worker_main


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


class TranscriptionWorker:
    """Manages a long-lived transcription subprocess."""

    def __init__(self, cfg: dict, preload_model: str | None = None):
        self._cfg = cfg
        self._preload_model = preload_model
        self._process: multiprocessing.Process | None = None
        self._request_q: multiprocessing.Queue | None = None
        self._response_q: multiprocessing.Queue | None = None
        self._ready = False
        self._request_counter = 0
        self._lock = threading.Lock()

    def update_config(self, cfg: dict) -> None:
        """Update the config snapshot used for future spawns."""
        self._cfg = cfg

    def _next_id(self) -> int:
        self._request_counter += 1
        return self._request_counter

    def start(self) -> None:
        """Spawn the worker process. Non-blocking."""
        ctx = multiprocessing.get_context("spawn")
        self._request_q = ctx.Queue()
        self._response_q = ctx.Queue()
        self._ready = False
        self._process = ctx.Process(
            target=worker_main,
            args=(self._request_q, self._response_q, self._cfg, self._preload_model),
            daemon=True,
        )
        self._process.start()
        logger.info(f"Worker process spawned (pid={self._process.pid})")

    def wait_ready(self, timeout: float = 120) -> bool:
        """Block until worker reports models are loaded."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.is_alive():
                logger.error(f"Worker died during startup (exit code {self._exitcode()})")
                return False
            try:
                msg = self._response_q.get(timeout=1.0)
                if msg.get("type") == "ready":
                    self._ready = True
                    return True
                if msg.get("type") == "error":
                    logger.error(f"Worker startup error: {msg.get('message')}")
                    return False
            except queue.Empty:
                continue
        logger.error("Worker startup timed out")
        return False

    def transcribe_fast(self, audio_np: np.ndarray, model_override: str | None = None,
                        timeout: float = 60) -> str:
        """Send dictation audio to worker, return transcribed text.

        Audio is transferred via a temp .npy file to avoid pickling large arrays.
        """
        # Save audio to temp file (NamedTemporaryFile avoids mktemp TOCTOU race)
        tmp_fd = tempfile.NamedTemporaryFile(suffix=".npy", prefix="ws_audio_", delete=False)
        tmp_path = Path(tmp_fd.name)
        tmp_fd.close()
        np.save(str(tmp_path), audio_np)

        try:
            request_id = self._next_id()
            self._request_q.put({
                "type": "transcribe_fast",
                "audio_path": str(tmp_path),
                "model": model_override,
                "request_id": request_id,
            })
            response = self._wait_response(request_id, timeout)
        finally:
            tmp_path.unlink(missing_ok=True)

        if response["type"] == "error":
            raise _reconstruct_error(response)
        return response.get("text", "")

    def transcribe(self, audio_path: str, diarize: bool = False,
                   model_override: str | None = None, timeout: float = 600) -> dict:
        """Send meeting audio to worker, return result dict."""
        request_id = self._next_id()
        self._request_q.put({
            "type": "transcribe",
            "audio_path": audio_path,
            "diarize": diarize,
            "model": model_override,
            "request_id": request_id,
        })
        response = self._wait_response(request_id, timeout)
        if response["type"] == "error":
            raise _reconstruct_error(response)
        return response.get("result", {})

    def reload_model(self, model_name: str, timeout: float = 120) -> bool:
        """Ask the worker to load a different model."""
        request_id = self._next_id()
        self._request_q.put({
            "type": "reload_model",
            "model": model_name,
            "request_id": request_id,
        })
        try:
            response = self._wait_response(request_id, timeout)
            return response.get("type") == "model_loaded"
        except (WorkerCrashedError, TimeoutError):
            return False

    def is_alive(self) -> bool:
        return self._process is not None and self._process.is_alive()

    def is_ready(self) -> bool:
        return self._ready and self.is_alive()

    def restart(self) -> None:
        """Kill and respawn the worker (e.g., after a crash).

        Blocks until the new worker is ready so that no concurrent
        ``_wait_response`` call can race with ``wait_ready`` on the
        response queue.
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
        self._ready = False

    def _wait_response(self, request_id: int, timeout: float) -> dict:
        """Wait for a response matching request_id. Raises on crash or timeout."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.is_alive():
                raise WorkerCrashedError(
                    f"Worker process died (exit code {self._exitcode()})"
                )
            try:
                msg = self._response_q.get(timeout=0.5)
                if msg.get("request_id") == request_id:
                    return msg
                # Handle "ready" messages that arrive while waiting for a response
                if msg.get("type") == "ready":
                    self._ready = True
                # Stale message from a previous request — discard
            except queue.Empty:
                continue
        raise TimeoutError(f"Transcription timed out after {timeout}s")

    def _exitcode(self) -> int | None:
        return self._process.exitcode if self._process else None

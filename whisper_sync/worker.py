"""Transcription worker — runs whisperX/CTranslate2/CUDA in an isolated subprocess.

If the worker segfaults (CTranslate2 crash, CUDA driver error), only this
process dies. The main tray/hotkey process survives and respawns automatically.

Meeting transcription uses a staged pipeline with inter-stage priority checks,
allowing dictation requests to be processed between meeting stages.
"""

import faulthandler
import os
import queue
import sys
import traceback
from pathlib import Path

import numpy as np


def _drain_priority(request_queue, response_queue, transcribe_fast_fn, preload_fn):
    """Process pending high-priority requests between meeting stages.

    Pulls all pending items from the queue. Dictation and model reload requests
    are processed immediately. Other requests (e.g. another meeting) are deferred
    back to the queue. Returns True if shutdown was requested.
    """
    deferred = []
    shutdown = False

    while True:
        try:
            req = request_queue.get_nowait()
        except queue.Empty:
            break

        req_type = req.get("type")
        req_id = req.get("request_id", "?")

        if req_type == "shutdown":
            shutdown = True
            break

        if req_type == "transcribe_fast":
            try:
                audio_np = np.load(req["audio_path"], allow_pickle=False)
                text = transcribe_fast_fn(audio_np, model_override=req.get("model"))
                response_queue.put({"type": "result", "text": text, "request_id": req_id})
            except Exception as e:
                response_queue.put({
                    "type": "error",
                    "error_type": type(e).__name__,
                    "message": str(e),
                    "traceback": traceback.format_exc(),
                    "request_id": req_id,
                })
            continue

        if req_type == "reload_model":
            try:
                preload_fn(model_name=req["model"])
                response_queue.put({"type": "model_loaded", "request_id": req_id})
            except Exception as e:
                response_queue.put({
                    "type": "error",
                    "error_type": type(e).__name__,
                    "message": str(e),
                    "request_id": req_id,
                })
            continue

        # Anything else (e.g. another transcribe) — defer until after current meeting
        deferred.append(req)

    for req in deferred:
        request_queue.put(req)

    return shutdown


def worker_main(request_queue, response_queue, cfg_snapshot: dict,
                preload_model_name: str | None = None):
    """Entry point for the transcription worker subprocess.

    Args:
        request_queue: multiprocessing.Queue for incoming requests.
        response_queue: multiprocessing.Queue for outgoing responses.
        cfg_snapshot: Frozen copy of the config dict at spawn time.
        preload_model_name: Model to preload at startup. None = skip preload
            (model loads on first request instead).
    """
    # Enable faulthandler in child so segfaults are logged to stderr
    faulthandler.enable()

    # Suppress known harmless warnings before any imports trigger them
    import warnings
    import logging
    # torchcodec not installed — we don't use it, pyannote complains anyway
    warnings.filterwarnings("ignore", message="torchcodec is not installed correctly",
                            category=UserWarning, module=r"pyannote\.audio\.core\.io")
    # TF32 disabled — pyannote's own ReproducibilityWarning, not a UserWarning
    warnings.filterwarnings("ignore", message="TensorFloat-32.*has been disabled",
                            module=r"pyannote\.audio\.utils\.reproducibility")
    # std() degrees of freedom — numerical edge case in short audio segments
    warnings.filterwarnings("ignore", message="std\\(\\): degrees of freedom is <= 0",
                            category=UserWarning)
    # Lightning checkpoint upgrade nag
    logging.getLogger("lightning.pytorch.utilities.migration.utils").setLevel(logging.ERROR)
    # whisperx verbose INFO messages (VAD, diarization loading)
    logging.getLogger("whisperx.vads.pyannote").setLevel(logging.WARNING)
    logging.getLogger("whisperx.diarize").setLevel(logging.WARNING)

    # Pin the config snapshot so transcribe.py's config.load() returns
    # the spawner's cfg (including backup device/model overrides) instead
    # of reading the user's config file from disk.
    from . import config as _config_mod
    _config_mod.override(cfg_snapshot)

    # Set model cache env vars before any torch/whisperx imports
    from .paths import get_model_cache
    _MODEL_CACHE = get_model_cache()
    os.environ["HF_HUB_CACHE"] = str(_MODEL_CACHE)
    os.environ["TORCH_HOME"] = str(_MODEL_CACHE / "torch")

    from .transcribe import (
        preload, transcribe_fast,
        stage_prepare, stage_transcribe, stage_align, stage_diarize, stage_finalize,
    )

    # Preload model if specified
    if preload_model_name is not None:
        try:
            preload(model_name=preload_model_name)
        except Exception as e:
            response_queue.put({
                "type": "error",
                "error_type": type(e).__name__,
                "message": str(e),
                "traceback": traceback.format_exc(),
                "request_id": "__init__",
            })
            return

    response_queue.put({"type": "ready"})

    def _check_priority():
        """Check for priority requests. Returns True if shutdown requested."""
        return _drain_priority(request_queue, response_queue, transcribe_fast, preload)

    # Main request loop
    while True:
        try:
            request = request_queue.get()
        except (EOFError, OSError):
            break

        req_type = request.get("type")
        req_id = request.get("request_id", "?")

        if req_type == "shutdown":
            break

        if req_type == "reload_model":
            try:
                preload(model_name=request["model"])
                response_queue.put({"type": "model_loaded", "request_id": req_id})
            except Exception as e:
                response_queue.put({
                    "type": "error",
                    "error_type": type(e).__name__,
                    "message": str(e),
                    "request_id": req_id,
                })
            continue

        try:
            if req_type == "transcribe_fast":
                audio_np = np.load(request["audio_path"], allow_pickle=False)
                text = transcribe_fast(audio_np, model_override=request.get("model"))
                response_queue.put({
                    "type": "result",
                    "text": text,
                    "request_id": req_id,
                })

            elif req_type == "transcribe":
                # Staged pipeline with inter-stage priority checks
                ctx = stage_prepare(
                    request["audio_path"],
                    model_override=request.get("model"),
                )
                if _check_priority():
                    response_queue.put({
                        "type": "error",
                        "error_type": "Cancelled",
                        "message": "Shutdown requested during transcription",
                        "request_id": req_id,
                    })
                    break

                result = stage_transcribe(ctx)
                if _check_priority():
                    response_queue.put({
                        "type": "error",
                        "error_type": "Cancelled",
                        "message": "Shutdown requested during transcription",
                        "request_id": req_id,
                    })
                    break

                result = stage_align(ctx, result)
                if _check_priority():
                    response_queue.put({
                        "type": "error",
                        "error_type": "Cancelled",
                        "message": "Shutdown requested during transcription",
                        "request_id": req_id,
                    })
                    break

                diarize_segments = None
                if request.get("diarize", False):
                    diarize_segments = stage_diarize(
                        ctx, force_method=request.get("diarize_method"),
                    )
                    if _check_priority():
                        response_queue.put({
                            "type": "error",
                            "error_type": "Cancelled",
                            "message": "Shutdown requested during transcription",
                            "request_id": req_id,
                        })
                        break

                output = stage_finalize(ctx, result, diarize_segments)
                response_queue.put({
                    "type": "result",
                    "result": output,
                    "request_id": req_id,
                })

            else:
                response_queue.put({
                    "type": "error",
                    "error_type": "ValueError",
                    "message": f"Unknown request type: {req_type}",
                    "request_id": req_id,
                })

        except PermissionError as e:
            response_queue.put({
                "type": "error",
                "error_type": "PermissionError",
                "message": str(e),
                "request_id": req_id,
            })
        except FileNotFoundError as e:
            response_queue.put({
                "type": "error",
                "error_type": "FileNotFoundError",
                "message": str(e),
                "request_id": req_id,
            })
        except Exception as e:
            response_queue.put({
                "type": "error",
                "error_type": type(e).__name__,
                "message": str(e),
                "traceback": traceback.format_exc(),
                "request_id": req_id,
            })

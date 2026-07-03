"""Vendor-agnostic GPU memory probes.

The GPU Guard (gpu_guard.py) needs device-wide free VRAM from the MAIN
process, which deliberately has no torch/CUDA context (transcription
lives in the worker subprocess). Probes are small callables returning
``ProbeResult(total_mb, free_mb)`` or ``None`` on failure, registered by
name so a new GPU vendor is a new provider function here - never edits
scattered through the app (2026-07-03 gpu-guard spec, design principle 2).

Providers:
- ``pynvml``: NVIDIA NVML bindings if the package happens to be
  installed. No CUDA context required, ~zero cost per call.
- ``nvidia-smi``: subprocess query against the driver's CLI. Universally
  present with any NVIDIA driver install; ~100ms per call. The default
  on NVIDIA machines.
- (future) an Intel/AMD provider slots in as another function.

``get_probe()`` auto-selects the first available provider and is called
once at guard startup; per-poll calls go through the returned callable.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Callable, NamedTuple, Optional

from .logger import logger


class ProbeResult(NamedTuple):
    total_mb: int
    free_mb: int


ProbeFn = Callable[[], Optional[ProbeResult]]


def _pynvml_available() -> ProbeFn | None:
    try:
        import pynvml  # noqa: F401
    except ImportError:
        return None

    def _probe() -> ProbeResult | None:
        try:
            import pynvml
            pynvml.nvmlInit()
            try:
                handle = pynvml.nvmlDeviceGetHandleByIndex(0)
                mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
                return ProbeResult(int(mem.total // 1_048_576), int(mem.free // 1_048_576))
            finally:
                pynvml.nvmlShutdown()
        except Exception as exc:
            logger.debug(f"pynvml probe failed: {exc}")
            return None

    return _probe


def _nvidia_smi_available() -> ProbeFn | None:
    if shutil.which("nvidia-smi") is None:
        return None

    def _probe() -> ProbeResult | None:
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.total,memory.free",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if out.returncode != 0:
                logger.debug(f"nvidia-smi probe rc={out.returncode}: {out.stderr.strip()[:200]}")
                return None
            # First GPU only; multi-GPU machines report line per device.
            first = out.stdout.strip().splitlines()[0]
            total_s, free_s = (p.strip() for p in first.split(","))
            return ProbeResult(int(total_s), int(free_s))
        except (subprocess.TimeoutExpired, OSError, ValueError, IndexError) as exc:
            logger.debug(f"nvidia-smi probe failed: {exc}")
            return None

    return _probe


# Ordered registry: first available wins. A new vendor adds a pair here.
PROVIDERS: list[tuple[str, Callable[[], ProbeFn | None]]] = [
    ("pynvml", _pynvml_available),
    ("nvidia-smi", _nvidia_smi_available),
]


def get_probe(preferred: str | None = None) -> tuple[str, ProbeFn] | tuple[None, None]:
    """Select a probe provider. Returns (name, callable) or (None, None).

    ``preferred`` pins a specific provider by name (config escape hatch);
    otherwise the first available in PROVIDERS order wins.
    """
    for name, factory in PROVIDERS:
        if preferred is not None and name != preferred:
            continue
        probe = factory()
        if probe is not None:
            return name, probe
    return None, None

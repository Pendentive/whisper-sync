"""GPU Guard: VRAM watchdog with a sticky model-downgrade ladder.

One module owns the whole feature (2026-07-03 gpu-guard spec). The rest
of the app touches it through three seams: ``start()`` at app startup,
``effective_model()`` where transcription requests choose a model, and
``note_pressure_trigger()`` on worker crashes. Disabled (``gpu_guard``
config key false, or no probe provider on this machine) means inert:
no polling, ``effective_model`` is a pass-through.

Behavior (spec B1-B3):
- A scheduler tick offloads a VRAM probe onto the IO executor every
  ``gpu_guard_poll_seconds``. Free VRAM below ``gpu_guard_low_vram_mb``
  arms one downgrade step for the NEXT transcription request; an
  in-flight job is never killed for a watermark. Re-arming requires
  recovery above the watermark first (hysteresis), so a persistently
  low reading does not walk the ladder to the floor by itself.
- Worker crashes and OOM-retry exhaustion escalate immediately by one
  step per event.
- Downgrades are sticky for the session (the configured model returns
  on next app start), and every event appends a timestamped JSON line
  to ``gpu-guard.jsonl`` in the data dir - the artifact for correlating
  against Windows crash/BSOD times, and the machine surface for the
  API-first extension app.

Device failover (2026-07-04 voice-assistant-direction spec): hybrid
laptops can power the dGPU off mid-session. Three consecutive failed
probes - or a worker crash whose immediate probe fails - declare the
device lost: ``effective_model`` clamps to ``cpu_fallback_model`` and
``respawn_overlay()`` tells the respawn path to pin the next worker
spawn to cpu, so a dead GPU is never retried in a loop. A later
successful probe clears the state. Explicit ``device: cpu`` configs
never declare loss (the user never wanted cuda).
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .logger import logger

DEFAULT_LADDER = ["large-v3", "medium", "small", "base"]

# Consecutive failed polls before the device is declared lost. At the
# default 30s poll this is ~90s worst-case detection; a worker crash
# whose immediate probe fails short-circuits the wait entirely.
DEVICE_LOST_AFTER_FAILURES = 3


class GpuGuard:
    """Owns watermark monitoring, the downgrade ladder, and the event log."""

    def __init__(self, cfg, notify: Optional[Callable[[str, str], None]] = None,
                 probe=None, probe_name: str | None = None,
                 event_path: Path | None = None):
        self._cfg = cfg
        self._notify = notify or (lambda title, msg: None)
        self._lock = threading.Lock()
        self._level = 0            # rungs below the requested model
        self._armed_low = False    # hysteresis: True while below watermark
        self._last_free_mb: int | None = None
        self._device_lost = False  # dGPU unreachable; cpu failover active
        self._probe_fail_streak = 0
        self._probe = probe
        self._probe_name = probe_name
        self._event_path = event_path
        self._started = False

    # -- configuration ------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return bool(self._cfg.get("gpu_guard", True))

    def _ladder(self) -> list:
        """Validated ladder; malformed config falls back to the default."""
        ladder = self._cfg.get("gpu_guard_ladder")
        if (isinstance(ladder, (list, tuple)) and len(ladder) > 0
                and all(isinstance(m, str) and m for m in ladder)):
            return list(ladder)
        if ladder is not None:
            logger.warning(f"GPU guard: invalid gpu_guard_ladder {ladder!r}; using default")
        return list(DEFAULT_LADDER)

    def _watermark_mb(self) -> int:
        return int(self._cfg.get("gpu_guard_low_vram_mb", 750))

    def _fallback_model(self) -> str:
        """Validated cpu_fallback_model; malformed config falls back to base."""
        model = self._cfg.get("cpu_fallback_model", "base")
        if isinstance(model, str) and model:
            return model
        logger.warning(
            f"GPU guard: invalid cpu_fallback_model {model!r}; using 'base'")
        return "base"

    # -- lifecycle ------------------------------------------------------------

    def start(self, scheduler, io_executor) -> None:
        """Begin polling. No-op when disabled or no probe provider exists."""
        if self._started:
            return
        if not self.enabled:
            logger.info("GPU guard disabled by config")
            return
        if self._probe is None:
            from .vram_probe import get_probe
            self._probe_name, self._probe = get_probe(
                self._cfg.get("gpu_guard_probe") or None
            )
        if self._probe is None:
            # Mark started so repeated start() calls do not re-log or
            # re-probe; the guard is permanently inert this session.
            self._started = True
            logger.info("GPU guard: no VRAM probe provider on this machine; guard inactive")
            self._event("probe_unavailable")
            return
        poll = float(self._cfg.get("gpu_guard_poll_seconds", 30))
        # The probe (subprocess for nvidia-smi) must not run on the
        # scheduler thread (short-jobs contract): the tick only enqueues
        # onto IO, native-gauged so idle-GC sees the subprocess.
        scheduler.call_every(
            poll,
            lambda: io_executor.submit_native("gpu-probe", self.check_once),
            label="gpu-guard-poll",
        )
        logger.info(
            f"GPU guard active: provider={self._probe_name}, "
            f"watermark={self._watermark_mb()}MB free, poll={poll:.0f}s"
        )
        self._event("guard_started", provider=self._probe_name,
                    watermark_mb=self._watermark_mb())

    # -- monitoring -----------------------------------------------------------

    def check_once(self) -> None:
        """One probe + watermark evaluation. Runs on the IO executor."""
        result = self._probe() if self._probe else None
        self._observe_probe_result(result, source="poll")
        if result is None:
            return
        with self._lock:
            self._last_free_mb = result.free_mb
            watermark = self._watermark_mb()
            if result.free_mb < watermark and not self._armed_low:
                self._armed_low = True
                self._escalate_locked(
                    trigger="low_vram",
                    free_mb=result.free_mb, total_mb=result.total_mb,
                )
            elif result.free_mb >= watermark and self._armed_low:
                self._armed_low = False
                self._event("vram_recovered", free_mb=result.free_mb,
                            total_mb=result.total_mb, level=self._level)

    def _observe_probe_result(self, result, source: str) -> None:
        """Track device reachability from one probe outcome.

        A ProbeResult proves the GPU is reachable and clears any loss
        state. None counts one strike: DEVICE_LOST_AFTER_FAILURES
        consecutive poll strikes - or a single strike observed at a
        worker crash (any non-"poll" source) - declare the device lost.
        """
        with self._lock:
            if result is not None:
                self._probe_fail_streak = 0
                if self._device_lost:
                    self._device_lost = False
                    self._event("gpu_device_recovered", source=source,
                                free_mb=result.free_mb,
                                total_mb=result.total_mb)
                    logger.info("GPU guard: GPU reachable again")
                return
            if str(self._cfg.get("device", "auto")).lower() == "cpu":
                # The user never wanted cuda; nothing to fail over. Keep
                # the streak at zero so an explicit-cpu period does not
                # bank failures that would fire instantly after a later
                # switch to auto/cuda (review catch on this PR).
                self._probe_fail_streak = 0
                return
            self._probe_fail_streak += 1
            if self._device_lost:
                return
            from_crash = source != "poll"
            if (not from_crash
                    and self._probe_fail_streak < DEVICE_LOST_AFTER_FAILURES):
                return
            self._device_lost = True
            fallback = self._fallback_model()
            self._event("gpu_device_lost", source=source,
                        fail_streak=self._probe_fail_streak)
            logger.warning(
                f"GPU guard: GPU unreachable ({source}); failing over to "
                f"'{fallback}' on cpu until it returns"
            )
            self._notify(
                "WhisperSync switched to CPU",
                f"The GPU is not reachable; using '{fallback}' until it returns.",
            )

    def note_pressure_trigger(self, reason: str) -> None:
        """Escalate one rung immediately (worker crash, OOM exhaustion).

        Inert without a probe provider: on providerless machines the
        guard must never alter model selection (documented contract).

        Probes device reachability first: a crash WITH an unreachable
        GPU is the power-off signature on hybrid laptops and fails over
        to cpu instead of walking the ladder - the ladder assumes the
        GPU still exists.
        """
        if not self.enabled or self._probe is None:
            return
        # Probe outside the lock: nvidia-smi can take seconds.
        result = self._probe()
        self._observe_probe_result(result, source=reason)
        with self._lock:
            if self._device_lost:
                return  # cpu failover governs; ladder rungs are meaningless
            if result is not None:
                # The probe just ran; log the fresh reading instead of
                # the last poll's (which may be stale or None).
                self._last_free_mb = result.free_mb
            self._escalate_locked(
                trigger=reason,
                free_mb=self._last_free_mb,
                total_mb=result.total_mb if result is not None else None,
            )

    def _escalate_locked(self, trigger: str, free_mb, total_mb) -> None:
        ladder = self._ladder()
        if self._level >= len(ladder) - 1:
            self._event("ladder_floor", trigger=trigger, free_mb=free_mb,
                        total_mb=total_mb, level=self._level)
            return
        old = self._effective_locked(str(self._cfg.get("model", ladder[0])))
        self._level += 1
        new = self._effective_locked(str(self._cfg.get("model", ladder[0])))
        self._event("downgrade_armed", trigger=trigger, free_mb=free_mb,
                    total_mb=total_mb, level=self._level,
                    model_from=old, model_to=new)
        logger.warning(
            f"GPU guard: downgrade armed ({trigger}); next transcriptions "
            f"use '{new}' (level {self._level}, was '{old}')"
        )
        self._notify(
            "WhisperSync reduced its model",
            f"GPU pressure ({trigger}): using '{new}' until restart.",
        )

    # -- the model seam ---------------------------------------------------------

    def effective_model(self, requested: str) -> str:
        """The model transcription should actually use.

        Level 0 (or guard disabled) passes through. Otherwise the result
        is the LOWER of the requested model and the current ladder rung -
        a downgrade may never upgrade a caller that already asked for a
        small model. Models not on the ladder map to the current rung.

        While the device is lost the result is instead clamped to
        ``cpu_fallback_model``: the worker runs (or will respawn) on
        cpu, where the configured cuda-sized model would be unusably
        slow.
        """
        if not self.enabled or self._probe is None:
            return requested
        with self._lock:
            if self._device_lost:
                return self._clamp_locked(requested, self._fallback_model())
            return self._effective_locked(requested)

    def _effective_locked(self, requested: str) -> str:
        if self._level == 0:
            return requested
        ladder = self._ladder()
        rung_index = min(self._level, len(ladder) - 1)
        try:
            requested_index = ladder.index(requested)
        except ValueError:
            return ladder[rung_index]
        return ladder[max(requested_index, rung_index)]

    def _clamp_locked(self, requested: str, ceiling: str) -> str:
        """The smaller of requested and ceiling by ladder position.

        A failover may never upgrade a caller that already asked for a
        smaller model; models not on the ladder resolve to the ceiling.
        """
        ladder = self._ladder()
        try:
            ceiling_index = ladder.index(ceiling)
        except ValueError:
            return ceiling
        try:
            requested_index = ladder.index(requested)
        except ValueError:
            return ceiling
        return ladder[max(requested_index, ceiling_index)]

    @property
    def device_lost(self) -> bool:
        """True while the dGPU is unreachable and cpu failover governs."""
        with self._lock:
            return self._device_lost

    def respawn_overlay(self) -> dict | None:
        """Config overlay for the NEXT worker spawn, or None for live config.

        While the device is lost every spawn must be pinned to cpu with
        the fallback model - a live-config respawn would retry CUDA in
        a loop (spec: forbidden). The caller merges this over a config
        snapshot and hands it to ``worker.update_config()`` before
        restarting (the backup_worker pinned-dict precedent).
        """
        if not self.enabled or self._probe is None:
            return None
        with self._lock:
            if not self._device_lost:
                return None
            fallback = self._fallback_model()
            return {
                "device": "cpu",
                "model": fallback,
                "dictation_model": fallback,
                "compute_type": "int8",
            }

    def log_external_event(self, event: str, **fields) -> None:
        """Append a non-guard subsystem event to gpu-guard.jsonl.

        Power transitions land here (hardware-resilience spec H1) so one
        timeline holds everything needed to correlate GPU pressure and
        sleep/wake against Windows crash times. Logged regardless of the
        guard toggle - the timeline is diagnostic, not behavioral.
        """
        self._event(event, **fields)

    def status(self) -> dict:
        with self._lock:
            return {
                "enabled": self.enabled,
                "provider": self._probe_name,
                "level": self._level,
                "last_free_mb": self._last_free_mb,
                "device_lost": self._device_lost,
            }

    # -- event log ---------------------------------------------------------------

    def _event(self, event: str, **fields) -> None:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **{k: v for k, v in fields.items() if v is not None},
        }
        try:
            path = self._event_path or self._default_event_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        except OSError as exc:
            logger.warning(f"GPU guard: could not write event log: {exc}")

    @staticmethod
    def _default_event_path() -> Path:
        from .paths import get_data_dir
        return get_data_dir() / "gpu-guard.jsonl"

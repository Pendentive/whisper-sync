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
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .logger import logger

DEFAULT_LADDER = ["large-v3", "medium", "small", "base"]


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
        if result is None:
            return  # transient probe failure; next poll retries
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

    def note_pressure_trigger(self, reason: str) -> None:
        """Escalate one rung immediately (worker crash, OOM exhaustion).

        Inert without a probe provider: on providerless machines the
        guard must never alter model selection (documented contract).
        """
        if not self.enabled or self._probe is None:
            return
        with self._lock:
            self._escalate_locked(trigger=reason,
                                  free_mb=self._last_free_mb, total_mb=None)

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
        """
        if not self.enabled or self._probe is None:
            return requested
        with self._lock:
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

    def status(self) -> dict:
        with self._lock:
            return {
                "enabled": self.enabled,
                "provider": self._probe_name,
                "level": self._level,
                "last_free_mb": self._last_free_mb,
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

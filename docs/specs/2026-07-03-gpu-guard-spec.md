# Spec: GPU Guard - modular, toggleable GPU/VRAM protection

Status: PROPOSED (derived from docs/specs/2026-07-03-owner-directive-verbatim.md)
Owner concern: the machine suffered a deterministic NVIDIA driver
divide-by-zero crash storm (see the directive doc for case facts); this
app's GPU workload is the suspected trigger. Whether or not the driver is
at fault, the app must protect the system, and the protection must be a
feature that can be turned on/off and ported across GPU vendors without
scattering vendor-specific lines through the codebase.

## Current state (from the 2026-07-03 hardware audit)

- One static VRAM read at model load picks a batch-size tier
  (transcribe.py); there is NO runtime VRAM monitoring anywhere.
- OOM handling is a string-match retry with batch halving inside the
  worker; it never falls back to a smaller model or CPU, and non-OOM CUDA
  errors just re-raise.
- The backup worker is a concurrency feature (dictation during meeting
  transcription), not a GPU-failure fallback.
- No single-instance guard exists in the app; only start.ps1 kills
  duplicates and orphan CUDA workers, so updater restarts or direct
  launches can double VRAM load, and orphans persist until the next
  launcher run.
- Worker restarts fully tear down and recreate the CUDA context; crash
  recovery already restarts the worker on the same (possibly failing)
  model.

## Design principles

1. **One module owns the feature**: `whisper_sync/gpu_guard.py`. The rest
   of the app interacts with it through at most three seams: startup
   wiring, the worker-spawn path (model selection), and the event log.
   Disabled means inert: no probes run, no imports of vendor libraries.
2. **Vendor abstraction**: a `VramProbe` provider interface returning
   `(total_mb, free_mb) | None`. Providers registered by name:
   `nvml` (pynvml, no CUDA context needed, reads device-wide free VRAM),
   `nvidia-smi` (subprocess fallback), `none`. An Intel/AMD port is a new
   provider file, not edits across the app.
3. **Feature toggle as config**: flat keys in config.defaults.json +
   `_VALID_KEYS`:
   - `gpu_guard` (bool, default true)
   - `gpu_guard_low_vram_mb` (int, default 750)
   - `gpu_guard_poll_seconds` (int, default 30; scheduler job, not a thread)
   - `gpu_guard_ladder` (list, default ["large-v3", "medium", "small", "base"])
   - `gpu_guard_allow_cpu_floor` (bool, default true; CPU int8 as final rung)
   - `gpu_guard_single_instance` (bool, default true)

## Behaviors

### B1. VRAM watermark monitor
A scheduler job samples the active probe every poll interval while the
worker is alive. Below `gpu_guard_low_vram_mb` free: log a WARN event,
emit a state event, and arm a downgrade for the next transcription
request (do not kill an in-flight job for a watermark alone).

### B2. Model downgrade ladder with handoff
Triggers: (a) armed watermark breach, (b) worker OOM retries exhausted,
(c) WorkerCrashedError during transcription. Action: select the next
model down the ladder, restart the worker on it (existing restart path
already tears down the CUDA context cleanly), and re-submit the job.
Handoff is safe because audio is disk-first: meetings always (streaming
WAV), dictation unless incognito (see companion hardware-resilience spec
for the overlay-dictation disk gap that must close for full coverage).
Downgrades are sticky for the session; the tray shows the active model;
recovery to the configured model happens on next app start (keep v1
simple, no auto re-upgrade).

### B3. Downgrade/pressure event log (crash correlation + API surface)
Every guard event (watermark breach, downgrade, probe failure, instance
rejection) appends a timestamped JSON line to `gpu-guard.jsonl` in the
data folder: `{ts, event, model_from, model_to, trigger, free_mb,
total_mb, provider}`. This is the artifact the owner correlates against
Windows Event Log / BSOD times, and the machine-readable surface the
API-first extension app consumes. Also mirrored as StateManager events
so the tray can toast on downgrade.

### B4. Single-instance guard (in-app)
At startup, acquire a named Win32 mutex (`Global\WhisperSyncV1`). If
already held: log, toast "WhisperSync is already running", exit 0.
Additionally, on startup sweep for orphan `multiprocessing.spawn` workers
from dead parents (port the start.ps1 logic into the app) so orphaned
CUDA contexts do not survive a parent crash until the next launcher run.

## Explicit non-goals (v1)
- No GPU temperature/utilization sampling (free-VRAM only).
- No automatic re-upgrade mid-session.
- No attempt to detect the specific nvlddmkm bug; the guard reduces GPU
  pressure generally and produces the correlation data to prove or clear
  this app as the trigger.

## Test plan
- Unit: ladder selection, sticky downgrade, event-log schema, config
  gating (guard off = no probe calls), probe registry with a fake
  provider, mutex acquire/reject with a fake mutex seam.
- Integration: fake probe forcing a watermark breach mid-session, assert
  next transcription runs on the downgraded model and the event log
  records the handoff; WS_E2E variant on the real worker with the ladder
  forced to a small model.
- Manual (testing/manual-checklist.md): two-launch rejection, downgrade
  toast visible, gpu-guard.jsonl entries parse.

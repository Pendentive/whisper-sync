# Assistant Build Round - 2026-07-04

> Status: ACTIVE
> Intake: docs/specs/2026-07-04-voice-assistant-direction.md (owner
> decisions, GPU power-state failover requirement, hardware research,
> build order; merged via #176-#178).
> This doc is the state surface for the round: the table below is
> updated as each step ships, and the progress log records results and
> decisions. Any session (or person) can resume the round from this
> file alone.

## Build order status

| # | Step | Spec anchor | Status |
|---|------|-------------|--------|
| 1 | GPU power-state failover (guard-owned, cpu_fallback_model) | GPU power-state resilience | IN PROGRESS |
| 2 | Tier 0+1 always-on listener (VAD + openWakeWord, off by default) | Staged architecture, step 2 | NEEDS OWNER INPUT |
| 3 | Tier 2 splice: wake -> ring-buffer-prefixed dictation + outro | Staged architecture, step 2 | QUEUED |
| 4 | Per-app meeting auto-record (WASAPI session watch + app list) | Staged architecture, step 3 | QUEUED |
| 5 | In-app wake-word trainer (verifier first, full training later) | Wake-word model landscape | QUEUED |
| 6 | NPU/OpenVINO backup-transcriber backend (committed scope) | NPU section | QUEUED |
| - | Installer refresh (screens generated from docs/features/) | BACKLOG.md | QUEUED |

Standing authorization (owner, 2026-07-03/04): proceed step to step
without asking unless a large unforeseen blocker. Before steps 2-3 the
owner still owes: wake + outro phrase choices, whisper-mode interaction,
and the command allowlist shape.

## Step 1 plan - GPU power-state failover

Requirement (spec, owner: "actually really important"): a
hybrid-graphics laptop can power the dGPU off mid-session. The app must
never crash or hang, must never retry CUDA in a loop when the GPU is
gone, must respawn the worker on device=cpu with a new
cpu_fallback_model key, toast + log the failover to gpu-guard.jsonl,
and notify when the dGPU returns. GPU Guard owns the decision (the
"CPU floor" ladder deferral becomes this requirement).

Findings that shape the design (2026-07-04 code read):

- Device resolution (auto -> cuda/cpu) and the cpu -> int8 compute map
  already live in the worker (transcribe._get_device and _get_model);
  a respawned worker on a GPU-less machine already lands on cpu. The
  gaps are decisional: vram_probe failures are silently ignored
  (check_once returns on None), effective_model() keeps requesting
  large-v3 (which a cpu worker would then load), and every respawn
  site calls worker.restart() with the same config - the forbidden
  CUDA-retry loop.
- worker_manager needs NO changes: update_config() already accepts a
  pinned dict snapshot (backup_worker precedent) and restart() is
  stop + start + wait_ready. The protected paths (worker_manager.py,
  worker.py, transcribe.py) should stay untouched for this step.
- Respawn sites to reroute: dictation_flow (crash handler),
  meeting_flow (crash handler), meeting_job (dead worker before
  transcribe), __main__ resume-check, tray_menu manual restart,
  auto_sleep.wake.

PRs:

- **PR A - guard failover core**: probe fail-streak detection (3
  consecutive failed polls) plus a crash-time immediate probe inside
  note_pressure_trigger -> device_lost state; effective_model() clamps
  to cpu_fallback_model while lost (never upgrades a smaller request);
  respawn_overlay() seam returning the pinned cpu config for the next
  spawn; gpu_device_lost / gpu_device_recovered events in
  gpu-guard.jsonl; failover toast; new config key cpu_fallback_model
  (defaults + _VALID_KEYS + features docs). No call-site changes yet.
  Explicit device=cpu configs never declare loss (the user never
  wanted cuda).
- **PR B - respawn routing**: app.restart_worker(reason) consults
  respawn_overlay(); all six respawn sites route through it; while
  lost, every spawn is pinned to cpu + fallback model; on healthy
  probes the live ConfigStore is restored. WS_E2E=1 run (respawn
  semantics change).
- **PR C - recovery + startup**: automatic switch-back when the dGPU
  returns and the app is idle (toast; auto-sleep already protects
  gaming VRAM); a startup probe so booting with the dGPU off pins the
  first spawn instead of loading large-v3 on cpu; features.md prose;
  BACKLOG deferral note for WM_DEVICECHANGE.

Deliberate deferral: WM_DEVICECHANGE removal events need a hidden
top-level window + message pump (message-only windows do not receive
broadcasts, same constraint as power_events.py). Probe streak +
crash-time probe covers detection within one poll interval (default
30s) and instantly on any crash, which is when detection matters.
Recorded in BACKLOG.md in PR C.

## Progress log

- **2026-07-04 (round start)**: Handoff consumed; #178 verified merged;
  dev at 6e9151f. Round opened with step 1 (GPU power-state failover).
  Owner reminder outstanding: the tray app still runs pre-#167
  bytecode and needs a restart to pick up everything from #167 through
  auto-sleep.

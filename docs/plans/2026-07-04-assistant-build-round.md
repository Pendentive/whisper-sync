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
| 1 | GPU power-state failover (guard-owned, cpu_fallback_model) | GPU power-state resilience | MERGED (#180-#182) |
| 2 | Tier 0+1 always-on listener (VAD + openWakeWord, off by default) | Staged architecture, step 2 | NEEDS OWNER INPUT |
| 3 | Tier 2 splice: wake -> ring-buffer-prefixed dictation + outro | Staged architecture, step 2 | QUEUED |
| 4 | Per-app meeting auto-record (mic consent-store watch + app list) | Staged architecture, step 3 | MERGED (#183, #184) |
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

## Step 4 plan - per-app meeting auto-record

Requirement (spec, owner second intake): a configurable app list
(Zoom, Slack huddles, Teams, ...); when a watched app is in a call,
recording auto-starts. Manual trigger stays. Steps 2-3 being blocked
on owner input, step 4 has no dependency on the listener and proceeds
first (standing authorization).

DELIBERATE DEVIATION from the spec's mechanism sketch: detection reads
the Windows CapabilityAccessManager mic consent store (stdlib winreg)
instead of WASAPI session enumeration. Rationale: session enumeration
needs pycaw/comtypes (new dependencies) and primarily sees RENDER
sessions (a notification sound would look like a call); the consent
store is the OS's own mic-in-use indicator - capture-specific, zero
dependencies, poll-cheap. Verified live on the dev machine 2026-07-04,
which also surfaced the stale-entry hazard (a dead Slack version's
entry stuck active) that dictates transition-only triggering.

PRs:

- **PR 1 - watcher module**: meeting_watch.py (consent-store probe,
  per-entry transition detection with 2-poll debounce, auto-start via
  meetings.toggle under the app lock, auto-stop only for auto-started
  recordings after sustained release, disabled-state reset), four
  config keys, wiring in __main__, features/defaults docs, fake-probe
  test suite.
- **PR 2 - tray surface**: settings toggle + watched-apps visibility
  in the tray menu, manual-validation notes. Browser/Meet detection
  stays out (any tab's audio would trigger); revisit with the
  listener's voice command ("record the meeting") or a window-title
  heuristic later.

## Progress log

- **2026-07-04 (round start)**: Handoff consumed; #178 verified merged;
  dev at 6e9151f. Round opened with step 1 (GPU power-state failover).
  Owner reminder outstanding: the tray app still runs pre-#167
  bytecode and needs a restart to pick up everything from #167 through
  auto-sleep.

- **2026-07-04 (step 1 PR A merged, #180)**: guard failover core as
  planned. Review caught two real issues, both fixed with regression
  tests: explicit-cpu periods were banking probe failures (a later
  switch to auto would declare loss on ONE failure, bypassing the
  3-failure rule - streak now resets at the cpu gate), and crash
  events logged stale VRAM readings despite a fresh probe having just
  run. 13 failover tests; suite 303.

- **2026-07-04 (step 1 PR B)**: respawn routing.
  AppControl.restart_worker(reason) is the single guard-aware respawn
  path: consults respawn_overlay(), pins the spawn to a
  cpu+fallback-model snapshot while the device is lost (logging
  worker_respawn_pinned_cpu to the timeline), rebinds the live
  ConfigStore otherwise. All five generic respawn sites rerouted
  (dictation crash, meeting crash, meeting_job pre-transcribe,
  suspend/resume check, auto-sleep wake). tray_menu's manual device
  switch deliberately keeps its own restart: an explicit user choice
  must not be overridden by the overlay. Protected paths untouched;
  WS_E2E run against the real worker (999s, OK - slow because the
  owner was gaming on the GPU at the time; future E2E runs get a
  warning first, or the CPU variant when the change is
  device-agnostic).

- **2026-07-04 (step 1 PR C)**: recovery + startup. guard.prime() runs
  one synchronous probe before the first worker spawn, so booting with
  the dGPU off pins the first spawn to cpu + fallback (startup
  counterpart of the failover). On a lost -> recovered probe
  transition the guard fires on_device_recovered (outside its lock);
  AppControl.schedule_gpu_switchback restarts a pinned-cpu worker onto
  the live config once idle - retrying each minute while busy,
  aborting if the GPU is lost again, skipping while asleep (never grab
  VRAM back mid-game) or when no pinned respawn ever happened.
  auto_sleep._busy extracted to module-level app_busy() and shared.
  BACKLOG: CPU-floor entry removed (shipped); WM_DEVICECHANGE instant
  detection recorded as the deliberate deferral.

- **2026-07-04 (step 4 PR 1 merged, #183)**: meeting_watch.py shipped
  per the step 4 plan. Review caught three real issues, all fixed with
  regression tests: round() could stop an auto-started recording
  before the configured release window (now math.ceil); the baseline
  only covered watch-listed entries, so editing meeting_watch_apps
  mid-session could promote a stale always-active entry into a fake
  transition (baseline now covers ALL entries, filter applies at the
  trigger decision); and a busy app at the debounce moment lost the
  meeting permanently (start now retries every poll while the mic
  stays held, via a handled-set). Suite 342. Session lesson recorded:
  verify suite exit codes DIRECTLY, never through a tail pipe (a
  test-harness infinite loop hid behind tail's exit 0 for a while).

- **2026-07-04 (step 4 PR 2)**: tray surface. Settings > Meeting
  Auto-Record submenu: Enabled checkbox (_toggle_meeting_auto_record;
  the always-registered poll reads the flag per tick and re-seeds its
  baseline on re-enable) plus a disabled info line listing the watched
  apps. Two pre-existing pyflakes nits fixed in the touched files.
  Manual validation for the owner: enable the toggle, join any Zoom or
  Slack call, expect the start toast within ~10s (two 5s polls) and
  the stop + save dialog ~30s after leaving.

- **2026-07-04 (step 4 complete, #184 merged)**: clean first review
  (the first of the round). Step 4 shipped as #183 + #184. Remaining
  unblocked work: step 6 (NPU/OpenVINO backup-transcriber backend,
  committed scope) - sized for a fresh session (new backend, venv
  dependencies, quality/latency benchmark gate). Steps 2-3 still wait
  on the owner: wake phrase, outro phrase(s), whisper-mode listener
  behavior (asked again 2026-07-04). Owner reminder still outstanding:
  the tray app runs pre-#167 bytecode; a restart picks up everything
  from #167 through failover, auto-sleep, and meeting auto-record.

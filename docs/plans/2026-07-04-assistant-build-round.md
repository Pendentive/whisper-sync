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
| 2 | Tier 0+1 always-on listener (VAD + openWakeWord, off by default) | Staged architecture, step 2 | POC MERGED (#187, pretrained phrase) |
| 3 | Tier 2 splice: wake -> ring-buffer-prefixed dictation + outro | Staged architecture, step 2 | MERGED (#189, #190) |
| 4 | Per-app meeting auto-record (mic consent-store watch + app list) | Staged architecture, step 3 | MERGED (#183, #184) |
| 5 | Self-serve phrase manager (typed phrases, background training, active checkmarks) | Owner decisions, fourth intake: the phrase manager | QUEUED |
| 6 | NPU/OpenVINO backup-transcriber backend (committed scope) | NPU section | QUEUED |
| - | Installer refresh (screens generated from docs/features/) | BACKLOG.md | QUEUED |

Standing authorization (owner, 2026-07-03/04): proceed step to step
without asking unless a large unforeseen blocker. Third intake
(2026-07-04): the listener POC is GO with a pretrained phrase. Fourth
intake: custom wake/outro phrase strings arrive as TYPED text through
the step 5 phrase manager (never ask the owner to speak or pre-pick
them); still open are the final whisper-mode decision (default until
then: listener off in whisper mode) and the command allowlist shape
(tier-2 command routing, later round).

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

## Step 3 plan - tier-2 splice

Requirement (spec + handoff design anchors): a wake detection must
become a hands-free dictation with no syllables lost, ended by an
outro phrase or silence.

- **PR 1 - splice core**: the listener keeps a rolling ~2.5s ring
  buffer of int16 frames (fed only while inference runs; cleared on
  pause entry so recordings and whisper-mode speech never cross into
  it). On wake it hands the assembled float32 prefix to
  DictationFlow.begin_via_wake, which starts a NORMAL disk-first
  dictation with the prefix seeded ahead of live capture in both sinks
  (RAM accumulator and crash-safety WAV - both seams are ordered
  before the callback can write, so no locking against the audio
  thread). The spoken wake phrase rides along in the prefix;
  strip_leading_phrase removes it (and any ring lead-in before it)
  from the transcription. Busy states refuse with a yellow flash; a
  sleeping model is woken and recording proceeds while it loads.
  Opportunistic fix: AudioRecorder.start() now resets a stale
  _disk_only flag left by a disk-streamed meeting (an incognito
  dictation after such a meeting used to capture nothing).
- **PR 2 - outro + silence stop**: keep listening during a
  wake-initiated dictation; end it on an outro phrase (second
  openWakeWord model in the same score dict) or a sustained-silence
  fallback, whichever is configured.

Known POC gaps (deliberate): audio between detection and the dictation
mic opening (~0.1-0.3s, usually the natural pause after the phrase) is
not captured - a seamless handoff needs a listener-to-recorder stream
handover and is not worth it before the custom-phrase trainer. The
overlay path (dictation during meeting recording/transcription) is not
spliced; wakes during meetings refuse politely.

## Step 5 plan - self-serve phrase manager

Requirement (spec, fourth intake): typed phrase strings, background
dGPU training with menu status/ETA, saved phrases with active
checkmarks, multiple phrases at once, reset - never ask the owner to
say anything out loud.

Training pipeline findings (2026-07-04, verified against the
installed openwakeword package):

- openwakeword ships the official trainer:

  ```
  python -m openwakeword.train --training_config <yaml> --generate_clips --augment_clips --train_model
  ```

  The YAML carries target_phrase, model_name, n_samples,
  piper_sample_generator_path, rir_paths, background_paths,
  feature_data_files, false_positive_validation_data_path, steps,
  output_dir (full key list read from train.py).
- Its dependency stack is NOT in whisper-env and must stay out of it:
  torch (CUDA), torchinfo, torchmetrics, plus a piper-sample-generator
  checkout with its libritts TTS checkpoint. Plan: a separate trainer
  venv created by a one-time guided setup step.
- One-time dataset downloads on the order of 10-20 GB (room impulse
  responses, background noise, precomputed negative features,
  false-positive validation corpus). The setup step needs an explicit
  size warning and a resumable downloader.
- Training runs tens of minutes on the dGPU. It must be a background
  job with tray status/ETA, and it must respect the GPU protocols:
  refuse to start while the app is busy or asleep-for-gaming, and warn
  the owner before any run (feedback_warn-before-gpu-heavy-tests).
- Output: a .onnx per phrase in a phrases directory + a saved-phrase
  registry in config (name -> model path + active flag + role
  wake/outro). The listener already routes a multi-model score dict
  (#190), so activating N phrases is just loading N models.

PR sketch: **PR A** trainer venv + dataset setup script and a headless
CLI that trains ONE phrase end to end on the dev machine (validates
the whole pipeline before any UI exists). **PR B** phrase registry +
listener loading of all active phrase models + tray Saved Phrases
surface (checkmarks, roles). **PR C** Set Phrase dialog + background
training job with menu status/ETA + completion toast.

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

- **2026-07-04 (third intake shipped, #186)**: auto-record settings
  redesign per the owner's third intake - Record/Ask/Ignore per app
  (the 3-state resolves the exhausting-Discord case), "Detect apps..."
  populating from the consent store (new apps arrive as Ignore),
  opt-in/opt-out toasts with master + sub-toggles, and
  MeetingFlow.abort_recording for the silent "Don't record" discard.
  Review caught a misleading menu label (said stop, does discard).
  Owner's streaming-dictation idea recorded as a BACKLOG deferral.
  Suite 357.

- **2026-07-04 (step 2 POC)**: wake-word listener shipped as a POC per
  the third-intake GO. listener.py: an always-on SHARED mic stream
  (sounddevice/WASAPI, never exclusive) feeds 80ms frames to
  openWakeWord (onnx, built-in silero VAD gate) on a daemon thread;
  detection wakes the model (auto_sleep.wake) and toasts - the tier-2
  dictation splice is next. Pretrained "hey_jarvis" placeholder until
  the owner picks custom phrases; pauses in whisper mode (owner
  default) and while recording; 3s refractory so one utterance fires
  once; RAM-only until wake. openwakeword added to requirements
  (lazy-imported; the module is inert without it - CI never sees it).
  Verified live in the venv: model download + load + predict on the
  dev machine. Config: wake_listener (off), wake_phrase_model,
  wake_threshold; tray toggle under Settings.

- **2026-07-04 (step 3 PR 1)**: tier-2 splice core per the step 3 plan.
  A wake now starts a real dictation: ring-buffer prefix into both
  recorder sinks, wake-phrase strip on the stop path (window-bounded,
  wake sessions only - a mid-sentence mention of the phrase in a
  normal dictation is never touched), begin_via_wake busy/sleep
  semantics mirroring the hotkey toggle, and the stale _disk_only fix
  with regression tests. The listener frame loop was refactored into
  handle_frame() so ring gating and pause-latch behavior are
  unit-tested without audio.

- **2026-07-04 (step 3 PR 2)**: hands-free stop per the step 3 plan.
  During a wake-initiated dictation the listener keeps running
  (handle_frame routes on the session flag before the pause gate) and
  ends the dictation on the outro phrase (wake_outro_model loads into
  the SAME openWakeWord model; its score key never fires a wake, and a
  2s grace window keeps a wake-alike outro from ending the session it
  started) or on sustained silence (wake_silence_stop_s, default 8s,
  read from the silero VAD scores the model already computes; a
  missing VAD buffer counts every frame as voice, failing safe). The
  spoken outro is stripped from the text tail (strip_trailing_phrase,
  anchored to the end so a mid-sentence outro is preserved). Hotkey
  stop, discard, the minutes cap, and incognito all hand the listener
  back to normal listening cleanly.

- **2026-07-05 (step 3 complete, #189 + #190 merged)**: #189 took a
  clean Copilot review (second of the round); #190 had one catch
  (invalid wake_silence_stop_s values silently disabled the silence
  stop instead of falling back to the default), fixed with a
  regression sweep. Multi-model score keys and the silero VAD buffer
  were verified live in the venv before #190 shipped. Suite 418.
  Step 5 plan section added from training-pipeline research (official
  openwakeword trainer contract read from the installed package).
  Remaining: step 5 phrase manager (plan above), step 6 NPU backend
  (fresh session). Owner reminder still outstanding: the tray app
  needs ONE restart to pick up everything from #167 onward.

- **2026-07-05 (owner intake + checklist, #192)**: owner asked for a
  running list of everything shipped but not yet hand-tested, kept
  current with development. docs/owner-test-checklist.md shipped with
  concrete steps/expected results (tray restart gates all), and the
  CLAUDE.md same-PR rule now requires owner-facing PRs to append or
  update an entry. Standing instruction: keep developing.

- **2026-07-05 (step 5 PR A)**: trainer environment + headless CLI
  per the step 5 plan. Dataset sources, piper checkpoint, package
  list, and the three-stage invocation verified VERBATIM against the
  upstream automatic_model_training notebook. whisper_sync/
  phrase_training.py owns the pure logic (workspace layout, config
  generation as JSON-is-YAML, command construction - CI-tested, no
  yaml/torch imports); training/setup_trainer.py builds the separate
  trainer venv + downloads (idempotent, 12-18 GB size gate behind
  --yes); training/train_phrase.py trains one phrase and drops the
  .onnx into workspace/phrases/ (GPU gate behind --go per the
  warn-owner protocol). EXECUTION STAGED: the dataset download and
  the first supervised training run wait for an explicit owner go -
  PROVISIONAL config values are validated by that first run.

- **2026-07-05 (owner-reported listener failure, root-caused +
  fixed)**: the owner enabled the wake listener and "nothing
  happened". The app log showed the listener reporting "openwakeword
  is not installed" - it IS installed; the real failure was
  onnxruntime's pybind11 DLL init, which fails when windows_toasts'
  WinRT bindings load first (bisected across the app's import set;
  scipy/portaudio/pystray/PIL/keyboard/whisperx are all innocent).
  Fix: onnxruntime preload at the top of __main__.py before any app
  import, plus an honest error split in the listener
  (ModuleNotFoundError = install hint; any other ImportError = real
  traceback + "could not load" toast, regression-tested). Reproduced
  and proven in-process: without preload the import fails after
  windows_toasts; with it the model loads and predicts. Also: owner
  gaming - the app was STOPPED to free VRAM (stronger than sleep;
  one start.ps1 relaunch brings it back on the fixed code), and all
  GPU/model use now ASKS FIRST until further notice. Trainer setup:
  the 17.5 GB feature downloads completed; datasets 2.14.6 needed
  pyarrow<17 (PyExtensionType removal); conversion resumed.

- **2026-07-05 (owner directives: automate validation, real-meeting
  fixture, hardware freed)**: the owner asked that nothing require his
  hand-testing, that a real 5-10 minute multi-speaker meeting be
  pinned into the test folder as a standing fixture, and freed the
  GPU/machine for real testing until further notice. Shipped
  tests/test_live_validation.py (WS_LIVE=1): shared-mic coexistence +
  prefix ordering on the real mic, consent-store read, synthesized
  "hey jarvis" through the real model + decision loop (one wake, >1s
  prefix, an unrelated spoken phrase never fires, VAD speech/silence
  split), GPU
  probe healthy + dead-probe -> CPU-pinned real worker transcribing
  correctly, and the FULL production pipeline on the pinned fixture
  (tests/fixtures/real-meeting/, gitignored - private audio never
  enters the public repo) judged against its reference transcript.
  9/9 green on the dev machine 2026-07-05. Findings fixed along the
  way: onnxruntime DLL-init order conflict (preload at collection),
  live guard tests polluting the production gpu-guard.jsonl
  (event_path pinned to temp). Trainer-env execution findings (PR A
  scaffold validated by running it): torch must come from the cu128
  index (Blackwell sm_120 + the proven whisper-env build), the
  piper-sample-generator clone must pin v2.0.0 (v3 broke train.py's
  generate_samples import), piper-phonemize -> piper-phonemize-fix
  and webrtcvad -> webrtcvad-wheels (upstream ships no Windows
  wheels), and the trainer venv must be Python 3.11. Machine note:
  a crash-reboot occurred 20:59 local while the machine was idle of
  session workloads (HYPERVISOR_ERROR bugcheck + WHEA fatal hardware
  error; this laptop has a documented chronic GPU-driver instability
  history); the full GPU pipeline test ran clean immediately after
  reboot. The tray app was found not running and was started fresh
  from the checkout (post-#194 code) - the restart gate is cleared.

- **2026-07-05 (step 5 PR B)**: saved-phrase registry. New config key
  wake_phrases (name -> {path, role wake/outro, active}); the
  listener loads ALL active models into one openWakeWord Model
  (wake_model_paths falls back to the pretrained wake_phrase_model
  when nothing is active, so the listener never goes deaf; outro
  routing generalized from one key to the _outro_keys set, legacy
  wake_outro_model still honored). Text strips try every active
  phrase name plus the legacy keys - the strips are conservative, so
  only the spoken one matches. Tray: Settings > Wake Word Listener >
  Saved Phrases with active checkmarks; toggling bounces the listener
  thread (model list binds at thread start). Malformed registry
  entries are skipped, never crash. Remaining for step 5: PR C (Set
  Phrase dialog + background training job + tray status/ETA +
  registry auto-registration on training success).

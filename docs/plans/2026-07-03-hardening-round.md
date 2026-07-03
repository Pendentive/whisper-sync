# Hardening Round - 2026-07-03

> Status: ACTIVE
> Intake: docs/specs/2026-07-03-owner-directive-verbatim.md (verbatim owner
> directive) and the four companion specs dated 2026-07-03.
> This doc is the state surface for the round: the table below is updated
> as each item merges, and the progress log records results and decisions.
> Any session (or person) can resume the round from this file alone.

## Item status

| # | Item | Spec | Status |
|---|------|------|--------|
| 2 | In-app single-instance lock + orphan worker reaping | gpu-guard-spec B4 | MERGED (#156) |
| 9 | CI runs the system test suite on every PR | testing-and-docs-cleanup T1 | MERGED (#157) |
| 1 | GPU Guard: VRAM watchdog, model downgrade ladder, event log | gpu-guard-spec B1-B3 | MERGED (#158) |
| 3 | Sleep/resume power event handling | hardware-resilience H1 | MERGED (#160) |
| 11 | Docs truth and navigation pass | testing-and-docs-cleanup D1-D5 | MERGED (#159) |
| 10 | docs/testing.md single testing entry point | testing-and-docs-cleanup T2-T4 | MERGED (#159) |
| 8 | Finish executor migration (remaining stray spawns) | architecture-validation A3 | MERGED (#161) |
| 4 | Device-loss and wedged-worker stall detection | hardware-resilience H2+H4 | MERGED (#165) |
| 5 | Overlay dictation disk-first | hardware-resilience H3 | MERGED (#162) |
| 7 | State machine transition table | architecture-validation A1 | MERGED (#164) |
| 6 | __main__.py decomposition by workflow | architecture-validation A2 | PENDING |

Execution order: 2, 9, 1, 3, 11+10, 8, 4, 5, 7, 6. Owner approved the
full list 2026-07-03 with standing authorization to proceed item to item
without further questions absent a large unforeseen blocker.

Design constraint from the owner (applies to item 7): this app stays a
very light, load-and-unload Python tray app (the API-first successor is a
separate heavier application). The state machine must be reliable and
easy to troubleshoot - a flat data table of allowed transitions checked
in one place, not a branching if/then system and not a framework.

## Progress log

- **2026-07-03 (round start)**: Intake PR #155 merged (verbatim
  directive + 4 specs). Round opened with item 2.

- **2026-07-03 (item 2)**: instance_guard.py: named-mutex single
  instance (fail-open if the mutex API itself fails), crash-survivable
  worker pid registry wired into TranscriptionWorker start/stop, startup
  reaping with PID-reuse guard (only python images are ever terminated)
  and unkillable-pid retry. Toggle gpu_guard_single_instance (default
  true). 10 tests (real named mutex on Windows; all process seams faked
  for reap logic).

- **2026-07-03 (item 2 merged, #156)**: after review, all Win32 calls
  prototyped via a single _win32() accessor (HANDLE-truncation class from
  PR #141) and orphan reaping made Windows-only (no safe positive
  PID-reuse check elsewhere; never guess-kill).

- **2026-07-03 (item 9 merged, #157)**: tests.yml runs the system suite
  on windows-latest per PR; auto-merge requires the system-suite check
  to succeed on the head commit (plus checks: read permission - Copilot
  catch). Verified: check runs attach to the PR head sha; the suite
  passed green on its first CI run.

- **2026-07-03 (item 1)**: gpu_guard.py + vram_probe.py per spec B1-B3.
  Deviations from spec, both deliberate: no gpu_guard_allow_cpu_floor
  key (forcing device per request needs a worker restart path; the
  ladder floor is the smallest model instead - CPU floor deferred), and
  watermark hysteresis added (a persistently low reading arms ONE step,
  not one per poll; re-arm requires recovery above the watermark).
  Escalation triggers: low-VRAM watermark, dictation worker crash,
  meeting pipeline worker crash. Model seam: effective_model() at the 6
  dictation_model computations and meeting_job step_transcribe
  (model_override). 17 tests. System suite 164; venv 36.

- **2026-07-03 (item 1 merged, #158)**: review hardening: providerless
  machines fully inert (crash triggers included), invalid ladder config
  falls back to default instead of IndexError. 16 tests.

- **2026-07-03 (items 11+10 merged, #159)**: docs truth/navigation pass
  + docs/testing.md entry point; review caught a real quality bug in the
  committed retranscribe utility (transcription must run on the ORIGINAL
  recording, balanced mono is diarization-only) plus a temp-file leak;
  python floor aligned to installer truth (3.10).

- **2026-07-03 (item 3)**: power_events.py using
  RegisterSuspendResumeNotification with DEVICE_NOTIFY_CALLBACK (a
  message-only window would never receive WM_POWERBROADCAST - broadcasts
  do not reach HWND_MESSAGE windows). Suspend and resume land in
  gpu-guard.jsonl via GpuGuard.log_external_event (one correlation
  timeline); resume verifies the worker survived sleep and restarts it
  off-thread with a toast if not. Callbacks stay cheap on the OS thread.

- **2026-07-03 (item 8)**: remaining stray spawns migrated: feature
  recovery format + deep speaker-ID (IO, native-gauged), meeting WAV
  save+enqueue (IO), github first-poll waiter (scheduler step chain
  replaces a sleeping thread), dictation model reload (DICTATION lane -
  reloads and dictations cannot overlap anyway), paste clipboard restore
  (scheduler delayed job). NOT migrated, deliberate: the error-popup
  dialog spawn (a modal dialog can block for minutes and would starve
  the serial IO lane; the dialog dispatcher already single-owns Tk),
  startup recovery transcription, and the documented once-per-session
  threads (download/update/restart/quit) plus long-lived loops.

- **2026-07-03 (item 8 merged, #161)**: review moved the clipboard
  restore off the scheduler thread (timer only enqueues onto IO). A
  scripting mistake briefly committed unresolved conflict markers into
  this doc; repaired in 6bfc97e - lesson: never chain git commands after
  a resolution script without checking its exit status.

- **2026-07-03 (item 5)**: overlay dictation now streams to disk
  (overlay_ prefix in the dictation log dir, same naming family as
  normal dictation), deleted after either transcription path succeeds,
  preserved with a log line when both fail. Incognito stays RAM-only by
  design. Meeting-stop overlay cancellation cleans the temp like the
  normal-dictation path. Also fixes the stale paste.py docstring flagged
  post-merge on #161.

- **2026-07-03 (item 5 merged, #162)**: review caught that stop() closes
  streams but not the mic writer; both overlay endpoints now call
  stop_streaming() before delete-or-preserve.

- **2026-07-03 (session checkpoint)**: 8 of 11 items shipped this
  session (PRs #156-#162 plus intake #155). REMAINING, in order:
  item 7 (state machine transition table - flat data dict of
  mode -> allowed next modes checked in ONE place inside StateManager,
  warn-then-allow first, then enforce; fold _feature_suggest_active,
  _updating, _flash_active into AppState; owner constraint: simple and
  reliable, no branching system, no framework), item 4 (device-loss +
  wedged-worker stall detection, hardware-resilience H2+H4: callback
  status flags, no-buffer stall detector with one reopen attempt,
  worker progress pings during unbounded meeting transcriptions), item 6
  (__main__.py decomposition by workflow, architecture-validation A2 -
  largest, do last). A fresh session resumes from this doc alone; the
  pipeline protocol is in CONTRIBUTING.md; production validation of the
  running app (docs/testing.md, five signals) still awaits the owner
  updating via tray > Settings > Update > Labs/dev.

- **2026-07-03 (item 4)**: H4 - worker emits a liveness ping every 10s
  during meeting transcriptions (pinger thread in the worker process);
  the manager's unbounded wait wakes every 15s and, ONLY once a first
  ping proved this build pings, treats 90s of silence as a wedged
  process: kill + WorkerCrashedError (feeds the GPU guard ladder).
  Ping-less workers keep the pure unbounded contract (4f3b307). H2 -
  callbacks stamp every delivered buffer and surface PortAudio status
  flags once; a 2s scheduler check notifies the user when the mic goes
  silent >5s mid-recording and logs a mic_stall event to
  gpu-guard.jsonl. DEFERRED from H2: automatic mid-recording stream
  reopen (needs the format-ladder rework; detection ships first) and
  loopback-stream stall coverage (mic is the critical channel).

- **2026-07-03 (item 7)**: MODE_TRANSITIONS flat table in
  state_manager.py, validated in exactly one place (_apply_locked),
  warn-then-allow: an unexpected transition applies but logs loudly, so
  flows the table missed surface during a soak period without breaking
  production; tighten to reject after the log stays clean. Table derived
  from the actual emit sites (all 7 modes). Self-transitions silent.
  Folding the stray flags (_feature_suggest_active, _updating,
  _flash_active) into AppState is DEFERRED to item 6: those flags are
  toggled at 15+ sites inside the code the decomposition will move, so
  folding them first would create double churn. 4 new tests including
  the full legal-matrix sweep.

- **2026-07-03 (items 7 and 4 merged, #164/#165)**: review rounds fixed
  a mid-file test main guard, tightened the table annotation, made the
  matrix sweep use only public emits, bound the wedge-kill to the
  request-registration generation (a restart mid-wait can never kill
  the new healthy worker), and re-armed the mic-stall flag across
  recordings.

- **2026-07-03 (round closing)**: 10 of 11 items shipped (PRs
  #155-#165). ONLY item 6 remains: __main__.py decomposition by
  workflow (architecture-validation A2). Do it in a FRESH session, in
  this extraction order: dictation flow (incl. overlay), meeting flow
  (start/stop/save/recovery), settings + menu construction,
  update/restart/quit lifecycle. Fold the stray flags
  (_feature_suggest_active, _updating, _flash_active) into AppState
  during the dictation/meeting extractions (deferred from item 7 for
  exactly this reason). Target: __main__.py becomes wiring + hotkey
  routing under ~800 lines; every extracted module gets unit tests.
  Also watch the MODE_TRANSITIONS soak: any "Unexpected mode
  transition" warnings in production logs mean the table needs a row
  (or a flow needs fixing) BEFORE tightening warn-to-reject.
  Production validation (docs/testing.md five signals + gpu-guard.jsonl
  correlation) still awaits the owner updating the running app.

- **2026-07-03 (item 6 opened, fresh session)**: hygiene sub-task first
  (#168): tests/__init__.py points WS_LOG_DIR + WS_DATA_DIR at a temp
  dir before any whisper_sync import, so test runs no longer pollute
  the live app log / worker-pids.json / gpu-guard.jsonl (the tray app
  runs FROM this checkout). Seam lives in the package init, not
  conftest, because the venv suite runs under unittest. Soak check on
  MODE_TRANSITIONS: zero "Unexpected mode transition" warnings in all
  app logs, BUT the running app predates #164, so the soak clock only
  starts once the owner restarts onto current dev - warn-to-reject
  stays deferred past this round.

- **2026-07-03 (item 6, extraction 1)**: dictation flow extracted to
  dictation_flow.py (~700 lines out of __main__.py: toggles, start/
  stop, auto-stop cap, overlay dictation, discard, crash recovery,
  feature formatting, recent history). _feature_suggest_active FOLDED
  into AppState.feature_suggest: intent passes down as a parameter and
  enters state atomically with DICTATION_STARTED, clearing on the
  completion/discard/idle emit - the old set-then-unwind pattern (15+
  sites) is gone. Duplicated overlay bookkeeping (backup path vs
  fallback path) collapsed into _deliver_overlay. Heavy deps (capture,
  backup_worker, paste) import lazily so the flow is testable on the
  dependency-light system python - 20 unit tests the god-class made
  impossible, incl. the feature-flag lifecycle and the overlay
  fallback. DEVIATION from the closing-entry list: _flash_active stays
  a UI-owned animation gate (moves out with the menu extraction, not
  into AppState - a cosmetic flash gate in the event log would push
  out useful history); _updating folds during the lifecycle
  extraction as planned.

- **2026-07-03 (item 6, extraction 1 merged, #169)**: review caught a
  REAL latent bug the move exposed: the feature hotkey during a normal
  overlay dictation fell through to _start_overlay and would have
  replaced the live overlay recorder (double-opened mic). Predates the
  extraction; now ignored like the dictation-in-progress case, with a
  regression test pinning recorder identity.

- **2026-07-03 (item 6, extraction 2a)**: meeting dialogs extracted to
  meeting_dialogs.py (~730 lines: ask_meeting_name,
  ask_speaker_confirmation incl. deep-ID rerun, ask_recovery_name,
  show_llm_unavailable, plus module-level styling helpers and the ABORT
  sentinel shared with the remaining flows). meeting_job now calls
  app.dialogs.ask_speaker_confirmation. Extraction 2 is split 2a
  (dialogs, mechanical) / 2b (meeting flow logic) to keep each PR
  reviewable. 9 new tests pin sanitize_name and the abort-on-crash
  contract for every dialog (a Tk failure must never abort a meeting
  save or hang the caller).

- **2026-07-03 (production bugs, owner-reported)**: two real bugs from
  field use, both fixed. (1) PIPELINE ABORT AFTER TRANSCRIPTION: every
  past-week meeting had transcript.json but no flatten/speakers/minutes.
  Root cause found in the 07-03 app log: #141 aliased speaker_segments
  to the raw segments LIST; log_transcript_preview expects
  {speaker: [utterances]}, raised AttributeError at the END of
  step_transcribe, and the job aborted with the transcript already
  saved. Fix: meeting_job groups previews itself
  (group_speaker_previews, garbage-tolerant), the alias is removed from
  stage_finalize, and log_transcript_preview can no longer raise at
  all. Verified with WS_E2E on real audio. (2) SPLIT NAME COLLISION:
  portion [1] starts at t=0 so its MMDD_HHMM prefix equals the
  source's; reusing the source's name made dest == source (copy2
  SameFileError; exist_ok would also silently clobber unrelated
  folders). Fix: pre-flight destination checks (duplicates and existing
  folders rejected BEFORE any write) + the source renames to
  <name>.splitting during the split, so a portion may legitimately
  reuse the original name. 8 new tests (5 split on synthetic wavs, 3
  preview/logger). NOTE: the running tray app executes from this repo
  checkout - it must be RESTARTED after this merges to load the fix,
  and pytest runs pollute the live app log (logs/app), a hygiene item
  for item 6's session.

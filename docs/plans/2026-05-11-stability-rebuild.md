# WhisperSync Stability Rebuild — Architecture Review and Plan

> Status: ACTIVE. This document persists context across Claude sessions.
> Each implementation session appends to the Progress Log at the bottom.
> Written 2026-05-11 after a full read of the runtime codebase (~8k lines).

## 1. What this app is

WhisperSync is a lightweight Windows tray app for local meeting transcription
and dictation. It records mic + WASAPI speaker loopback, transcribes via
whisperX/CTranslate2/CUDA in an isolated subprocess, diarizes speakers,
identifies them via Claude CLI, and writes meeting folders
(transcript.json, transcript-readable.txt, minutes.md) into the
ic-product-mgmt repo at `meetings/in-house/`. The PM workflow in that repo
(pm-extract-meeting, pm-transcribe-recording, meeting recovery flows in its
CLAUDE.md) consumes these outputs. It must stay lightweight: tray icon,
hotkeys, no heavy frameworks.

## 2. Why this plan exists

The app has had a months-long series of native crashes (Windows fatal
exceptions), memory growth, and state races. Each crash was patched
point-by-point (PRs #122-#136). The crash site keeps moving because the
underlying architecture — unbounded ephemeral threading + native Windows
libraries + CPython GC — is structurally unsafe. This plan replaces the
point fixes with a small set of structural changes while keeping the app's
footprint and behavior identical.

### Crash history (what we learned)

| PR | Crash site | Point fix |
|----|-----------|-----------|
| #122 | CUDA query in tray menu | cache GPU name from worker |
| #124/#126/#127 | speaker dialog | scrollable dialog rework |
| #128 | speaker_id step death | failsafe placeholders |
| #129 | tk.Tk() on rotating threads | DialogDispatcher (single tkinter thread) |
| #131 | json.load in write_speaker_map (bg thread, GC interleave) | pass pre-parsed dict |
| #132 | json.load in flatten/_generate_minutes | same pattern |
| #133 | speaker-id 90s timeout never fit reality | 240s, no retry |
| #134 | GC on any bg thread mid-native-call | gc.disable() + collect "checkpoints" |
| #135 | the #134 collect checkpoints THEMSELVES crashed | remove collects, keep disable |
| #136 | mic probe log noise | per-device format cache |

**Honest root-cause statement:** the 0x80000003 (STATUS_BREAKPOINT) faults
are Windows heap-corruption detections. The GC was usually the *detector*,
not necessarily the corruptor. Candidate corruptors, in likelihood order:
(a) repeated `tk.Tk()` create/destroy cycles (now serialized to one thread
but still churning Tcl interpreters), (b) un-serialized pystray menu swaps
from arbitrary threads racing the Win32 pump, (c) ephemeral-thread churn
interacting with COM/Win32 thread-affine state, (d) pyaudiowpatch loopback
callbacks. We never proved a single culprit; the plan removes every
candidate structurally instead of betting on one.

## 3. Architecture as-is (findings)

### 3.1 Thread inventory — the core problem

Long-lived threads (acceptable): pystray pump (main), keyboard listener,
keyboard dispatcher, DialogDispatcher (tkinter), ClipboardThread (STA),
toast worker (COM), post-process worker, stats-flush loop, heartbeat,
GitHubPoller, 2 mp.Queue feeder threads per worker subprocess (×2-3
workers).

Ephemeral threads — **a fresh `threading.Thread(daemon=True)` is spawned
for nearly every user action** (~20 spawn sites in `__main__.py` alone):

- `_process` (every dictation), `_process_overlay` (every overlay dictation)
- `_save_and_enqueue` (every meeting stop)
- `_schedule_idle` `_reset` (every state reset), `_yellow_flash` reset
- paste.py `_restore` (every dictation with clipboard restore)
- `_do_restart`, `_do_quit`, `_do_update`, `_do_download`, `_do_spawn`
  (backup worker), `_wait_worker`, `_wait_first_poll`
- `_format_feature_async` (every feature), `_recover_dictation`/`_recover_feature`/
  `_recover_meetings` `_transcribe`, `_recover_meeting_speakers` `_run`
- `_run_deep` (deep identify), `_show_error_popup` `_dispatch`
- toast button callbacks (every button click spawns a thread)
- `_set_compute_device` `_do_restart`, `_set_model` reload lambda

Thread count oscillates 9-16+ constantly. Every spawn/exit cycle churns
the Windows heap and TLS, and any of these threads can be the allocation
context in which corruption is detected.

### 3.2 State management — scattered and racy

`StateManager` (good core: typed events, lock, listeners outside lock) holds
only `mode / meeting_transcribing / dictation_overlay / speaker_ok /
progress`. Everything else lives as loose attributes on the `WhisperSync`
god-object (3,703 lines):

- `_feature_suggest_active` — read/written under `self._lock` sometimes,
  not always
- `_flashing` — accessed via `getattr(self, '_flashing', False)`, no lock
- `_stats` dict — mutated from `_process`, `_process_overlay`, meeting job
  threads concurrently, no lock
- `_dictation_history` — list mutated from two different worker threads,
  no lock
- `_updating`, `_recovering_meetings`, `_github_prs`, `_overlay_recorder`,
  `_dictation_wav_path`, `_meeting_start_time` — mixed access patterns
- `self.cfg` — a plain dict shared by reference with BackupTranscriber,
  StateManager, ToastListener, and worker config snapshots; mutated from
  menu callbacks (pystray thread) while read everywhere. **No lock at all.**

Mode transitions are if/elif chains in `toggle_dictation` /
`toggle_feature_suggest` / `toggle_meeting` reading 3 state fields
non-atomically (they hold `self._lock`, but the workers that complete
transitions don't).

### 3.3 Tray safety hole (live bug)

`_update_tray()` serializes `icon`/`title` writes under `_tray_lock` — but
`_refresh_menu()` does `self.tray.menu = self._build_menu();
self.tray.update_menu()` with **no lock**, called from: dictation worker
threads, overlay threads, github poller, recovery threads, menu callbacks.
`_build_menu()` allocates hundreds of pystray MenuItems with closures.
The 2026-05-07 15:10 crash trace is exactly this: `_build_menu ←
_refresh_menu ← _process_overlay` racing the pump thread.

### 3.4 Memory

- **Speaker loopback audio is RAM-only, always.** `start_streaming()` only
  ever creates the mic writer; `_speaker_writer` is never assigned by any
  caller. The pyaudiowpatch callback appends native-rate float32 chunks to
  `_speaker_data` for the entire meeting: 48 kHz × 4 B ≈ **691 MB/hour**
  held until stop(), then a full-size resample allocates more. This is the
  dominant memory consumer for long meetings.
- Dictation mic audio accumulates in RAM unbounded (~230 MB/hour @16k);
  fine for minutes, bad for a forgotten hotkey.
- `gc.disable()` (PR #134/#135) means true reference cycles now leak
  permanently. Menu rebuilds create closure-heavy object graphs on every
  refresh (after every dictation); any cycle in them is permanent.
- `_build_menu` on every refresh: ~hundreds of objects, plus `IconAnimator`
  per flash.

### 3.5 Worker protocol

- `TranscriptionWorker._wait_response()` **ignores its timeout** by design;
  a wedged-but-alive worker hangs the pipeline forever.
- Multiple threads can race `.get()` on the single response queue
  (`wait_ready` vs `_wait_response` — partially mitigated by restart()
  blocking, acknowledged in comments).
- Requests have IDs but there's no reader-owner; stale-message handling is
  ad hoc inside `_wait_response`.

### 3.6 What is already good (keep)

- Worker subprocess isolation for whisperX/CUDA (crash containment works)
- StreamingWavWriter crash-safety + `fix_orphan` recovery
- DialogDispatcher, ClipboardThread, toast worker — correct single-thread
  affinity patterns, just incomplete coverage
- StateManager event model
- MeetingJob step pipeline with per-step logging
- Lifecycle/heartbeat/faulthandler forensics (these made the crash history
  diagnosable at all)
- Backup transcriber design (always-available dictation)

## 4. The rebuild plan

Principle: **fixed thread topology, single-owner state, provable-idle GC.**
No frameworks, no new dependencies, same behavior. Each phase is a separate
PR through the normal Copilot auto-merge pipeline.

### Phase 1 — Concurrency primitives (new modules, no behavior change)

`whisper_sync/scheduler.py` — ONE timer thread for the whole app.
- `call_later(delay_s, fn, label) -> handle`, `call_every(interval_s, fn,
  label) -> handle`, `handle.cancel()`, graceful `shutdown()`.
- Replaces every `time.sleep` thread: `_schedule_idle`, flash resets,
  deferred restart/quit, stats flush loop, github poll wait, toast delays,
  clipboard restore delay.

`whisper_sync/executors.py` — named long-lived single-thread executors.
- `Executor("dictation")`, `Executor("io")` with `submit(label, fn)`;
  bounded queue; exceptions logged, never propagate to thread death.
- Global `active_native_calls()` gauge: submit() variants mark jobs that
  enter native/subprocess code, so the app can *prove* quiescence.

`whisper_sync/idle_gc.py` — provable-idle cycle collection.
- Scheduler job every N minutes: if pipeline idle AND dictation executor
  idle AND io executor idle AND no recording AND no worker request in
  flight → `gc.collect()` once. Logged with freed count. This restores
  leak cleanup that #135 gave up, without the #134 crash (collection only
  when no thread can be mid-native-call).

### Phase 2 — Tray discipline

- All pystray mutations (`icon`, `title`, **`menu`**) go through
  `_update_tray` under `_tray_lock`.
- `_refresh_menu()` becomes `request_menu_refresh()`: debounced (300 ms)
  scheduler job; menu building happens on the scheduler thread, swap under
  lock. Arbitrary threads never touch pystray again.

### Phase 3 — Dialog system: persistent Tk root

- DialogDispatcher creates ONE hidden `tk.Tk()` at thread start; dialogs
  become `Toplevel` + `wait_window()`. Eliminates Tcl interpreter
  create/destroy churn (top corruption candidate). Dialog callables receive
  the root as an argument; old-style callables keep working during
  migration via a wrapper.

### Phase 4 — Memory fixes

- Speaker loopback → disk streaming: give `_pa_callback` a
  StreamingWavWriter at native rate (`speaker-temp.wav` exists in temp-file
  conventions already; `fix_orphan` covers it); resample on finalize
  chunk-wise. RAM for a 2 h meeting drops ~1.3 GB → ~0.
- Dictation RAM cap: configurable max minutes (default 30); auto-stop with
  toast.
- Menu rebuild only via debounced path (Phase 2) — cuts allocation churn.

### Phase 5 — State consolidation

- `ConfigStore` wrapper: lock + `get/set/snapshot()`; all `self.cfg[...]`
  writes route through it; subscribers (worker snapshots) take snapshots.
- Move `_feature_suggest_active`, `_flashing`, `_updating` into AppState;
  add `StateManager.try_transition(from_modes, to_mode) -> bool` so toggles
  are atomic check-and-set instead of read-then-act.
- `_stats`/`_dictation_history` guarded or moved onto single-owner threads.

### Phase 6 — Worker protocol hardening

- Single reader thread per worker owns `response_q`; requests get
  `Future`-style completion objects; real timeouts restored
  (timeout → kill + respawn + WorkerCrashedError).

### Phase ordering and risk

1 → 2 → 3 are the crash killers, in dependency order. 4 is the memory
fix and is independent. 5 and 6 are correctness hardening. Each phase
lands with tests and runs through Copilot review; the app is usable after
every phase.

## 5. Test plan

Repo convention: unittest + fake modules in `sys.modules` (see
`test_capture_open_input.py`), no heavy deps in CI.

- `test_scheduler.py`: call_later fires once; call_every repeats and
  cancels; shutdown joins; exceptions in jobs don't kill the thread;
  many jobs share one thread (assert `threading.active_count` stable).
- `test_executors.py`: serial execution order; exception isolation;
  `active_native_calls` increments/decrements; bounded-queue rejection.
- `test_idle_gc.py`: collects only when all gauges idle; skips when any
  busy; logs freed count (gc fakeable via monkeypatch).
- `test_tray_serialization.py`: fake pystray icon records mutation thread;
  assert all mutations from one thread, menu refresh debounces N requests
  into 1 rebuild.
- `test_dialog_dispatcher_root.py`: persistent-root contract with a fake
  tk module (root created once, N dialogs reuse it, shutdown destroys).
- `test_capture_speaker_streaming.py`: speaker callback writes to fake
  writer not RAM list; finalize resamples chunk-wise; orphan recovery
  covers speaker-temp.wav.
- Existing 51 tests must keep passing untouched.

## 6. Progress log

- **2026-05-11 (session 1)**: Full codebase read. This plan written.
  Phase 1 implemented: scheduler.py, executors.py (+ native_call gauge
  wrapped around ALL main-process subprocess sites), idle_gc.py wired
  into run(). 26 new tests. PR #137. Copilot caught a submit/shutdown
  race in Executor (fixed: enqueue under lock) and a flaky test (fixed).
  Phase 2 implemented on stacked branch feat/stability-phase2-tray:
  tray_refresh.MenuRefresher (debounced single-owner menu rebuild),
  _update_tray extended to own menu swaps under _tray_lock,
  _refresh_menu converted to coalescing request. 7 new tests (84 total).

- **2026-05-11 (session 1, end)**: All three PRs MERGED to dev:
  - **#137 (Phase 1)**: scheduler + executors + provable-idle GC.
    Restores cycle-leak cleanup safely (the #134/#135 dilemma resolved:
    collect only when every native call is gauged idle).
  - **#138 (Phase 2)**: tray mutation serialization + 300 ms menu
    debounce. Closes the 2026-05-07 crash path (unsynchronized
    tray.menu swap racing the Win32 pump). Copilot caught a stuck
    pending-flag edge on scheduler shutdown (fixed + regression test).
  - **#139 (Phase 4)**: speaker loopback disk streaming. Meeting RAM
    drops ~691 MB/hour -> flat. Copilot caught real dropped-audio bug
    (pre-writer backlog chunks); fixed with _migrate_speaker_backlog
    on first post-writer ingest and in stop().
  - Verification at dev b353f25: system suite 85 pass, venv capture
    suite 21 pass (106 total; was 51 at session start).
  - REMAINING: Phase 3 (persistent Tk root in DialogDispatcher),
    Phase 5 (ConfigStore + StateManager.try_transition + flag
    consolidation), Phase 6 (worker protocol: single reader thread,
    real timeouts), Phase 1b (migrate the ~20 ephemeral thread spawn
    sites in __main__.py onto the executors/scheduler). Phase 1b is the
    largest remaining diff; suggest one PR per group of spawn sites.

- **2026-06-10/11 (session 2, overnight)**: Log validation across May 12 -
  June 10 found the dominant RECENT crash family is tkinter Tcl churn
  (access violations in tkinter __del__; shutdown-GC faults), confirming
  Phase 3 as top priority. Shipped:
  - **#141**: real-data test harness (6 integration tests against real
    meetings, flatten byte-exact vs known-good outputs; opt-in WS_E2E=1
    end-to-end worker transcription of a real recording). The E2E
    immediately found and fixed a real bug: stage_finalize never
    populated word_count/num_speakers/duration/speaker_segments - every
    meeting logged "0 words, 0 speakers" and weekly stats recorded
    zeros. Also: heartbeat now logs rss=NMB (memory forensics) and
    dictation auto-stops at dictation_max_minutes (default 30).
  - **#142 (Phase 3)**: persistent hidden Tk root in DialogDispatcher;
    all 8 dialog sites in __main__.py are Toplevel children; zero
    tk.Tk() churn in the tray app. Copilot caught a shutdown
    state-clear race (fixed + regression test).
  - **Multi-channel mic support** (user request): _open_input_stream
    ladder now tries native max_input_channels when mono open is
    rejected (laptop 4-mic arrays were unusable); _mic_callback
    downmixes mean-across-channels before resample/write; effective
    channel count cached. 8 new tests (channel ladder + downmix math).
  - REMAINING: Phase 5 (state consolidation), Phase 6 (worker protocol),
    Phase 1b (ephemeral thread migration onto executors).

- **2026-06-11 (session 2, continued)**: **#144 (Phase 5a)** merged:
  StateManager.try_transition (atomic mode check-and-set) wired into
  _start_dictation/_start_meeting - concurrent mode changes now reject
  a start instead of double-starting; SessionStats lock-guarded
  counters replace the racy bare _stats dict; _dictation_history under
  a lock at all 3 append sites + readers; _yellow_flash gated by
  _flash_lock (Copilot caught that a bare Event is_set/set pair is not
  atomic). 11 new tests incl. 8-thread contention proofs. System suite
  107 pass on dev 79425c7.
  REMAINING: Phase 5b (ConfigStore - lock + snapshot for the shared cfg
  dict, ~100 call sites), Phase 6 (worker protocol: single reader
  thread, real timeouts), Phase 1b (migrate ~20 ephemeral thread spawns
  onto DICTATION/IO executors + scheduler). Each is one PR; start a
  fresh session from this doc.

- **2026-06-11 (session 2, Phase 6)**: worker_manager rewritten with a
  single reader thread owning the response queue (the old shared-get
  design let one consumer swallow another's response). Requests get
  pending-slots keyed by request_id; worker death fails ALL pending
  callers immediately; transcribe_fast regains a real timeout (expiry
  kills the wedged worker and raises WorkerCrashedError); transcribe
  (meetings) intentionally stays unbounded (hard caps killed legit long
  transcriptions, see 4f3b307) with death-detection via the reader.
  worker import deferred so the protocol layer is unit-testable without
  numpy. 7 protocol tests (routing, out-of-order, stale-drop, ready,
  startup-error, death-fails-all, timeout-kills). Real-data E2E re-run
  against the new protocol.

- **2026-06-11 (session 2, close-out)**: **#146 (Phase 6)** merged after
  a second Copilot round caught two real bugs, both fixed + regression
  tested: (1) a late-exiting old reader could clobber the NEW worker
  generation's ready state after restart - reader state now lives in a
  per-spawn _WorkerGeneration bound to each reader thread; (2) a closed
  response queue raises ValueError, which silently killed the reader and
  stranded pending waiters - now treated as worker death. 9 protocol
  tests total; system suite 116 pass; real-data E2E PASS.
  **#147 (pipeline fix)** merged: auto-merge was permanently blocked by
  GitHub remapping already-addressed Copilot comments onto each new head
  commit (commit_id follows the remap, so the sha-match gate re-counted
  fixed feedback forever). Gate now blocks on Copilot review threads
  that are unresolved AND not outdated (GraphQL reviewThreads), with an
  explicit close-out: reply citing the fixing commit, resolve the
  thread. Also removed review-pr.yml's duplicate legacy auto-merge job
  (second writer, stale gate, could merge past open feedback). #146 then
  merged through the normal pipeline - no emergency override.
  REMAINING: Phase 5b (ConfigStore - in progress: module + 10 tests
  written, __main__/meeting_job wired; worker spawn pickle boundary
  pending), Phase 1b (migrate ~20 ephemeral thread spawns onto
  DICTATION/IO executors + scheduler).

- **2026-06-11 (session 2, final)**: **#149 (Phase 5b)** merged:
  ConfigStore Mapping wraps the shared cfg dict - locked writes,
  copy-on-write set_nested, copy-on-read for mutable values (Copilot
  catch), atomic snapshot() for save/pickle boundaries, transaction()
  for the diarize-slot swap; worker spawn snapshots at the pickle
  boundary and update_config rebinds the live store (fixing a
  pre-existing staleness bug where a device switch pinned a frozen
  copy). 11 tests. **#150 (Phase 1b part 1)** merged: high-frequency
  spawn sites onto the Phase 1 primitives - dictation/overlay/cap-stop
  on DICTATION, feature formatting (native-gauged) + toast callbacks +
  manual GitHub poll on IO, icon flashes as scheduler frame chains;
  submit_or_spawn() falls back to a one-shot thread (native-gauged,
  exception-logged - Copilot catch) so work is never dropped. 8 tests.
  System suite 135 pass; venv suite 36 pass.
  REMAINING (part 2, lower priority): _schedule_idle blink chain ->
  scheduler steps (touches state emissions, do carefully); leave
  once-per-session threads (download/update/restart/quit/recovery)
  as dedicated threads by design. Pipeline note: close out review
  feedback by replying with the fixing commit and resolving the
  thread - auto-merge gates on unresolved Copilot threads (#147).

# Backlog - deferred and known-open work

Deliberate deferrals and known gaps, swept from the plan docs and code
on 2026-07-04 (owner request). Each entry cites where the decision was
recorded. Remove entries when shipped; add new deferrals here in the
same PR that defers them.

## Owner-gated (waiting on real-world use)

- **Production validation of the 2026-06/07 rebuild + hardening** - the
  five signals in [docs/testing.md](testing.md) need evidence from the
  updated running app (restart onto current dev first).
- **MODE_TRANSITIONS warn-to-reject** - the transition table currently
  logs unexpected transitions but allows them; tighten to reject after
  the post-restart soak stays clean for a few days
  (plans/2026-07-03-hardening-round.md, item 7).
- **nvlddmkm crash correlation** - `gpu-guard.jsonl` (downgrades,
  suspend/resume, sleep/wake, mic stalls) plus heartbeat rss lines are
  the timeline to compare against Windows crash times
  (specs/2026-07-03-owner-directive-verbatim.md).

## Hardware resilience (deferred by design, detection shipped first)

- **Mid-recording mic stream reopen** - stall detection ships; the
  automatic reopen needs the capture format-ladder rework
  (hardening item 4, H2 deferral).
- **Loopback-stream stall coverage** - only the mic channel is
  monitored today; a dead loopback still records mic-only silently
  after the initial warning (item 4, H2 deferral).
- **WM_DEVICECHANGE removal events for GPU loss** - instant loss
  detection needs a hidden top-level window + message pump
  (message-only windows never receive broadcasts, the power_events.py
  constraint). The shipped failover detects via probe streak +
  crash-time probes instead: within one poll interval, and instantly
  on any crash (assistant round step 1,
  plans/2026-07-04-assistant-build-round.md).

## Feature gaps

- **middle_click has no plumbing** - the config key, CLICK_ACTIONS
  entry, and `_on_middle_click` handler exist, but pystray's Win32
  backend only surfaces left/right buttons; middle clicks never reach
  the app. Implement via a message-hook extension or remove the
  setting (discovered 2026-07-04 during the auto-sleep click work).
- **Installer refresh** - install experience predates most current
  features. Planned shape (owner, 2026-07-04): screens generated from
  [docs/features/](features/) - a features screen, a
  shortcuts/interaction screen, and a defaults/configuration screen -
  so the installer stays current automatically.
- **Streaming dictation ("fill as it goes")** - live word-by-word
  insertion with rolling self-correction for paste-unfriendly apps.
  Owner-floated, explicitly deferred (2026-07-04 third intake). A
  different ASR architecture (streaming zipformer / whisper-streaming
  partial hypotheses + edit-in-place delivery), not a config flip on
  the batch pipeline (specs/2026-07-04-voice-assistant-direction.md).

## Hygiene

- **Timeout constants naming (A4 remainder)** - the magic shutdown
  joins (2/3/5s) in the primitives still want named module constants
  with rationale comments (architecture spec A4; the paste
  scheduler-job part shipped in #161).

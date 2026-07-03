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
| 2 | In-app single-instance lock + orphan worker reaping | gpu-guard-spec B4 | IN PIPELINE |
| 9 | CI runs the system test suite on every PR | testing-and-docs-cleanup T1 | PENDING |
| 1 | GPU Guard: VRAM watchdog, model downgrade ladder, event log | gpu-guard-spec B1-B3 | PENDING |
| 3 | Sleep/resume power event handling | hardware-resilience H1 | PENDING |
| 11 | Docs truth and navigation pass | testing-and-docs-cleanup D1-D5 | PENDING |
| 10 | docs/testing.md single testing entry point | testing-and-docs-cleanup T2-T4 | PENDING |
| 8 | Finish executor migration (remaining stray spawns) | architecture-validation A3 | PENDING |
| 4 | Device-loss and wedged-worker stall detection | hardware-resilience H2+H4 | PENDING |
| 5 | Overlay dictation disk-first | hardware-resilience H3 | PENDING |
| 7 | State machine transition table | architecture-validation A1 | PENDING |
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

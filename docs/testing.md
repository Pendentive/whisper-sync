# Testing - the single entry point

Everything about verifying WhisperSync, in one place. If a doc and this
page disagree, fix the other doc.

## The suites

| Suite | What | Where it runs | Command |
|-------|------|---------------|---------|
| System suite | 160+ tests: worker protocol, state, scheduler/executors, config store, GPU guard, instance guard, meeting pipeline, icons, notifications, heartbeat, lifecycle | **CI on every PR** (tests.yml; auto-merge requires it green on the head commit) and locally | see [development.md](development.md) "Running the Test Suites" |
| Venv suite | 36 tests: audio capture (open ladder, downmix, speaker streaming) + real-data harness | Local only (needs numpy/scipy in whisper-env) | see development.md |
| Real-data harness | `flatten()` reproduced byte-for-byte against real shipped meetings | Local only; discovers via `WS_MEETINGS_DIR`, skips cleanly when absent. **Private audio never enters this repo.** | part of the venv suite |
| End-to-end | Production worker subprocess on the smallest real recording (~1 min) | Local, opt-in (`WS_E2E=1`); run after worker-protocol or pipeline changes | see development.md |
| Manual checklist | Hardware-in-the-loop checks no automated test covers (real mic, hotkeys, tray, GPU) | Local, before releases and after audio/tray changes | [.claude/rules/testing.md](../.claude/rules/testing.md) |

## Where results and decisions are recorded

Test results and the decisions made on them live in the active plan
doc's progress log - the pattern established by the stability rebuild:
pass counts per session, bugs the suites caught, and what was decided.

- Current: [plans/2026-07-03-hardening-round.md](plans/2026-07-03-hardening-round.md)
- Historical: [plans/2026-05-11-stability-rebuild.md](plans/2026-05-11-stability-rebuild.md)
  (its progress log records, e.g., the E2E harness catching the
  stage_finalize zero-stats production bug, and per-phase pass counts)

## Open validation work

The stability rebuild's five-signal production validation checklist
(final entry of its progress log) is awaiting evidence from the updated
running app: clean exits, real meeting word counts, flat rss=NMB
heartbeat, mic-array + dictation cap behavior, worker respawn recovery.

## Benchmarks

README's model table was measured 2026-03 on an RTX 3090 (float16) and
is indicative only. Regenerate for your hardware with
`python -m whisper_sync.benchmark`; if you update the README table,
date-stamp it.

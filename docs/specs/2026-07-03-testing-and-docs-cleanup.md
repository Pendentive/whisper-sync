# Spec: Testing Records and Docs Cleanup - 2026-07-03

Status: PROPOSED (derived from docs/specs/2026-07-03-owner-directive-verbatim.md)
Source: testing-history mining and full docs inventory (2026-07-03).

## Part 1 - Testing: what exists, what is missing

What exists (verified): 28 test files, 178 tests, all plain unittest;
run commands documented in docs/development.md (added #154); the manual
checklist in .claude/rules/testing.md; test RESULTS and the decisions
made on them are recorded in the stability plan's progress log (pass
counts per session, bugs the tests caught, and what was done). Git
history mining confirms no testing-procedure or result doc was ever
deleted and lost; the one deleted guide (TRANSCRIPTION-GUIDE.md) was
deliberately consolidated into README in 648731f.

Gaps to close:

### T1. CI runs the suites on every PR (the critical gap)
No workflow runs any test today; Copilot review is the only merge gate.
Add a GitHub Actions job running the system-python suite (141 tests, no
heavy deps, runs on a stock runner) on every PR, required before
auto-merge. The venv/real-data suites stay local-only (need CUDA and
private audio) and get a documented pre-release manual step.

### T2. A single testing entry point: docs/testing.md
One page that says: the two automated suites and their commands (link
development.md), the real-data harness rules, the E2E opt-in, the manual
checklist (absorb and refresh .claude/rules/testing.md content), where
results are recorded (the plan-doc progress-log pattern), and the
five-signal production validation checklist currently open.

### T3. Fix the three stale "no automated tests" claims
.claude/rules/testing.md line 3, CONTRIBUTING.md Testing section, and
CLAUDE.md's framing all assert manual-only verification while 178 tests
exist. Rewrite all three to point at docs/testing.md.

### T4. Back the README benchmark table
README's model-comparison numbers are inline, undated, with no backing
artifact. Either regenerate via python -m whisper_sync.benchmark and
commit the dated output under docs/benchmarks/, or date-stamp the table
and note the hardware and how to regenerate.

## Part 2 - Docs: navigation and self-enclosure

### D1. Fix broken and orphaned docs
README links docs/specs/2026-03-24-governance-learning-loop-design.md
which was NEVER committed (404 on a fresh clone); commit it. Disposition
the other five untracked 2026-03-24 draft docs (backup subprocess x2,
backup dictation icons x2, log color) - commit with a shipped marker or
delete. Also disposition untracked retranscribe_tier2.py and
whisper_sync/.standalone.

### D2. Status-stamp the dated plans and specs
2026-03-20-automated-dev-pipeline.md, 2026-03-20-github-status-in-tray.md,
and 2026-03-24-state-manager-design.md describe shipped work with no
completion marker; add "Status: SHIPPED (see ...)" headers. Mark the
speaker-fingerprinting plan+spec pair explicitly PARKED with the reason.

### D3. Make the stability plan readable as state
Its header says Status: ACTIVE while the final log entry says COMPLETE.
Flip the header to COMPLETE (PRs #137-#152), add a phase-status table at
the top, and pull the open production-validation checklist under an
explicit "Open - awaiting evidence" heading so outcome is visible without
reading eight log entries.

### D4. README contributor section + truth fixes
Add a short "For contributors" section linking CONTRIBUTING.md,
docs/development.md, docs/testing.md (T2), and the stability plan as the
current-state doc (today README links none of them). Fix README's three
stale claims: diarization default order (balanced_mix is primary, not
per-channel), the tray icon is three-ring not dual-ring, and the broken
governance-spec link (D1).

### D5. Small staleness sweep
docs/TECHNICAL.md icons.py entry (old API described), audio-pipeline.md
speaker-ID timeout (240s no-retry since #133), ui-patterns.md menu
refresh wording (debounced since #138), README vs development.md python
version floor (3.10 vs 3.11), and reconcile the three conflicting tray
icon descriptions into one canonical table (ui-patterns.md) that others
link.

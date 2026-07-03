# Agentic Governance Learning Loop - Design Spec

> Status: SHIPPED (2026-03; governance-analysis.yml + review-logger.yml + policy.yaml). Committed 2026-07-03 - this file was referenced by README but never committed.

> **Date**: 2026-03-24
> **Status**: Approved
> **Scope**: WhisperSync repo (POC, generalizable to other repos)

## Summary

Every PR teaches the system. The system automatically proposes governance improvements based on what it learns. No human goes to GitHub. Everything flows through Claude Code conversations and automated agents.

## Architecture

### Governance Files (YAML - deterministic, machine-parsed)

**`.github/governance/policy.yaml`** - Single file for all deterministic rules:

```yaml
merge_policy:
  auto_merge_enabled: true
  require_copilot_review: true
  require_zero_suggestions: true
  complexity_gates:
    low: auto
    medium: auto
    high: auto  # learning mode

review_thresholds:
  complexity:
    low_max_lines: 50
    medium_max_lines: 200
    high_above: 200

path_protection:
  - pattern: "whisper_sync/transcribe.py"
    reason: "Core audio pipeline"
    require: architecture_note
  - pattern: "whisper_sync/worker.py"
    reason: "Multiprocessing model"
    require: architecture_note
  - pattern: ".github/workflows/**"
    reason: "CI/CD pipeline"
    require: architecture_note
  - pattern: ".github/governance/**"
    reason: "Governance rules"
    require: architecture_note

analysis:
  schedule: weekly
  min_prs_for_analysis: 3
  suggestion_categories:
    - dead-code
    - missing-function
    - error-handling
    - security
    - style
    - performance
    - naming
    - documentation
```

### Guidance Files (Markdown - interpretive, LLM-read)

**`CLAUDE.md`** - Under 200 lines, high-level architecture and conventions

**`.claude/rules/audio-pipeline.md`** - Audio processing rules:
- How stereo recording works (mic ch0, loopback ch1)
- Per-channel diarization architecture
- Balanced mono fallback chain
- Model loading and VRAM management

**`.claude/rules/ui-patterns.md`** - Tray menu and dialog conventions:
- Menu item ordering (Recent Dictations, Dictation, Meeting, Settings)
- Right-aligned state labels
- Pystray limitations (no colored dots, no split click/hover)
- Dark theme for dialogs

**`.claude/rules/testing.md`** - Verification approach:
- Manual test checklist (dictation, meeting, device switch)
- What to verify after each change category
- How to restart and test from dev branch

### Logging (JSONL - learning data)

**`docs/review-log.jsonl`** - One line per merged PR, Qodo-style attribution:

```json
{
  "pr": 54,
  "title": "feat: per-channel transcription",
  "complexity": "high",
  "files": ["channel_merge.py", "transcribe.py"],
  "lines_changed": 539,
  "copilot": {
    "reviewed": true,
    "suggestions": [
      {
        "id": "s1",
        "file": "transcribe.py",
        "line": 559,
        "category": "dead-code",
        "body": "_per_channel_result set but never read",
        "confidence": "low",
        "resolution": "accepted",
        "fix_commit": "67c970b"
      }
    ],
    "suggestions_accepted": 3,
    "suggestions_dismissed": 0
  },
  "push_cycles": 3,
  "duration_minutes": 45,
  "merge_method": "auto",
  "protected_paths_touched": ["whisper_sync/transcribe.py"],
  "outcome": "auto-merged"
}
```

### Analysis Agent

Runs weekly via GitHub Actions schedule (or on-demand via `/ws-analyze` skill).

**Input**: review-log.jsonl + policy.yaml + CLAUDE.md + .claude/rules/*.md

**Process**:
1. Read all merged PRs since last analysis
2. Categorize Copilot suggestions (real bugs vs noise)
3. Calculate metrics (true positive rate, common categories, complexity correlation)
4. Identify patterns (what guidance is missing, what rules are stale)
5. Propose changes to governance files

**Output**: A PR containing:
- `docs/analysis/YYYY-MM-DD-review-analysis.md` (pattern report)
- Diffs to `policy.yaml` (threshold adjustments)
- Diffs to `CLAUDE.md` (guidance gaps)
- New `.claude/rules/*.md` files (if domain rules emerge)
- Explanation of each proposed change with data backing

### Feedback Loop

```
Code -> PR -> Copilot reviews (reads CLAUDE.md + rules)
  -> Auto-merge (reads policy.yaml)
  -> Logged to review-log.jsonl

Weekly:
  -> Analysis agent reads log + current governance
  -> Proposes governance PR
  -> Copilot reviews the governance PR
  -> Auto-merges
  -> Next week's PRs use updated governance
```

## Implementation Order

1. Create governance file structure (policy.yaml, rules/*.md)
2. Update workflows to read from policy.yaml
3. Fix review-logger to capture rich suggestion data (100% capture rate)
4. Backfill missing PR entries in review-log.jsonl
5. Trim CLAUDE.md to under 200 lines, move content to rules
6. Create analysis agent workflow
7. Create /ws-analyze skill for on-demand analysis
8. Test end-to-end with next batch of PRs

## Success Criteria

- 100% PR capture rate in review-log.jsonl
- Analysis agent produces actionable governance PRs
- Copilot true positive rate improves over 4-week period
- CLAUDE.md stays under 200 lines
- No guardrail contradictions (drift detection)

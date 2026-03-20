# WhisperSync Automated Development Pipeline

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an automated bug-report → fix → review → merge pipeline for WhisperSync using GitHub Actions, GitHub CLI, and AI agents.

**Architecture:** Bugs reported in Claude Code sessions are filed as GitHub Issues via `gh`. GitHub Actions workflows trigger coding agents (Copilot or Claude) to create fix branches and PRs. A review agent validates the PR, and approved changes auto-merge to main. A complexity router decides which agent handles what.

**Tech Stack:** GitHub Actions, GitHub CLI (`gh`), GitHub Copilot (included in Pro), Claude Code (for complex fixes), Python, PowerShell

---

## Context

### What exists today
- **Source repo:** `pendentive/whisper-sync` on GitHub (public)
- **Working repo:** `ic-product-mgmt/scripts/whisper_sync/` (edits happen here)
- **Sync hook:** Post-commit hook copies whisper_sync changes → pendentive repo → auto-push
- **Auth:** GitHub CLI (`gh`) authenticated as pendentive, SSH key as fallback
- **No CI/CD, no `.github/` directory, no issue templates, no Actions workflows**

### What we're building
1. **gh CLI setup** — complete auth for both accounts, verify from all shells
2. **Issue templates** — structured bug reports filed from Claude Code sessions
3. **Automated review agent** — runs on every PR via GitHub Actions
4. **Automated coding agent** — reads issues, creates branches, writes fixes, opens PRs
5. **Complexity router** — lightweight triage that decides which agent handles a PR/issue
6. **Project docs** — README, CONTRIBUTING, architecture docs in the whisper-sync repo

### Portability requirement
Everything must work on any machine with just:
```
gh auth login
gh auth setup-git
git clone https://github.com/pendentive/whisper-sync.git
```

---

## Phase 1: Foundation (gh CLI + repo hygiene)

### Task 1: Complete gh CLI setup

**Files:**
- No files — system configuration only

- [ ] **Step 1: Authenticate gh with pendentive account**

Run in PowerShell:
```powershell
gh auth login
# Choose: GitHub.com → HTTPS → Login with web browser
# Log in as pendentive
```

- [ ] **Step 2: Wire gh into git credential helper**

```powershell
gh auth setup-git
```

- [ ] **Step 3: Verify from all shells**

PowerShell:
```powershell
gh auth status
cd N:\Github\repos\pendentive\whisper-sync
git push --dry-run
```
Expected: `Logged in to github.com account pendentive` and `Everything up-to-date`

Git bash (Claude Code):
```bash
export PATH="/c/Program Files/GitHub CLI:$PATH"
gh auth status
```
Expected: Same result

- [ ] **Step 4: Add gh to system PATH permanently (if not already)**

```powershell
$ghPath = "C:\Program Files\GitHub CLI"
[System.Environment]::SetEnvironmentVariable("Path", $env:Path + ";$ghPath", "Machine")
```

- [ ] **Step 5: Verify sync hook works end-to-end**

Make a trivial edit in `ic-product-mgmt/scripts/whisper_sync/`, commit, confirm it lands in pendentive repo without any auth prompts.

---

### Task 2: GitHub Issue templates

**Files:**
- Create: `.github/ISSUE_TEMPLATE/bug-report.yml`
- Create: `.github/ISSUE_TEMPLATE/feature-request.yml`
- Create: `.github/ISSUE_TEMPLATE/config.yml`

- [ ] **Step 1: Create bug report template**

```yaml
# .github/ISSUE_TEMPLATE/bug-report.yml
name: Bug Report
description: Report a WhisperSync bug
labels: ["bug", "triage"]
body:
  - type: dropdown
    id: component
    attributes:
      label: Component
      options:
        - Dictation
        - Meeting recording
        - Transcription
        - Speaker identification
        - Installer
        - UI / Tray icon
        - Other
    validations:
      required: true
  - type: textarea
    id: description
    attributes:
      label: What happened?
      placeholder: Describe the bug
    validations:
      required: true
  - type: textarea
    id: expected
    attributes:
      label: Expected behavior
      placeholder: What should have happened?
  - type: textarea
    id: reproduce
    attributes:
      label: Steps to reproduce
      placeholder: "1. Start WhisperSync\n2. Press Ctrl+Shift+Space\n3. ..."
  - type: textarea
    id: logs
    attributes:
      label: Log output
      description: Paste relevant lines from the log window or log file
      render: shell
  - type: input
    id: gpu
    attributes:
      label: GPU
      placeholder: "e.g., RTX 3090 24GB, RTX 5070 Ti 12GB"
  - type: input
    id: model
    attributes:
      label: Model in use
      placeholder: "e.g., large-v3, base, tiny"
```

- [ ] **Step 2: Create feature request template**

```yaml
# .github/ISSUE_TEMPLATE/feature-request.yml
name: Feature Request
description: Suggest an improvement
labels: ["enhancement"]
body:
  - type: textarea
    id: description
    attributes:
      label: What would you like?
      placeholder: Describe the feature
    validations:
      required: true
  - type: textarea
    id: context
    attributes:
      label: Why is this useful?
      placeholder: What problem does this solve?
```

- [ ] **Step 3: Create config to disable blank issues**

```yaml
# .github/ISSUE_TEMPLATE/config.yml
blank_issues_enabled: false
contact_links:
  - name: Questions
    url: https://github.com/pendentive/whisper-sync/discussions
    about: Ask questions here
```

- [ ] **Step 4: Commit and push**

```bash
cd N:/Github/repos/pendentive/whisper-sync
git add .github/
git commit -m "ci: add issue templates for bugs and features"
git push
```

---

### Task 3: Labels and milestones

**Files:**
- No files — GitHub API via `gh`

- [ ] **Step 1: Create labels**

```bash
gh label create "triage" --color "fbca04" --description "Needs triage" -R pendentive/whisper-sync
gh label create "auto-fix" --color "0e8a16" --description "Agent can attempt fix" -R pendentive/whisper-sync
gh label create "manual-fix" --color "d93f0b" --description "Requires human intervention" -R pendentive/whisper-sync
gh label create "complexity:low" --color "c5def5" --description "<50 lines, single file" -R pendentive/whisper-sync
gh label create "complexity:medium" --color "bfd4f2" --description "50-200 lines, 2-3 files" -R pendentive/whisper-sync
gh label create "complexity:high" --color "0075ca" --description ">200 lines or architectural" -R pendentive/whisper-sync
gh label create "component:dictation" --color "e4e669" -R pendentive/whisper-sync
gh label create "component:meeting" --color "e4e669" -R pendentive/whisper-sync
gh label create "component:transcription" --color "e4e669" -R pendentive/whisper-sync
gh label create "component:installer" --color "e4e669" -R pendentive/whisper-sync
gh label create "component:ui" --color "e4e669" -R pendentive/whisper-sync
```

- [ ] **Step 2: Verify labels exist**

```bash
gh label list -R pendentive/whisper-sync
```

---

## Phase 2: Automated PR Review

### Task 4: Review agent workflow

**Files:**
- Create: `.github/workflows/review-pr.yml`

- [ ] **Step 1: Create the review workflow**

This workflow fires on every PR and runs a lightweight review. Uses GitHub's built-in Copilot for code review (included in Pro plan). If the PR is large (>200 lines), it adds a `complexity:high` label and requests human review.

```yaml
# .github/workflows/review-pr.yml
name: PR Review

on:
  pull_request:
    types: [opened, synchronize]

permissions:
  contents: read
  pull-requests: write
  issues: write

jobs:
  triage:
    runs-on: ubuntu-latest
    outputs:
      complexity: ${{ steps.check.outputs.complexity }}
      lines_changed: ${{ steps.check.outputs.lines }}
    steps:
      - name: Check PR complexity
        id: check
        uses: actions/github-script@v7
        with:
          script: |
            const { data: files } = await github.rest.pulls.listFiles({
              owner: context.repo.owner,
              repo: context.repo.repo,
              pull_number: context.issue.number,
            });
            const totalChanges = files.reduce((sum, f) => sum + f.changes, 0);
            const fileCount = files.length;

            let complexity = 'low';
            if (totalChanges > 200 || fileCount > 5) complexity = 'high';
            else if (totalChanges > 50 || fileCount > 2) complexity = 'medium';

            core.setOutput('complexity', complexity);
            core.setOutput('lines', totalChanges);

            // Add complexity label
            await github.rest.issues.addLabels({
              owner: context.repo.owner,
              repo: context.repo.repo,
              issue_number: context.issue.number,
              labels: [`complexity:${complexity}`],
            });

  review:
    needs: triage
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: Copilot Review
        if: needs.triage.outputs.complexity != 'high'
        uses: actions/github-script@v7
        with:
          script: |
            // Request Copilot code review (auto-enabled for Pro)
            await github.rest.pulls.requestReviewers({
              owner: context.repo.owner,
              repo: context.repo.repo,
              pull_number: context.issue.number,
              reviewers: ['copilot-pull-request-reviewer'],
            });

      - name: Flag for human review
        if: needs.triage.outputs.complexity == 'high'
        uses: actions/github-script@v7
        with:
          script: |
            await github.rest.issues.createComment({
              owner: context.repo.owner,
              repo: context.repo.repo,
              issue_number: context.issue.number,
              body: `## 🔍 Human Review Requested\n\nThis PR changes **${needs.triage.outputs.lines_changed} lines** across multiple files. Automated review flagged it as high-complexity.\n\n@pendentive please review before merging.`,
            });
```

- [ ] **Step 2: Commit and push**

```bash
git add .github/workflows/review-pr.yml
git commit -m "ci: add automated PR review with complexity routing"
git push
```

- [ ] **Step 3: Test with a dummy PR**

```bash
git checkout -b test/review-pipeline
echo "# test" >> README.md
git add README.md
git commit -m "test: verify review pipeline"
git push -u origin test/review-pipeline
gh pr create --title "test: verify review pipeline" --body "Testing automated review" -R pendentive/whisper-sync
```

Verify the workflow runs, adds a complexity label, and triggers review.

- [ ] **Step 4: Clean up test PR**

```bash
gh pr close --delete-branch
git checkout main
```

---

## Phase 3: Bug Filing from Claude Code

### Task 5: Bug filing command

**Files:**
- Create: `ic-product-mgmt/.claude/commands/ws-bug.md` (Claude Code command)

- [ ] **Step 1: Create the bug filing command**

This is a Claude Code command that files a GitHub Issue on `pendentive/whisper-sync` from within a Claude Code session. When the user reports a WhisperSync bug conversationally, Claude invokes this to create a structured issue.

```markdown
# WhisperSync Bug Report

File a bug on pendentive/whisper-sync from this session.

## Instructions

1. Gather from the conversation context:
   - **Component**: dictation, meeting, transcription, speaker-id, installer, ui
   - **Description**: what happened
   - **Expected**: what should have happened
   - **Steps to reproduce**: if known
   - **Log output**: any error text shared in the conversation
   - **GPU**: from memory (RTX 3090 desktop, RTX 5070 Ti laptop)
   - **Model**: from context or config

2. Draft the issue body and show it to the user for approval.

3. After approval, create the issue:

```bash
export PATH="/c/Program Files/GitHub CLI:$PATH"
gh issue create \
  --repo pendentive/whisper-sync \
  --title "[Bug] <title>" \
  --body "<body>" \
  --label "bug,triage,component:<component>"
```

4. Report the issue URL back to the user.
```

- [ ] **Step 2: Commit in ic-product-mgmt**

```bash
cd n:/Github/repos/icustomer/ic-product-mgmt
git add .claude/commands/ws-bug.md
git commit -m "feat: add /ws-bug command to file WhisperSync issues from Claude Code"
```

---

## Phase 4: Automated Coding Agent (Future)

> **Note:** This phase depends on GitHub Copilot Coding Agent availability and Actions minutes budget. Start with manual fixes + automated review (Phases 1-3) and add automation once the review pipeline is proven.

### Task 6: Coding agent workflow (design only — implement when ready)

**Files:**
- Create: `.github/workflows/auto-fix.yml`

**Design:**

The workflow triggers when an issue gets the `auto-fix` label (applied manually or by the triage agent). It:

1. Reads the issue body
2. Creates a branch `fix/issue-{number}`
3. Spins up a coding agent (Copilot Workspace or Claude Code)
4. Agent reads the issue, relevant source files, and writes a fix
5. Agent commits and opens a PR referencing the issue
6. PR triggers the review workflow (Phase 2)

**Complexity router logic:**
- `complexity:low` + `auto-fix` → Copilot handles it (free with Pro)
- `complexity:medium` + `auto-fix` → Copilot attempts, human reviews
- `complexity:high` → Always manual (never auto-fix)

**Budget guard:** Actions workflow checks monthly minutes used before spawning agents. If >80% of 3,000 minutes used, skip and comment "Monthly budget limit approaching — manual fix recommended."

- [ ] **Step 1: Design decision — defer implementation until Phase 2 review pipeline is proven**

This task is intentionally left as design-only. Implement after:
- 5+ PRs have been reviewed by the automated pipeline
- Confidence that the review agent catches real issues
- GitHub Copilot Coding Agent is stable for this repo

---

## Phase 5: Agent Context Files

### Task 7: CLAUDE.md + Copilot instructions

**Files:**
- Create: `CLAUDE.md` (source of truth for all agents)
- Create: `.github/copilot-instructions.md` (pointer to CLAUDE.md)

**Why this matters:** Every automated agent (Copilot, Claude Code, future coding agents) needs guardrails. Without context, agents guess at conventions and break things. `CLAUDE.md` is the single file that tells any agent how this codebase works, what to touch, and what not to touch.

- [ ] **Step 1: Create CLAUDE.md**

This file covers:
- **Architecture overview** — module map, audio pipeline, multiprocessing model
- **Conventions** — naming, logging, config, paths
- **Guardrails** — what not to touch, what requires human review
- **Testing** — how to verify changes without a test suite
- **File ownership** — which files are auto-synced from ic-product-mgmt vs local-only

Content will be generated by crawling the repo (see Phase 6).

- [ ] **Step 2: Create .github/copilot-instructions.md**

```markdown
Follow all instructions in CLAUDE.md in the root of this repository.
```

One line. Both agents read the same content, one file to maintain.

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md .github/copilot-instructions.md
git commit -m "docs: add agent context files (CLAUDE.md + Copilot instructions)"
git push
```

---

## Phase 6: Architecture Crawl + README

### Task 8: Automated architecture documentation

**Files:**
- Create/Update: `docs/architecture.md` (detailed, for agents and contributors)
- Update: `README.md` (human-facing summary)
- Update: `CLAUDE.md` (inject architecture context)
- Create: `CONTRIBUTING.md`
- Create: `docs/development.md`

**Approach:** Crawl every `.py` file in `whisper_sync/`, map the module graph, document the audio pipeline, multiprocessing model, config system, and paths resolution. This becomes the authoritative architecture doc.

- [ ] **Step 1: Crawl repo and generate docs/architecture.md**

An agent reads every module, traces imports, and produces:
- Module map (which .py does what, one line each)
- Audio pipeline flow (recording → WAV → transcription → diarization → output)
- Multiprocessing model (main process ↔ worker process, queues, lifecycle)
- Config system (defaults → overrides → runtime)
- Paths resolution (how output_dir, model cache, logs are located)

- [ ] **Step 2: Update README.md**

Add sections: project status, architecture summary (link to full doc), development setup, how to report bugs, how the automated pipeline works.

- [ ] **Step 3: Update CLAUDE.md with architecture context**

Inject the architecture summary into CLAUDE.md so agents have it immediately without reading a separate file.

- [ ] **Step 4: Create CONTRIBUTING.md**

Cover: branch naming (`fix/`, `feat/`, `test/`), PR process, how automated review works, label meanings, what triggers auto-fix vs manual.

- [ ] **Step 5: Create docs/development.md**

Cover: local dev setup, running from source, testing changes, the sync hook from ic-product-mgmt, how to debug.

- [ ] **Step 6: Commit all docs**

```bash
git add README.md CLAUDE.md CONTRIBUTING.md docs/
git commit -m "docs: add architecture, contributing, and development guides"
git push
```

---

## Phase 7: Docs Freshness Guard

### Task 9: Review agent checks for stale docs

**Files:**
- Modify: `.github/workflows/review-pr.yml`

**Design:** Add a step to the review workflow that checks if a PR:
- Adds or removes `.py` files → flags "architecture.md may need updating"
- Changes module imports → flags "CLAUDE.md module map may be stale"
- Changes config.py or paths.py → flags "config/paths docs may need updating"

This is a lightweight check — it doesn't block the PR, just comments a reminder.

- [ ] **Step 1: Add docs-freshness check to review workflow**

```yaml
      - name: Check docs freshness
        uses: actions/github-script@v7
        with:
          script: |
            const { data: files } = await github.rest.pulls.listFiles({
              owner: context.repo.owner,
              repo: context.repo.repo,
              pull_number: context.issue.number,
            });
            const pyFiles = files.filter(f => f.filename.endsWith('.py'));
            const added = pyFiles.filter(f => f.status === 'added');
            const removed = pyFiles.filter(f => f.status === 'removed');
            const configChanged = files.some(f =>
              f.filename.includes('config.py') || f.filename.includes('paths.py')
            );

            const warnings = [];
            if (added.length > 0) warnings.push(
              `New modules added: ${added.map(f => f.filename).join(', ')} — update docs/architecture.md and CLAUDE.md`
            );
            if (removed.length > 0) warnings.push(
              `Modules removed: ${removed.map(f => f.filename).join(', ')} — update docs/architecture.md and CLAUDE.md`
            );
            if (configChanged) warnings.push(
              'Config or paths changed — verify docs/architecture.md config section is current'
            );

            if (warnings.length > 0) {
              await github.rest.issues.createComment({
                owner: context.repo.owner,
                repo: context.repo.repo,
                issue_number: context.issue.number,
                body: `## Docs Freshness Check\n\n${warnings.map(w => '- ' + w).join('\n')}\n\nThese are reminders, not blockers.`,
              });
            }
```

- [ ] **Step 2: Commit**

```bash
git add .github/workflows/review-pr.yml
git commit -m "ci: add docs freshness check to PR review"
git push
```

---

## Execution Order

| Phase | What | Depends on | Effort |
|-------|------|------------|--------|
| 1 | gh CLI + templates + labels | Nothing | 30 min |
| 2 | Automated PR review | Phase 1 | 30 min |
| 3 | Bug filing from Claude Code | Phase 1 | 15 min |
| 4 | Coding agent | Phases 2+3 proven | Design only for now |
| 5 | Agent context files (CLAUDE.md) | Nothing | 15 min (stub) |
| 6 | Architecture crawl + README | Phase 5 | 1 hour |
| 7 | Docs freshness guard | Phases 2+6 | 15 min |

**Total active implementation: ~3 hours** (excluding Phase 4 which is deferred)

---

## Open Questions

1. **Copilot Coding Agent access** — Is this enabled on the Pro plan, or does it require Copilot Enterprise? Need to verify before Phase 4.
2. **Actions minutes budget** — 3,000 min/month is generous for review workflows. Coding agents consume more. Monitor usage for first month before committing to Phase 4.
3. **Notification preferences** — Should the review agent ping you on Slack when a PR needs human review? Or is GitHub notifications sufficient?
4. **Test infrastructure** — WhisperSync has no test suite. Phase 4 (coding agent) works best when there are tests to validate fixes. Consider adding basic tests as a prerequisite.
5. **CLAUDE.md sync** — When the architecture changes and CLAUDE.md is updated in the pendentive repo, should the sync hook back-propagate it to ic-product-mgmt? Currently sync is one-way (ic-product-mgmt → pendentive). CLAUDE.md would be the first file that's authoritative in pendentive, not ic-product-mgmt.

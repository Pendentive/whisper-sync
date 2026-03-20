# GitHub Status in WhisperSync Tray — Feature Spec

**Goal:** Surface GitHub PR status (reviews, suggestions, auto-merges) directly in the WhisperSync tray icon without leaving the app or checking GitHub.

## Architecture

```
WhisperSync (already running)
  └── Background thread (polls every 5 min)
        └── shells out to: gh pr list --repo pendentive/whisper-sync --json number,title,state,labels,reviews
        └── Parses JSON response
        └── Compares against last known state
        └── If changed: show Windows toast notification + update menu
```

No webhooks, no servers, no infrastructure. Just `gh` CLI (already installed and authenticated).

## UI

### Settings submenu addition
```
Settings ►
  ├── ... existing items ...
  ├── ──────────────
  └── GitHub (2 open PRs) ►
        ├── #7: feat: add benchmark — awaiting review
        ├── #8: fix: config merge — 1 suggestion
        ├── ──────────────
        ├── Check now
        └── Open on GitHub
```

### Toast notifications (Windows native via pystray)
- "PR #7 auto-merged: feat: add benchmark"
- "PR #8 needs attention: Copilot found 1 suggestion"
- "PR #9 flagged for human review (complexity:high)"

### Tray icon badge (optional)
- Small dot overlay on tray icon when PRs need attention
- Clears when all PRs are merged or addressed

## Implementation

### Files to modify
- `__main__.py` — new `_github_status_thread()`, menu items in `_build_menu()`, toast notifications
- `config.py` / `config.defaults.json` — add `github_repo` key (default: null, set during install or settings)

### Dependencies
- `gh` CLI must be installed and authenticated
- No new Python packages needed (`subprocess` to call `gh`)

### Polling logic
```python
def _poll_github(self):
    """Poll GitHub for PR status changes every 5 minutes."""
    import subprocess, json
    while True:
        try:
            result = subprocess.run(
                ["gh", "pr", "list", "--repo", self.cfg.get("github_repo", ""),
                 "--json", "number,title,state,labels,reviewDecision,reviews",
                 "--limit", "10"],
                capture_output=True, text=True, timeout=15
            )
            if result.returncode == 0:
                prs = json.loads(result.stdout)
                self._update_pr_status(prs)
        except Exception:
            pass
        time.sleep(300)  # 5 minutes
```

### Status parsing
For each PR, determine status:
- Has Copilot inline suggestions → "needs attention"
- Has `complexity:high` label → "needs human review"
- Was auto-merged → "merged" (show once, then remove)
- Copilot reviewed, no suggestions → "will auto-merge" or already merged
- No review yet → "awaiting review"

### Config
```json
{
  "github_repo": "pendentive/whisper-sync",
  "github_poll_interval": 300,
  "github_notifications": true
}
```

Set during install (asks for repo) or via Settings menu. If `github_repo` is null, the feature is disabled — no polling, no menu item.

## Scope boundaries
- Read-only — no merging, commenting, or modifying PRs from the tray
- Only shows PRs for the configured repo
- Degrades gracefully if `gh` is not installed (feature simply disabled)
- No LLM cost — pure CLI + JSON parsing

## Testing
1. Open a PR → verify it appears in the tray menu within 5 minutes
2. Copilot reviews with suggestion → verify "needs attention" notification
3. PR auto-merges → verify "merged" notification
4. `gh` not installed → verify feature disabled, no errors
5. Network offline → verify no crashes, resumes when back online

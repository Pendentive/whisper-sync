# Contributing to WhisperSync

## Branch Naming

Use a prefix that describes the type of change:

| Prefix | Use for |
|--------|---------|
| `fix/` | Bug fixes (`fix/cuda-oom-retry`) |
| `feat/` | New features (`feat/auto-speaker-map`) |
| `test/` | Test additions or changes (`test/benchmark-models`) |
| `docs/` | Documentation only (`docs/readme-update`) |

## PR Process

1. **Create a branch** from `main` with the appropriate prefix
2. **Make your changes** -- keep PRs focused on a single concern
3. **Open a PR** against `main`
4. **Automated review runs** -- the CI workflow classifies your PR by complexity and posts a review comment
5. **Merge** after review passes

### Automated Review

The [review-pr workflow](.github/workflows/review-pr.yml) runs on every PR:

- **Triage step** -- counts changed files and lines, classifies complexity
- **Complexity labels** are auto-applied to the PR:

| Label | Criteria | Review |
|-------|----------|--------|
| `complexity:low` | <50 lines, <3 files | Automated review comment, merge-ready |
| `complexity:medium` | 50-200 lines, 3-5 files | Automated review comment, merge-ready |
| `complexity:high` | >200 lines or >5 files | Human review requested, @pendentive tagged |

Low and medium PRs get an automated review comment summarizing the change scope. High complexity PRs require human review.

## Labels

### Triage Labels

| Label | Meaning |
|-------|---------|
| `complexity:low` | Small change, auto-reviewed |
| `complexity:medium` | Medium change, auto-reviewed |
| `complexity:high` | Large change, needs human review |

### Component Labels (optional, applied manually)

| Label | Area |
|-------|------|
| `component:audio` | Audio capture, WASAPI, streaming WAV |
| `component:transcription` | WhisperX, worker, diarization |
| `component:ui` | Tray icon, hotkeys, paste |
| `component:config` | Config system, paths |
| `component:installer` | install.ps1, start.ps1, build scripts |

## Commit Message Format

Use conventional commit prefixes scoped to the project:

```
fix(whisper-sync): description of the fix
feat(whisper-sync): description of the feature
ci: description of CI/workflow change
docs: description of documentation change
```

Examples:
```
fix(whisper-sync): prevent CUDA OOM on long recordings by halving batch size
feat(whisper-sync): add speaker map persistence to transcript.json
ci: add complexity label to PR triage
docs: update README installation steps
```

Keep the first line under 72 characters. Add a blank line and longer description if needed.

## What NOT to Commit

The following are gitignored and must never be committed:

| Path | Reason |
|------|--------|
| `whisper_sync/config.json` | Per-machine user settings |
| `whisper_sync/logs/` | Runtime log files |
| `whisper_sync/models/` | Downloaded model cache |
| `whisper-env/` | Python virtual environment |
| `__pycache__/` | Python bytecode cache |
| `*.pyc` | Compiled Python files |
| `*.wav`, `*.mp4` | Audio/video recordings |

If you are unsure whether a file should be committed, check `.gitignore`.

## Testing

There is no automated test suite yet. Before submitting a PR, manually verify:

1. **Dictation mode** -- press Ctrl+Shift+Space, speak for 3-5 seconds, press again. Text should paste into the focused window.
2. **Meeting mode** -- press Ctrl+Shift+M, speak for 10+ seconds, press again. Transcript should be saved to disk.
3. **GPU memory** -- after 5+ dictations, VRAM should be stable (check with `nvidia-smi`).
4. **Crash recovery** -- kill the worker process. The main process should detect the crash and respawn the worker.

## Development Setup

See [docs/development.md](docs/development.md) for prerequisites, building from source, debugging, and common issues.

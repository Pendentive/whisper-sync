# WhisperSync

Hotkey-triggered audio capture and transcription that runs 100% locally. Two modes:

- **Dictation** -- press a hotkey, speak, press again. Your words are transcribed and pasted into whatever app is focused. Sub-second turnaround with a fast model.
- **Meeting recording** -- press a hotkey to record your mic + system audio (what you hear). Press again to stop, name the meeting, and get a full transcript with speaker labels saved to disk.

Runs as a system tray icon on Windows.

**What runs where:**
- **Dictation transcription** -- 100% local GPU. No network calls.
- **Meeting transcription + speaker identification** -- 100% local GPU. No network calls.
- **Meeting minutes / action items** -- requires an LLM (e.g., Claude). The transcript stays on your machine; only the text you choose to send leaves it.

---

## Features

- **Per-channel stereo diarization** -- 3-tier cascade for accurate speaker identification (see Diarization section)
- **Always-available dictation** -- dictate during active meeting recording without interruption
- **Tiered log window** -- Off / Normal / Detailed / Verbose with ANSI color coding
- **CPU/GPU device selection** -- Auto-detect, force GPU, or force CPU via tray menu
- **Dual-ring tray icon** -- inner circle = mic status, outer ring = speaker/loopback status
- **Incognito mode** -- RAM-only dictation; no transcription text logged or saved to disk
- **Persistent dictation history** -- last 10 dictations in tray menu, survives restarts
- **Session stats** -- dictation/meeting counts, averages, and uptime for the current session
- **Windows toast notifications** -- native Windows notifications for transcription events
- **GitHub PR status in tray** -- shows open PR count for a configured repo
- **GUI installer** -- 4-page wizard with GPU detection, dependency install, model bootstrap, and shortcut creation
- **Agentic governance learning loop** -- automated PR analysis feeds guardrail improvements (see Governance section)

---

## How It Works

| Component | Purpose |
|-----------|---------|
| [WhisperX](https://github.com/m-bain/whisperX) (faster-whisper) | Speech-to-text transcription |
| wav2vec2 alignment model | Word-level timing accuracy |
| [pyannote](https://github.com/pyannote/pyannote-audio) speaker diarization | Identifies who said what (meeting mode) |
| WASAPI loopback | Captures system audio without virtual cables |
| pystray | System tray icon and menu |

Models download once on first run and are cached locally. Subsequent launches load from cache -- works fully offline.

---

## Diarization Architecture

WhisperSync uses a **3-tier diarization cascade** for stereo recordings (mic + system audio captured as separate channels):

**Tier 1: Per-channel transcription + confidence fusion (~95% of meetings)**
- The stereo recording is split into mic channel and loopback channel
- Each channel is transcribed independently with WhisperX
- Segments are merged using energy-based confidence scoring
- Cross-channel bleed (hearing the remote speaker faintly on the mic channel) is treated as a confirmation signal, not noise

**Tier 2: RMS-balanced mono + PyAnnote (fallback)**
- If per-channel fusion produces low-confidence results, channels are mixed to mono with RMS balancing
- PyAnnote speaker diarization runs on the balanced mono signal

**Tier 3: Raw mono + PyAnnote (baseline)**
- Used for mono recordings (single-channel mic, no loopback)
- Standard PyAnnote diarization on the raw audio

The cascade selects the highest-confidence tier automatically. Stereo recordings almost always resolve at Tier 1 because channel separation provides a strong speaker signal without relying on voice embeddings.

---

## Always-Available Dictation

Dictation works even while a meeting is being recorded and transcribed:

- Meeting recording continues uninterrupted on the primary audio stream
- Dictation uses a configurable backup model (defaults to `base`) so it does not compete with the main worker transcribing the meeting
- If the backup model is unavailable, dictation falls back to the main worker queue with an extended timeout
- The tray icon flashes amber briefly when a dictation is queued behind a meeting transcription stage

---

## Governance Learning Loop

WhisperSync includes an agentic governance system that improves its own development guardrails over time:

- Every merged PR is logged and analyzed weekly by an automated agent
- The analysis agent proposes updates to `policy.yaml` and `.claude/rules/` based on patterns in merged changes
- Proposals are submitted as PRs for human review before taking effect

For the full design, see [docs/specs/2026-03-24-governance-learning-loop-design.md](docs/specs/2026-03-24-governance-learning-loop-design.md).

---

## System Requirements

| Requirement | Details |
|-------------|---------|
| **OS** | Windows 10 or 11 |
| **Python** | 3.10 or newer (3.13 recommended) |
| **GPU** | NVIDIA with CUDA support recommended (RTX 20/30/40/50 series, GTX 10-series). CPU-only mode available but 5-10x slower. |
| **VRAM** | 2 GB minimum (tiny/base models), 4 GB+ recommended, 8 GB+ for large-v3 |
| **Disk** | ~200 MB base install + model sizes (see table below) |
| **HF Account** | Free [Hugging Face](https://huggingface.co) account (required for meeting mode speaker diarization) |

---

## Installation

1. **Clone the repo:**
   ```powershell
   git clone https://github.com/Pendentive/whisper-sync.git
   cd whisper-sync
   ```

2. **Run the installer:**
   ```powershell
   # GUI installer (recommended)
   python -m whisper_sync.installer_gui

   # CLI installer (alternative)
   powershell -ExecutionPolicy Bypass -File install.ps1
   ```

3. **Follow the prompts.** The installer detects your GPU, creates a venv, installs dependencies, downloads base models, and offers desktop/startup shortcuts.

4. **Launch:**
   ```powershell
   powershell -ExecutionPolicy Bypass -File start.ps1
   ```

### Hugging Face Token (Meeting Mode)

Speaker diarization requires a free Hugging Face token. Skip this for dictation-only use.

1. Create an account at [huggingface.co/join](https://huggingface.co/join)
2. Accept the license on [pyannote/segmentation-3.0](https://huggingface.co/pyannote/segmentation-3.0) and [pyannote/speaker-diarization-3.1](https://huggingface.co/pyannote/speaker-diarization-3.1)
3. Generate a **Read** token at [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens)
4. Save it:
   ```powershell
   mkdir "$env:USERPROFILE\.huggingface" -Force
   "hf_YOUR_TOKEN_HERE" | Out-File -Encoding ASCII "$env:USERPROFILE\.huggingface\token" -NoNewline
   ```

---

## Usage

### Tray Icon

A dual-ring icon appears in your system tray. Inner circle = mic state, outer ring = speaker/loopback state.

| Inner Circle | State |
|--------------|-------|
| Gray | Idle |
| Blue | Dictating |
| Red | Meeting recording |
| Orange | Saving |
| Yellow | Transcribing |
| Green | Done |
| Magenta | Error |

| Outer Ring | Meaning |
|------------|---------|
| Gray | No speaker capture |
| Red | Speaker loopback active |
| Yellow | Speaker loopback failed |

### Hotkeys

| Hotkey | Action |
|--------|--------|
| **Ctrl+Shift+Space** | Toggle dictation |
| **Ctrl+Shift+M** | Toggle meeting recording |

### Click Actions

| Click | Default Action |
|-------|---------------|
| Left-click | Toggle meeting (or discard if dictating) |
| Middle-click | Toggle dictation |
| Right-click | Open settings menu |

### Settings

All settings via right-click tray icon. Key options: dictation/meeting hotkeys, paste method, click actions, dictation/meeting model, device (Auto/GPU/CPU), log window level, session stats, output folder.

**Recommended:** Use `base` for dictation (instant) and `large-v3` for meetings (best accuracy).

---

## Model Comparison

| Model | Size | Dictation (45s) | Meeting (30min) | VRAM |
|-------|:---:|:---:|:---:|:---:|
| **tiny** | ~75 MB | ~0.3s | ~28s | ~1 GB |
| **base** | ~150 MB | ~0.3s | ~28s | ~1 GB |
| **small** | ~500 MB | ~0.5s | ~30s | ~2 GB |
| **medium** | ~1.5 GB | ~0.7s | ~33s | ~4 GB |
| **large-v3** | ~3 GB | ~1.2s | ~39s | ~8 GB |

*Benchmarked on RTX 3090 with float16. Run `python -m whisper_sync.benchmark` to test your hardware.*

---

## Transcription Output

- **Dictation**: Text pasted into focused app. History saved to `<output_dir>/.whispersync/dictation-logs/YYYY-MM-DD.md`.
- **Meeting**: Saved to your transcriptions folder as `recording.wav` + `transcript.json` with timestamps and speaker IDs.

Use `python -m whisper_sync.flatten path/to/transcript.json` to generate a compact `transcript-readable.txt` (~90% smaller than the JSON).

---

## Speaker Identification

Out of the box, speakers are labeled `SPEAKER_00`, `SPEAKER_01`, etc. With Claude Code or any AI tool:

1. Scan transcript for name callouts ("Hey David", "Thanks Sarah")
2. Map generic IDs to real names
3. Save as `speaker_map` in `transcript.json`
4. Re-run `flatten` for named output

See the `.claude/rules/` directory for transcription workflow conventions used with Claude Code.

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| CUDA not available | Update NVIDIA drivers, run `nvidia-smi` to verify |
| No audio captured | Check Windows Sound Settings, ensure correct default mic |
| Slow transcription | Use smaller model, check GPU usage in Task Manager |
| Hotkey not working | Try a different combo in Settings, or run as Administrator |
| Diarization errors | Verify `~/.huggingface/token` exists, accept both pyannote model licenses |
| Config issues | Delete `<output_dir>/.whispersync/config.json` to reset to defaults |

Logs: `whisper_sync/logs/app/whisper-sync-YYYY-MM-DD.log`

### Reading the log

Each run emits a startup banner with PID, Python version, OS, and git SHA:

```
=== WhisperSync starting === pid=12840 python=3.13.0 os=Windows-11-10.0.26200 git=a1b2c3d
```

Every clean exit (tray quit, restart, SIGTERM, uncaught exception) emits a matching exit banner with a reason:

```
=== WhisperSync exiting === reason=user_quit
=== WhisperSync exiting === reason=exception type=RuntimeError
```

**Missing exit banner** between two startups = silent native crash. Check the Windows Application Event Log:

```powershell
Get-WinEvent -FilterHashtable @{LogName='Application'; ProviderName='Application Error'; StartTime=(Get-Date).AddHours(-1)} | Format-List Message
```

The faulting module name identifies the culprit (e.g. `tcl86t.dll` = Tk GUI thread, `torch_cuda.dll` = CUDA DLL load). WhisperSync also scans the Event Log on startup and logs any recent crashes it finds with the faulting module highlighted.

A `heartbeat` DEBUG line fires every 60 seconds; the last heartbeat before a silent death pins down time-of-death within a minute.

Meeting post-processing logs each step start/end at INFO (`step start: transcribe job=...`, `step done: transcribe job=... elapsed=12.3s`) so a crash mid-step reveals which step was active.

---

## Updating

```powershell
cd whisper-sync
git pull
```

Settings, models, and logs are preserved. Only re-run the installer if release notes mention dependency changes.

---

## Technical Reference

For architecture details, file reference, dependency deep-dive, configuration reference, and AI assistant troubleshooting playbooks, see [docs/TECHNICAL.md](docs/TECHNICAL.md).

---

## License

MIT License -- see [LICENSE](LICENSE)

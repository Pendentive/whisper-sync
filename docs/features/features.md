# Features

The living feature catalog. Update this file IN THE SAME PR as any
feature change (add/remove/behavior change) - the installer and README
treat this folder as the source of truth for what the app does.
`tests/test_feature_docs.py` enforces the mechanical parts (defaults
and hotkeys); the prose here is on the PR author.

Companion files: [shortcuts.md](shortcuts.md) (how to interact),
[defaults.md](defaults.md) (every setting and its default).

## Core

- **Dictation** - hotkey-toggled speech-to-text pasted into the focused
  window (clipboard or simulated keystrokes). Streams to disk for crash
  recovery; auto-stops at a configurable cap so a forgotten hotkey
  cannot grow unbounded.
- **Meeting recording** - mic + speaker loopback captured to a stereo
  WAV, then a sequential post-processing pipeline: transcription with
  diarization, speaker identification (Claude CLI with manual
  fallback), readable transcript, minutes generation, rename
  suggestion. Recording never waits on the pipeline.
- **Per-channel stereo diarization** - 3-tier cascade
  (balanced mix, per channel, raw audio) selectable per meeting from
  the save dialog.
- **Always-available dictation** - dictate during an active meeting via
  a second mic stream and the backup model (CPU or secondary GPU).
- **Feature suggestions** - a dedicated hotkey records a voice note to
  the feature log and formats it via Claude CLI.

## Model and VRAM lifecycle

- **Model auto-sleep** - the worker (and all of its VRAM) unloads after
  `auto_sleep_minutes` of inactivity (default 30, 0 disables), or on
  demand by double-clicking the tray icon (for gaming). Any dictation
  or meeting action wakes it with the yellow loading flash; meetings
  start recording immediately while the model loads. Sleeping shows
  the deep-gray sleep icon.
- **GPU Guard** - VRAM watermark polling with a sticky model downgrade
  ladder (large-v3 -> medium -> small -> base) on low-VRAM readings or
  worker crashes. Vendor-agnostic probes. All events (downgrades,
  suspend/resume, sleep/wake, mic stalls) land in `gpu-guard.jsonl`
  for crash-time correlation.
- **CPU/GPU device selection** - auto-detect, force GPU, or force CPU
  from the tray menu; backup model has independent device/model
  settings.

## Resilience

- **Crash-safe audio** - all recordings stream to disk; orphaned WAVs
  are detected at startup and offered for recovery (dictations to
  clipboard, meetings through the naming dialog and pipeline).
- **Worker crash recovery** - the transcription subprocess is respawned
  on crash; wedged workers are detected via liveness pings and killed
  after 90s of silence.
- **Single-instance guard** - a named mutex prevents duplicate tray
  instances; orphaned worker processes from a previous crash are
  reaped at startup.
- **Suspend/resume awareness** - sleep/resume events are logged and the
  worker is health-checked (and restarted if needed) after resume.
- **Mic stall detection** - a toast fires when the mic stops delivering
  audio mid-recording (device unplugged, Bluetooth drop).
- **Self-update** - tray menu pulls the latest main/dev and restarts.

## Quality of life

- **Three-ring tray icon** - inner dot = overlay dictation, middle =
  mic/mode, outer ring = speaker loopback health; deep-gray middle =
  model asleep (canonical table: `.claude/rules/ui-patterns.md`).
- **Incognito (Whisper) mode** - RAM-only dictation; nothing logged or
  written to disk.
- **Persistent dictation history** - last 10 dictations in the tray
  menu, survives restarts.
- **Weekly + session stats** - counts, character totals, uptime.
- **Windows toast notifications** - configurable per event type, with
  action buttons (recover, merge PR, rename meeting).
- **GitHub PR status** - open-PR poller with tray menu section, change
  toasts, and one-click gh merge.
- **Meeting split tool** - `python -m whisper_sync.split_meeting`
  divides a recorded meeting into portions with re-flattened
  transcripts (safe against name collisions).

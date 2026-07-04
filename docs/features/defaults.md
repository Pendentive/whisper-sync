# Settings and defaults

Every shipped defaults key (whisper_sync/config.defaults.json) with its default value. The source of truth
is `whisper_sync/config.defaults.json`; user overrides live in
`<output_dir>/.whispersync/config.json`. Update IN THE SAME PR as any
key addition/removal/default change - `tests/test_feature_docs.py`
fails if this table and the defaults file disagree on keys.

| Key | Default | Meaning |
|-----|---------|---------|
| `hotkeys` | dictation `ctrl+shift+space`, meeting `ctrl+shift+m`, feature `ctrl+shift+alt+f` | Global hotkeys (see shortcuts.md) |
| `paste_method` | `clipboard` | How dictation text is delivered: clipboard+Ctrl+V or simulated keystrokes |
| `language` | `en` | Transcription language |
| `model` | `large-v3` | Meeting transcription model |
| `dictation_model` | `large-v3` | Dictation model (may be auto-downgraded by GPU Guard) |
| `compute_type` | `float16` | Whisper compute precision |
| `output_dir` | `transcriptions` | Meeting output root (relative paths resolve from the repo root) |
| `mic_device` | `null` | Explicit mic device id (null = default) |
| `speaker_device` | `null` | Explicit loopback device id (null = default) |
| `sample_rate` | `16000` | Capture sample rate (Hz) |
| `batch_size` | `auto` | Whisper batch size |
| `use_system_devices` | `true` | Follow Windows default devices instead of the explicit ids |
| `left_click` | `meeting` | Tray single-left-click action (`meeting` / `dictation` / `none`) |
| `middle_click` | `dictation` | Reserved: no middle-click plumbing yet (docs/BACKLOG.md) |
| `auto_sleep_minutes` | `30` | Idle minutes before the model unloads from VRAM (0 = never) |
| `github_repo` | `null` | `owner/repo` for PR status in the tray (null = off) |
| `github_poll_interval` | `120` | PR poll interval (seconds) |
| `github_notifications` | `true` | Toasts on PR review-state changes |
| `log_window` | `normal` | Console verbosity: off / normal / detailed / verbose |
| `device` | `auto` | Compute device: auto / cuda / cpu |
| `incognito` | `false` | Whisper mode: RAM-only dictation, nothing on disk |
| `always_available_dictation` | `true` | Allow dictation during meetings via the backup model |
| `backup_device` | `cpu` | Device for the backup transcriber |
| `backup_model` | `base` | Model for the backup transcriber |
| `diarize_primary` | `balanced_mix` | First diarization method |
| `diarize_fallback` | `per_channel` | Second diarization method |
| `diarize_last_resort` | `raw_audio` | Final diarization fallback |
| `dictation_max_minutes` | `30` | Auto-stop cap for a forgotten dictation hotkey (0 = off) |
| `gpu_guard_single_instance` | `true` | Named-mutex single instance + orphan worker reaping |
| `gpu_guard` | `true` | VRAM watchdog + model downgrade ladder |
| `gpu_guard_low_vram_mb` | `750` | Free-VRAM watermark that arms a downgrade |
| `gpu_guard_poll_seconds` | `30` | VRAM poll interval |
| `gpu_guard_ladder` | `["large-v3", "medium", "small", "base"]` | Downgrade order as a JSON list (floor = last entry); non-list values are rejected and fall back to this default |
| `gpu_guard_probe` | `null` | Force a probe backend (null = auto: pynvml, nvidia-smi) |

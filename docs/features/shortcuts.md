# Shortcuts and interactions

Every way to drive the app. Update IN THE SAME PR as any hotkey or
click-behavior change; `tests/test_feature_docs.py` verifies the
default hotkeys below match `config.defaults.json`.

## Hotkeys (defaults, configurable in Settings > Hotkeys)

| Hotkey | Action |
|--------|--------|
| `ctrl+shift+space` | Toggle dictation (start / stop-and-paste) |
| `ctrl+shift+m` | Toggle meeting recording (start / stop-and-save) |
| `ctrl+shift+alt+f` | Toggle feature-suggestion recording |

During a meeting, the dictation and feature hotkeys record through the
backup model (overlay dictation) without touching the meeting audio.
While the model is asleep (or still loading at startup), any of these
wakes it AND starts recording immediately - recordings are disk-first,
so capture never waits on the model. A dictation stopped before the
model is ready just shows the yellow transcribing state a little
longer. Exception: whisper mode (RAM-only) still requires a loaded
model.

## Tray icon clicks

| Gesture | Action |
|---------|--------|
| Left click (single) | Configured action (default: toggle meeting; `left_click` setting). While dictating: discard the recording. |
| Left double-click | Sleep / wake the model (unload from or reload into VRAM) |
| Right click | Open the menu |

The single-click action fires after a short double-click window
(~450ms), so one gesture never triggers both. Note: `middle_click`
exists in config but currently has no plumbing (see docs/BACKLOG.md).

## Tray menu highlights

- **Meetings** - recent meetings with per-meeting speaker re-identify.
- **Recent Dictations** - last 10; click to copy, plus Open Logs /
  Clear History.
- **Sleep Model / Wake Model** - same as double-click.
- **Settings** - devices, models, hotkeys, paste method, click actions,
  compute device, diarization, auto-sleep, notifications, whisper
  mode, log window, output folder.
- **Update** - self-update from main (stable) or dev (labs), then
  restart.

## Icon states (summary)

Gray ring + gray middle = idle. Deep-gray middle = model asleep. Red =
recording meeting. Green = dictating. Blue/animated = transcribing.
Yellow double-flash = model loading or request queued. Broken outer
ring = speaker loopback unavailable. Full table:
`.claude/rules/ui-patterns.md`.

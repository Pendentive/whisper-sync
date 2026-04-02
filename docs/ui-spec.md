# WhisperSync UI Specification

> This document is the authoritative map of every user-visible element, interaction route, and state transition in WhisperSync. Agents and contributors read this before making any UI change.

---

## Platform Abstraction

| Layer | Current (Windows) | Future (Mac/Linux) |
|-------|-------------------|-------------------|
| System tray | `pystray` (Win32) | `pystray` (AppIndicator/macOS native) |
| Dialogs | `tkinter` | `tkinter` (cross-platform, or native via PyObjC/GTK) |
| Hotkeys | `keyboard` (low-level Win32 hooks) | Needs platform-specific solution |
| Audio capture | `sounddevice` + `PyAudioWPatch` (WASAPI loopback) | `sounddevice` (PortAudio, no loopback on Mac) |
| Clipboard | `pyperclip` | `pyperclip` (cross-platform) |
| File paths | Windows paths, `Path.home()` | Same via `pathlib` |

**Design principle:** All UI logic lives in `__main__.py`. Platform-specific code should be isolated into importable helpers so a Mac port can swap implementations without rewriting the UI flow.

---

## Component Inventory

### 1. System Tray Icon

**File:** `__main__.py` (WhisperSync class) + `icons.py`

The tray icon is the only persistent UI element. It communicates state via color:

| State | Icon Color | Meaning |
|-------|-----------|---------|
| `None` (idle) | Gray | Ready for input |
| `dictation` | Blue | Dictation recording active |
| `meeting` | Red | Meeting recording active |
| `saving` | Amber | Saving audio to disk |
| `transcribing` | Amber | Transcription in progress |
| `done` | Green | Last operation succeeded |
| `error` | Red (error variant) | Last operation failed |
| `queued` | Amber (queued variant) | Dictation queued while meeting transcribes |

**Interactions:**
| Input | Action | Handler |
|-------|--------|---------|
| Left-click | Configurable action (default: toggle meeting) | `_on_left_click()` |
| Middle-click | Configurable action (default: toggle dictation) | `_on_middle_click()` |
| Right-click | Open context menu | `_build_menu()` (pystray auto) |

---

### 2. Context Menu (Right-Click)

**File:** `__main__.py` → `_build_menu()`

The menu is rebuilt on every open via `_refresh_menu()`. Structure:

```
┌─────────────────────────────────────────┐
│ Meetings ►                              │  → _build_meetings_menu()
│   ├── 0401_Abhi-11       No speakers    │  → _recover_meeting_speakers(dir)
│   ├── 0401_retro         Colby, Vinod   │  → _recover_meeting_speakers(dir)
│   └── ...  (10 most recent)             │
│ Dictation              Ctrl+Shift+Space │  → toggle_dictation()
│ Meeting                Ctrl+Shift+M     │  → toggle_meeting()
│ ─────────────────────────────────────── │
│ Mic Input              system           │  → _set_device("mic_device", id)
│ Speaker Output         system           │  → _set_device("speaker_device", id)
│ Always Use System Devices  ☑            │  → _toggle_system_devices()
│ Device Filter (Windows WASAPI) ►        │  → _set_api_filter(name)
│ ─────────────────────────────────────── │
│ Open Output Folder                      │  → _open_output_folder()
│ Settings ►                              │
│   ├── Dictation Hotkey ►                │  → _set_hotkey("dictation_toggle", hk)
│   ├── Meeting Hotkey ►                  │  → _set_hotkey("meeting_toggle", hk)
│   ├── Paste Method ►                    │  → _set_paste_method(method)
│   ├── ──────────────                    │
│   ├── Left Click ►                      │  → _set_click("left_click", action)
│   ├── Middle Click ►                    │  → _set_click("middle_click", action)
│   ├── ──────────────                    │
│   ├── Dictation Model ►                 │  → _set_model("dictation_model", name)
│   ├── Meeting Model ►                   │  → _set_model("model", name)
│   ├── Diarization (Speaker Detection) ► │  → submenu with Primary/Fallback/Last Resort
│   │   ├── Primary ►     Balanced Mix    │  → _set_diarize_method("diarize_primary", m)
│   │   ├── Fallback ►    Per-Channel     │  → _set_diarize_method("diarize_fallback", m)
│   │   └── Last Resort ► Raw Audio       │  → _set_diarize_method("diarize_last_resort", m)
│   ├── ──────────────                    │
│   └── Download [model] (~X GB)          │  → _download_model_bg(name)
│ Restart                                 │  → _restart()
│ Quit                                    │  → quit()
└─────────────────────────────────────────┘
```

---

### 3. Dialogs (tkinter)

All dialogs share a consistent dark theme via `_style_window()` and `_flat_button()`.

**Theme constants:**
```python
bg = "#1e1e2e"        # Background (Catppuccin Mocha base)
fg = "#cdd6f4"        # Primary text
fg_dim = "#6c7086"    # Secondary text
accent = "#89b4fa"    # Primary action (blue)
danger = "#f38ba8"    # Destructive action (red/pink)
entry_bg = "#313244"  # Input field background
hover_bg = "#585b70"  # Button hover
```

**Helper methods:**
| Method | Purpose |
|--------|---------|
| `_style_window(root)` | Apply dark theme, topmost, non-resizable |
| `_center_window(root)` | Center dialog on screen |
| `_flat_button(parent, text, command, ...)` | Borderless button (Label with click/hover bindings) |

#### Dialog: Save Meeting (`_ask_meeting_name`)

**When:** After stopping a meeting recording
**Shows:** Text entry for meeting name + diarization method selector + 3 buttons
**Returns:** `_ABORT` or `(name: str, summarize: bool, diarize_method: str | None)`

```
┌──────────────────────────────────────┐
│     Save Meeting Recording           │
│                                      │
│  Meeting name (leave blank for       │
│  default):                           │
│  ┌──────────────────────────────┐    │
│  │ architecture-review          │    │
│  └──────────────────────────────┘    │
│                                      │
│  Diarization: [Balanced Mix] [Per-Channel] [Raw Audio] │
│                                      │
│  [Discard]   [Save]   [Save & Sum]   │
└──────────────────────────────────────┘
```

The diarization selector defaults to the configured `diarize_primary` method. Selecting a different method overrides the fallback chain for this recording only (passed as `force_method` to `stage_diarize()`). Selecting the default returns `None` (no override).

**Route:** `toggle_meeting()` → stop recording → `_ask_meeting_name()` → `_process()` (transcription)

#### Dialog: Rename Meeting (`_ask_rename_suggestion`)

**When:** After minutes generation, if Claude suggests a better name
**Shows:** AI-generated name suggestions as clickable buttons + Keep Original
**Returns:** Chosen name string or `None` to skip

**Route:** `_process()` → minutes generated → `_ask_rename_suggestion()` → rename folder

#### Dialog: Identify Speakers (`_ask_identify_speakers` in speakers.py)

**When:** After transcription, before minutes generation
**Shows:** Speaker labels from transcript with text entries to assign real names
**Returns:** Speaker map dict `{"SPEAKER_00": "Colby", ...}`

**Route:** `_process()` → transcription done → `identify_speakers()` → `_ask_identify_speakers()` → write speaker map → continue

#### Dialog: Download Model Confirmation

**When:** User selects a model that isn't cached
**Shows:** Confirmation with model name and size
**Returns:** Boolean (proceed or cancel)

**Route:** Settings menu → model selection → `_set_model()` → check cache → confirm dialog → `_download_model_bg()`

#### Dialog: Error Popup (`_show_error_popup`)

**When:** Any error the user needs to see
**Shows:** Error title + message + OK button

---

## Routing Architecture

### Handler Pattern

All menu actions follow the same pattern:

```
Menu item click
  → self._cb(handler_method, *args)    # wraps in lambda for pystray
    → handler_method(*args)             # modifies self.cfg
      → self._save_and_refresh()        # persists config + rebuilds menu
```

`_cb()` is the callback wrapper that creates a lambda compatible with pystray's callback signature.

`_save_and_refresh()` is the standard termination: `config.save(self.cfg)` + `_refresh_menu()`.

### Adding a New Menu Item

1. Define the handler method on WhisperSync (e.g., `_change_output_folder`)
2. Add the menu item in `_build_menu()` at the appropriate location
3. If it opens a dialog: create the dialog method following the existing pattern:
   - Run dialog in a `threading.Thread(daemon=True)` to avoid blocking the tray
   - Use `threading.Event` to synchronize result back to caller
   - Apply `_style_window()` for consistent theming
   - Use `_flat_button()` for buttons
   - Use `_center_window()` for positioning
4. End with `_save_and_refresh()` if config changed

### Adding a New Dialog

1. Create the dialog method: `def _ask_<name>(self) -> <return_type>`
2. Follow the threading pattern from `_ask_meeting_name`:
   ```python
   result = [default_value]
   event = threading.Event()
   def _show():
       import tkinter as tk
       root = tk.Tk()
       root.title("WhisperSync")
       self._style_window(root)
       # ... build UI ...
       self._center_window(root)
       root.mainloop()
       event.set()
   t = threading.Thread(target=_show, daemon=True)
   t.start()
   event.wait(timeout=60)
   return result[0]
   ```
3. Use theme constants from the Theme section above
4. Buttons: `_flat_button()` — never use native `tk.Button` (Windows bevel looks bad)
5. Bind Enter/Escape for keyboard shortcuts

---

## State Machine

The `self.mode` attribute controls the app's state:

```
                    ┌──────────────┐
                    │  None (idle) │
                    └──────┬───────┘
                           │
              ┌────────────┼────────────┐
              ▼                         ▼
    ┌──────────────┐          ┌──────────────┐
    │  "dictation" │          │   "meeting"  │
    │  (recording) │          │  (recording) │
    └──────┬───────┘          └──────┬───────┘
           │                         │
           ▼                         ▼
    ┌──────────────┐          ┌──────────────┐
    │"transcribing"│          │   "saving"   │
    │ (worker busy)│          │ (to disk)    │
    └──────┬───────┘          └──────┬───────┘
           │                         │
           ▼                         ▼
    ┌──────────────┐          ┌──────────────┐
    │    "done"    │          │"transcribing"│
    │  (text pasted│          │ (full pipe)  │
    └──────┬───────┘          └──────┬───────┘
           │                         │
           │                         ▼
           │                  ┌──────────────┐
           │                  │    "done"    │
           │                  │ (files saved)│
           │                  └──────┬───────┘
           │                         │
           └────────┬────────────────┘
                    ▼
           (timer → back to None)
```

**Transitions:**
| From | To | Trigger | Handler |
|------|----|---------|---------|
| None | dictation | Hotkey or click | `toggle_dictation()` |
| None | meeting | Hotkey or click | `toggle_meeting()` |
| dictation | transcribing | Hotkey (stop) | `toggle_dictation()` → worker |
| transcribing | done | Worker returns | `_process()` callback |
| done | None | 3s timer | `_schedule_idle()` |
| meeting | saving | Hotkey (stop) | `toggle_meeting()` |
| saving | transcribing | Dialog complete | `_process()` |
| transcribing | done | Pipeline complete | `_process()` callback |
| Any | error | Exception | error handler |
| error | None | 5s timer | `_schedule_idle()` |

**Guard:** `_schedule_idle()` checks current mode before transitioning to None — prevents race conditions where a new recording starts during the timer.

---

## Extension Points

| Want to... | Where to add | Pattern to follow |
|-----------|-------------|-------------------|
| New menu item (simple toggle) | `_build_menu()` | Existing checkbox/radio items |
| New menu item (opens dialog) | `_build_menu()` + new `_ask_*` method | `_ask_meeting_name()` pattern |
| New settings submenu | `_build_menu()` Settings section | Existing hotkey/model submenus |
| New tray icon state | `icons.py` + `_update_icon()` | Existing icon functions |
| New mode in state machine | `self.mode` assignments + `_update_icon()` | Follow transition table above |
| New post-transcription step | `_process()` pipeline | After diarization, before done |
| New audio source | `capture.py` | Extend `AudioRecorder` |
| New output format | `flatten.py` or new module | Follow `flatten()` pattern |
| Platform-specific behavior | New module, import conditionally | `import sys; if sys.platform == 'win32': ...` |

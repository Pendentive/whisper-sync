# UI Patterns Rules

## Tray Menu

The tray menu is built in `_build_menu()` and refreshed via `_refresh_menu()`. Menu ordering is intentional and must be preserved:

1. Recent Dictations (submenu)
2. Separator
3. Dictation toggle (with hotkey shown via tab-aligned label)
4. Meeting toggle (with hotkey shown via tab-aligned label)
5. Separator
6. Mic Input / Speaker Output device selection
7. Separator
8. GitHub PR status (dynamic, when polling is enabled)
9. Open Output Folder
10. Separator
11. Settings (submenu with hotkeys, models, device, output folder, log window, session stats)
12. Separator
13. Restart / Quit

## Right-Aligned State Labels

Menu items use tab characters (`\t`) to right-align state information. For example:
- `f"Dictation\t{dict_hk}"` shows the hotkey on the right
- `f"Device\t{current_device} - {gpu_name or 'CPU'}"` shows the device on the right
- `f"Log Window\t{self.cfg.get('log_window', 'normal')}"` shows the current tier

This is a pystray convention. The tab character triggers right-alignment in the system tray menu renderer.

## Pystray Limitations

- `pystray` does not support dynamic menu updates without full menu rebuild. `_refresh_menu()` replaces the entire menu via `self.tray.update_menu()`.
- Menu item callbacks must be simple lambdas or bound methods. Complex state changes must be dispatched to background threads.
- Enabled/disabled state is set at construction time via the `enabled` parameter.
- Separators are `pystray.Menu.SEPARATOR`.
- Submenus are created by passing a `pystray.Menu(...)` as the second argument to `pystray.MenuItem`.

## Dialog Conventions

All dialogs use a consistent dark theme (Catppuccin Mocha palette):
- Background: `#1e1e2e`
- Text: `#cdd6f4`
- Dim text: `#6c7086`
- Muted text: `#a6adc8`
- Accent: `#89b4fa`
- Danger: `#f38ba8`
- Entry background: `#313244`
- Card background: `#181825`

Shared helpers in `WhisperSync`:
- `_style_window(root)` - applies dark theme, topmost, non-resizable
- `_center_window(root)` - centers on screen after `update_idletasks()`
- `_flat_button(parent, ...)` - creates flat, borderless buttons using Labels (avoids Windows tk.Button bevel). Supports hover color changes.

All dialogs are shown on background threads with `threading.Event()` for synchronization. The main thread never blocks on dialog input. Dialogs have timeout guards (typically 60-120s).

Button ordering convention: destructive action on the left, neutral in the middle, primary action on the right. Buttons are packed RIGHT to LEFT so the rightmost (primary) button is packed first.

## Log Window Tiers

Four tiers controlled by the `log_window` config key:
- **off** - console output suppressed entirely
- **normal** - standard operation messages (dictation results, meeting status)
- **detailed** - includes transcription content previews
- **verbose** - full debug output, prefixed with `[WhisperSync]`

File logging always captures DEBUG level regardless of console tier. Log files rotate daily.

## Tray Icon Anatomy (Three-Ring Model)

The tray icon uses a layered three-ring design:

- **Outer ring** (3px): reflects speaker/channel health status.
- **Middle circle**: primary indicator for mic status and recording state.
- **Inner dot** (4px, optional): dictation overlay indicator. Appears ONLY during overlay dictation (dictation while a meeting is active).

### Color State Table

| State | Outer Ring | Middle Circle | Inner Dot |
|-------|-----------|---------------|-----------|
| Idle | Gray | Gray | None |
| Recording (meeting) | Green | Red | None |
| Dictation (standalone) | Gray | Blue | None |
| Dictation (overlay, during meeting) | Green | Red | Blue |
| Transcribing | Gray | Yellow | None |
| Done (flash) | Gray | Green | None |
| Error | Gray | Red (pulse) | None |

### Yellow Double-Flash Convention

The yellow double-flash is the universal "loading/queuing" signal. Timing: 150ms on, 150ms off, 150ms on. Used when a hotkey press is received while a model is still loading or a previous operation is queued.

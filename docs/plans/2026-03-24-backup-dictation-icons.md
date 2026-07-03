# Backup Dictation, Device Management, and Icon Redesign - Implementation Plan

> Status: SHIPPED (2026-03; three-ring inner dot, icons.py). Committed 2026-07-03 (previously an untracked draft).

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix VRAM doubling during meeting dictation, add hotkey debounce with yellow flash, redesign tray icon to three concentric rings, fix device label bug.

**Architecture:** The backup model always runs on CPU (config default changed from "auto" to "cpu"). Pre-loads when meeting starts via background thread. Icon uses outer ring (speaker) + middle circle (mic/status) + optional inner dot (dictation overlay). Yellow double-flash (150ms on/off/on) is the universal loading signal.

**Tech Stack:** Python 3.13, PIL/Pillow, pystray, faster-whisper, numpy

**Spec:** `docs/specs/2026-03-24-backup-dictation-icons-design.md`

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `whisper_sync/backup_worker.py` | Modify | Remove _resolve_device() auto logic, force CPU, add preload() and is_loading |
| `whisper_sync/__main__.py` | Modify | Pre-load on meeting start, debounce hotkey, yellow flash, fix device label |
| `whisper_sync/icons.py` | Modify | Three-ring icon with outer/middle/inner dot geometry |
| `whisper_sync/config.defaults.json` | Modify | Change backup_device default to "cpu" |
| `.claude/rules/audio-pipeline.md` | Modify | Document backup lifecycle |
| `.claude/rules/ui-patterns.md` | Modify | Document three-ring states, yellow flash |
| `.claude/rules/testing.md` | Modify | Add backup dictation test checklist |

---

### Task 1: Force backup model to CPU

**Files:**
- Modify: `whisper_sync/backup_worker.py:118-166`
- Modify: `whisper_sync/config.defaults.json`

- [ ] **Step 1: Change config default**

In `config.defaults.json`, change `"backup_device": "auto"` to `"backup_device": "cpu"`.

- [ ] **Step 2: Simplify _resolve_device()**

Replace the entire `_resolve_device()` method (lines 118-166) with:

```python
def _resolve_device(self) -> str:
    """Determine device for backup model. Always CPU unless explicitly overridden."""
    backup_device = self.cfg.get("backup_device", "cpu")

    if backup_device in ("gpu", "cuda"):
        # Respect explicit GPU override but warn if main model is also on GPU
        main_device = self.cfg.get("device", "auto")
        if main_device != "cpu":
            logger.warning("Backup model on GPU while main model also on GPU - may cause VRAM pressure")
        return "cuda"

    return "cpu"
```

- [ ] **Step 3: Verify no other code references the old auto logic**

Search for `VRAM_THRESHOLD`, `MODEL_VRAM_GB` in backup_worker.py. If they exist only for `_resolve_device()`, remove the constants too.

- [ ] **Step 4: Commit**

```bash
git add whisper_sync/backup_worker.py whisper_sync/config.defaults.json
git commit -m "fix: force backup model to CPU, remove VRAM auto logic"
```

---

### Task 2: Add preload() and is_loading to BackupTranscriber

**Files:**
- Modify: `whisper_sync/backup_worker.py`

- [ ] **Step 1: Add _loading flag and is_loading property**

After `__init__`, add:

```python
self._loading = False

@property
def is_loading(self) -> bool:
    return self._loading
```

- [ ] **Step 2: Add preload() method**

```python
def preload(self):
    """Pre-load backup model in background thread. Called when meeting starts."""
    if self._model is not None or self._loading:
        return  # already loaded or loading
    import threading
    def _do_preload():
        self._loading = True
        try:
            self._load()
        finally:
            self._loading = False
    threading.Thread(target=_do_preload, daemon=True, name="backup-preload").start()
```

- [ ] **Step 3: Update _load() log messages**

Change "Backup worker spawned" to "Backup model loading" and "Backup worker ready" to "Backup model ready" (if not already done).

- [ ] **Step 4: Commit**

```bash
git add whisper_sync/backup_worker.py
git commit -m "feat: add preload() and is_loading to BackupTranscriber"
```

---

### Task 3: Pre-load backup on meeting start + debounce hotkey

**Files:**
- Modify: `whisper_sync/__main__.py:269-290` (toggle_dictation)
- Modify: `whisper_sync/__main__.py:732+` (_start_meeting)

- [ ] **Step 1: Add preload call to _start_meeting()**

In `_start_meeting()` (line 732), after the meeting recording setup, add:

```python
# Pre-load backup model for dictation during meeting
if self.cfg.get("always_available_dictation", True):
    self._backup.preload()
```

- [ ] **Step 2: Add debounce check to toggle_dictation()**

In `toggle_dictation()` (line 269), inside the meeting state branch, before `_start_overlay_dictation()`, add:

```python
if self._backup.is_loading:
    logger.debug("Backup model still loading, triggering yellow flash")
    self._yellow_flash()
    return
```

- [ ] **Step 3: Implement _yellow_flash() method**

Add to WhisperSync class:

```python
def _yellow_flash(self):
    """Universal loading/queuing signal: two quick yellow flashes."""
    import threading
    def _flash():
        from .icons import yellow_flash_icon
        original = self.tray.icon
        for _ in range(2):
            self.tray.icon = yellow_flash_icon()
            import time; time.sleep(0.15)
            self.tray.icon = original
            time.sleep(0.15)
    threading.Thread(target=_flash, daemon=True).start()
```

- [ ] **Step 4: Commit**

```bash
git add whisper_sync/__main__.py
git commit -m "feat: pre-load backup on meeting start, debounce with yellow flash"
```

---

### Task 4: Fix device label bug

**Files:**
- Modify: `whisper_sync/__main__.py:2429+` (_get_device_label)

- [ ] **Step 1: Fix _get_device_label()**

Find `_get_device_label()` (line 2429). Update it to show the resolved active device, not always the GPU name:

```python
def _get_device_label(self) -> str:
    device_setting = self.cfg.get("device", "auto")
    if device_setting == "cpu":
        return "CPU"
    elif device_setting in ("gpu", "cuda"):
        gpu_name = get_gpu_name()
        return gpu_name if gpu_name else "GPU"
    else:  # auto
        gpu_name = get_gpu_name()
        if gpu_name:
            return f"Auto ({gpu_name})"
        return "Auto (CPU)"
```

- [ ] **Step 2: Verify the Settings menu reflects the fix**

Check that wherever `_get_device_label()` is called in menu building, it uses the updated method.

- [ ] **Step 3: Commit**

```bash
git add whisper_sync/__main__.py
git commit -m "fix: device label shows active device, not always GPU"
```

---

### Task 5: Three-ring icon with inner dot

**Files:**
- Modify: `whisper_sync/icons.py`

- [ ] **Step 1: Replace _make_dual_icon with _make_three_ring_icon**

Replace `_make_dual_icon()` (line 23) with:

```python
def _make_three_ring_icon(outer_color: str, middle_color: str,
                          inner_color: str | None = None, size: int = 64) -> Image.Image:
    """Generate icon with outer ring + middle filled circle + optional inner dot.

    - Outer ring: 3px wide, 2px margin
    - Gap: 2px transparent
    - Middle circle: filled, remaining space
    - Inner dot: ~4px radius, centered (only for overlay dictation)
    """
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    margin = 2
    ring_width = 3
    gap = 2

    # Outer ring
    outer_r = size - margin
    draw.ellipse([margin, margin, outer_r - 1, outer_r - 1], fill=outer_color)
    inner_of_ring = margin + ring_width
    draw.ellipse(
        [inner_of_ring, inner_of_ring,
         outer_r - 1 - ring_width, outer_r - 1 - ring_width],
        fill=(0, 0, 0, 0),
    )

    # Middle filled circle (after gap)
    mid_start = margin + ring_width + gap
    mid_end = size - margin - ring_width - gap
    if mid_end > mid_start:
        draw.ellipse([mid_start, mid_start, mid_end - 1, mid_end - 1], fill=middle_color)

    # Inner dot (overlay dictation indicator)
    if inner_color is not None:
        dot_radius = 4
        cx, cy = size // 2, size // 2
        draw.ellipse(
            [cx - dot_radius, cy - dot_radius, cx + dot_radius, cy + dot_radius],
            fill=inner_color,
        )

    return img
```

- [ ] **Step 2: Add yellow_flash_icon function**

```python
def yellow_flash_icon(size: int = 64) -> Image.Image:
    return _make_three_ring_icon("#FFCC00", "#FFCC00", size=size)
```

- [ ] **Step 3: Update all public icon functions**

Update each function to use `_make_three_ring_icon()` with the color states from the spec table. Example:

```python
def recording_icon(speaker_ok: bool = True, size: int = 64) -> Image.Image:
    outer = "#CC3333" if speaker_ok else "#FFAA00"
    return _make_three_ring_icon(outer, "#FF4444", size=size)

def dictation_during_recording_icon(speaker_ok: bool = True, size: int = 64) -> Image.Image:
    outer = "#CC3333" if speaker_ok else "#FFAA00"
    return _make_three_ring_icon(outer, "#FF4444", inner_color="#4488FF", size=size)
```

Apply the same pattern for all 11 public functions per the spec color table.

- [ ] **Step 4: Remove old _make_dual_icon and _make_overlay_icon**

Delete `_make_dual_icon()` and `_make_overlay_icon()`. Update `_circle_icon()` legacy wrapper to use `_make_three_ring_icon(color, color)`.

- [ ] **Step 5: Commit**

```bash
git add whisper_sync/icons.py
git commit -m "feat: three-ring icon with outer/middle/inner dot"
```

---

### Task 6: Update documentation

**Files:**
- Modify: `.claude/rules/audio-pipeline.md`
- Modify: `.claude/rules/ui-patterns.md`
- Modify: `.claude/rules/testing.md`

- [ ] **Step 1: Update audio-pipeline.md**

Add section on backup model lifecycle:
- Pre-loads on meeting start (background thread, CPU)
- Stays in memory until app closes
- backup_device always CPU unless explicitly overridden
- Backup model tiers by VRAM (tiny/base/small table)

- [ ] **Step 2: Update ui-patterns.md**

Add:
- Three-ring icon geometry and color state table
- Yellow double-flash convention (150ms on/off/on)
- Inner dot = overlay dictation only

- [ ] **Step 3: Update testing.md**

Add backup dictation test checklist:
- Start meeting, press Ctrl+Shift+Space, verify dictation works on CPU
- Verify meeting recording continues uninterrupted
- Verify yellow flash on rapid hotkey presses during model load
- Verify device label shows correct device in Settings
- Verify icon shows three rings during meeting + dictation

- [ ] **Step 4: Commit**

```bash
git add .claude/rules/
git commit -m "docs: update rules for backup dictation, icons, yellow flash"
```

---

### Task 7: Create PR and verify

- [ ] **Step 1: Push branch and create PR**

```bash
git push -u origin fix/backup-vram-debounce-icon
gh pr create --base dev --title "fix: backup dictation VRAM, debounce, three-ring icon" --body "..."
```

- [ ] **Step 2: Wait for Copilot review**

Check with `gh pr view <number> --json reviews`

- [ ] **Step 3: Address any Copilot suggestions**

Fix and push. Repeat until clean review.

- [ ] **Step 4: Verify auto-merge or merge manually**

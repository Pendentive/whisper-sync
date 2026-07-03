# Backup Dictation, Device Management, and Icon Redesign

> Status: SHIPPED (2026-03; icons.py ICON_REGISTRY). Committed 2026-07-03 (previously an untracked draft).

> **Date**: 2026-03-24
> **Status**: Approved
> **Scope**: Fix VRAM doubling, add debounce, redesign tray icon, fix device label bug

## 1. Backup Model Defaults

Backup model always runs on CPU. Auto-detection based on GPU VRAM sets the default backup model during install:

| GPU VRAM | Default backup_model | backup_device |
|----------|---------------------|---------------|
| No GPU | base | cpu |
| < 8 GB | tiny | cpu |
| 8-12 GB | small | cpu |
| 13-24 GB | small | cpu |
| 24+ GB | small | cpu |

User can override both settings manually. If user explicitly sets backup_device to gpu, respect it but log a warning when main model is also on GPU.

**Config change**: `config.defaults.json` changes `backup_device` from `"auto"` to `"cpu"`. The `_resolve_device()` auto logic is removed - backup always returns CPU unless explicitly overridden. The installer writes `backup_model` to user config based on detected VRAM tier. `config.defaults.json` ships `"base"` as safe universal default.

## 2. Backup Model Lifecycle

### Pre-load on meeting start (Option C)
1. Meeting recording starts
2. Background thread loads backup model (small on CPU, ~1s)
3. Model stays in memory until app closes (no unload timer - 500 MB RAM is negligible, SSD reads are free, 1s reload not worth timer complexity)

**Implementation**: `_start_meeting()` calls `self._backup.preload()` (new method). Preload spawns a background thread, sets `_backup_loading=True`, loads model, sets `_backup_loading=False` when done.

### Hotkey behavior during meeting
- Model loaded: transcribe immediately (~2s for 20s clip on CPU)
- Model still loading: yellow double-flash icon (150ms on, 150ms off, 150ms on). Ignore additional presses until ready.
- always_available_dictation disabled: log info, ignore press

### Debounce
- `_backup_loading` flag: True during model load, checked before accepting hotkey
- No timer-based debounce needed, flag is sufficient

## 3. Primary Model Merging

### Rule
Dictation and meeting share one primary model instance. If dictation_model and model (meeting) are the same, one loaded instance serves both. If they differ, meeting model takes priority during active meetings.

The backup model is always separate. Never shares with primary.

### Device switch lifecycle
1. User changes device in Settings (e.g., GPU to CPU)
2. Current primary model unloads (torch.cuda.empty_cache if leaving GPU)
3. New model loads on new device
4. Same model name = no re-download, just re-initialize on new device
5. During idle: switch immediately
6. During meeting recording: queue switch, apply after meeting ends

### Bug fix: device label
Auto label in Settings shows GPU name even when CPU is selected. Fix: show the active resolved device, not the detected GPU. CPU selected = show "CPU". Auto selected = show resolved device name.

## 4. Yellow Double-Flash Convention

Universal "loading/queuing" signal across the app:
- Two quick flashes: 150ms yellow on, 150ms off, 150ms yellow on
- Full icon turns yellow (all rings)
- Used for: backup model loading, device switching in progress, any async user-triggered operation
- After flash completes, icon returns to current state

## 5. Three Concentric Ring Icon

### Geometry (64x64 canvas)
- Outer ring: 3px wide, starts at 2px margin from edge (radius 30 to 27)
- Gap: 2px transparent
- Middle circle: filled, remaining space (radius ~22, the standard inner circle from current dual-ring)
- Inner dot: small filled circle, ~4px radius, centered (overlay dictation indicator only)

Note: The current implementation's small blue dot already works visually at tray size. The main change is making the outer ring thicker (3px) so it's visible. The middle circle stays as the current "inner circle." This is an adjustment, not a full redesign.

### Color states

| State | Outer | Middle | Inner |
|-------|-------|--------|-------|
| Idle | gray (#808080) | gray (#808080) | none |
| Meeting recording | dark red (#CC3333) | light red (#FF4444) | none |
| Meeting + dictation overlay | dark red (#CC3333) | light red (#FF4444) | blue (#4488FF) |
| Dictation only | gray (#CCCCCC) | blue (#4488FF) | none |
| Transcribing meeting | dark amber (#CC8800) | light amber (#FFAA00) | none |
| Transcribing + dictation overlay | dark amber (#CC8800) | light amber (#FFAA00) | blue (#4488FF) |
| Done | green (#44CC44) | green (#66FF66) | none |
| Yellow flash (loading) | yellow (#FFCC00) | yellow (#FFCC00) | none |
| Speaker loopback fail | yellow (#FFAA00) | light red (#FF4444) | none |
| Error | magenta (#CC44CC) | magenta (#FF66FF) | none |

### Backward compatibility
States without overlay dictation (idle, done, error, etc.) use outer + middle rings only. No inner dot. Visual is similar to current dual-ring.

## 6. Benchmark Data (for defaults validation)

Tested on RTX 3090 desktop (7800X3D CPU), 20-second speech clips, CPU int8:

| Model | Avg transcribe time | Match vs large-v3 |
|-------|--------------------|--------------------|
| tiny | 0.37s | 88.9% |
| base | 0.76s | 97.1% |
| small | 1.96s | 98.4% |

Small was chosen as default for 8+ GB systems because the 98.4% accuracy justifies the ~2s delay during meetings. Tiny is for constrained systems where speed matters more than accuracy.

## 7. Documentation updates

The following must be updated in the same PR:
- CLAUDE.md: backup model architecture
- .claude/rules/audio-pipeline.md: backup lifecycle, device switching
- .claude/rules/ui-patterns.md: three-ring icon states, yellow flash convention
- .claude/rules/testing.md: backup dictation test checklist
- config.defaults.json: backup_model default
- README.md: always-available dictation section

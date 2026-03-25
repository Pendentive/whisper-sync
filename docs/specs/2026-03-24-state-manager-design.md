# WhisperSync Notification & State Management System

> **Date**: 2026-03-24
> **Status**: Design
> **Repo**: `https://github.com/Pendentive/whisper-sync`
> **Approach**: A (Python-Native Refactor) with B/C evolution paths documented

## Context

WhisperSync's UI state management is scattered across `__main__.py` (2781 lines, 33 `_update_icon()` call sites). Icons are hand-drawn via PIL with brittle pixel math. The `windows-toasts` library is underutilized (only title+body+buttons; progress bars, in-place updates, grouped notifications unused). There is no central state machine - `self.mode`, `self._meeting_transcribing`, and `self._dictation_overlay` are modified independently across the file.

**Goals**:
1. Centralize all state into an observable `StateManager` with typed semantic events
2. Make icon management declarative (registry-based) with progress ring support
3. Unlock configurable Windows toast notifications (off by default, completion-only default)
4. Design for forward-compatibility with a future web API (Approach C) and web shell (Approach B)
5. Cross-platform: tray icon works on Mac via pystray (Mac is second-class; toasts are Windows-only bonus)

**Non-goals**:
- No new dependencies (stays with pystray + Pillow + windows-toasts)
- No Electron/Tauri work in this phase
- No API server in this phase
- No Mac-specific toast support

---

## 1. StateManager (Core Observable)

**New file**: `whisper_sync/state_manager.py` (~200 lines)

### AppState dataclass

```python
@dataclass
class AppState:
    mode: str | None = None           # "meeting", "dictation", "saving", "transcribing", "summarizing", "done", "error", None
    meeting_transcribing: bool = False # meeting pipeline running in background
    dictation_overlay: bool = False    # dictation active during meeting
    speaker_ok: bool = True           # loopback health
    progress: float | None = None     # 0.0-1.0 for progress ring, None = no progress
```

### StateEvent dataclass

```python
@dataclass
class StateEvent:
    type: str              # semantic event name (see Event Types below)
    timestamp: float       # time.time()
    old_state: AppState    # snapshot before change
    new_state: AppState    # snapshot after change
    data: dict = field(default_factory=dict)  # event-specific payload
```

### Event Types (string constants)

| Constant | When emitted | State changes | data payload |
|----------|-------------|---------------|-------------|
| `MEETING_STARTED` | Meeting recording begins | mode="meeting" | {} |
| `MEETING_STOPPED` | User stops recording | mode="saving" | {} |
| `MEETING_COMPLETED` | Full pipeline done | mode="done", progress=None | {words, speakers, duration, folder} |
| `DICTATION_STARTED` | Dictation recording begins | mode="dictation" | {} |
| `DICTATION_COMPLETED` | Text pasted | mode="done", progress=None | {text, words, duration} |
| `TRANSCRIPTION_STARTED` | WhisperX begins | mode="transcribing", progress=0.0 | {} |
| `TRANSCRIPTION_PROGRESS` | Progress update | progress=N | {progress: 0.0-1.0, stage: str} |
| `TRANSCRIPTION_COMPLETED` | WhisperX done | progress=1.0 | {} |
| `SUMMARIZATION_STARTED` | Minutes generation begins | _(future - not yet in codebase)_ | {} |
| `SUMMARIZATION_COMPLETED` | Minutes done | _(future)_ | {file_path} |
| `DICTATION_DISCARDED` | User discards dictation (left-click) | mode=None OR dictation_overlay=False | {} |
| `ERROR` | Any error | mode="error" | {message, recoverable: bool} |
| `MODEL_LOADING` | Model load (not download) starting | | {model_name, device} |
| `MODEL_DOWNLOADING` | Model download in progress | | {model_name, size_gb} |
| `MODEL_READY` | Model loaded/downloaded | | {model_name, device} |
| `PR_STATUS_CHANGED` | GitHub PR update | | {number, review_state, title, url} |
| `SPEAKER_HEALTH_CHANGED` | Loopback status | speaker_ok=bool | {} |
| `QUEUED` | Dictation queued behind meeting stage | | {} |
| `IDLE` | Return to idle | mode=None, progress=None | {} |

> **Note on SUMMARIZATION events**: `self.mode` is never set to `"summarizing"` in the current codebase. These events are defined for forward-compatibility when minutes generation gets its own pipeline stage. Until then, summarization happens within the `_process()` pipeline and is covered by `TRANSCRIPTION_PROGRESS` with `stage="summarizing"`.

### StateManager class

```python
class StateManager:
    def __init__(self, tray: pystray.Icon, config: dict):
        self._state = AppState()
        self._lock = threading.Lock()  # C3 fix: thread safety - called from 6+ threads
        self._event_log: deque[StateEvent] = deque(maxlen=100)
        self._typed_listeners: dict[str, list[Callable]] = defaultdict(list)
        self._global_listeners: list[Callable] = []
        self._tray = tray
        self._config = config
        self._animator = IconAnimator(tray)

        # Register built-in listeners
        self.on_any(IconListener(tray, config))
        self.on_any(ToastListener(config))

    def emit(self, event_type: str, *, data: dict | None = None, **state_changes):
        """Emit a typed event, update state, notify all listeners.

        Thread-safe: acquires lock before state mutation. Listeners are called
        inside the lock to ensure they see consistent state. Listeners MUST NOT
        call emit() recursively (would deadlock). If a listener needs to trigger
        a state change, it should schedule it on a background thread.
        """
        with self._lock:
            old = replace(self._state)  # snapshot
            for k, v in state_changes.items():
                if hasattr(self._state, k):
                    setattr(self._state, k, v)
            event = StateEvent(
                type=event_type,
                timestamp=time.time(),
                old_state=old,
                new_state=replace(self._state),
                data=data or {},
            )
            self._event_log.append(event)
        # Notify outside lock to avoid deadlock with listeners that read state
        for cb in self._typed_listeners.get(event_type, []):
            try:
                cb(event)
            except Exception:
                logging.getLogger("whisper_sync.state").exception("Listener error")
        for cb in self._global_listeners:
            try:
                cb(event)
            except Exception:
                logging.getLogger("whisper_sync.state").exception("Listener error")

    def on(self, event_type: str, callback: Callable[[StateEvent], None]):
        """Subscribe to a specific event type."""
        self._typed_listeners[event_type].append(callback)

    def on_any(self, callback: Callable[[StateEvent], None]):
        """Subscribe to all events."""
        self._global_listeners.append(callback)

    @property
    def current(self) -> AppState:
        with self._lock:
            return replace(self._state)

    @property
    def history(self) -> list[StateEvent]:
        with self._lock:
            return list(self._event_log)

    @property
    def animator(self) -> IconAnimator:
        return self._animator
```

### Usage migration (before/after)

```python
# BEFORE (scattered across __main__.py):
self.mode = "transcribing"
self._meeting_transcribing = True
self._update_icon()

# AFTER (one-liner):
self.state.emit(TRANSCRIPTION_STARTED, mode="transcribing", meeting_transcribing=True, progress=0.0)

# Progress updates from worker:
self.state.emit(TRANSCRIPTION_PROGRESS, progress=0.45, data={"stage": "alignment"})

# Completion:
self.state.emit(MEETING_COMPLETED, mode="done", progress=None,
                data={"words": 1200, "speakers": 3, "folder": meeting_dir})
```

---

## 2. Icon System (Declarative Registry + Progress Ring)

**Modified file**: `whisper_sync/icons.py`

### IconSpec dataclass

```python
@dataclass(frozen=True)
class IconSpec:
    outer: str              # hex color for outer ring
    middle: str             # hex color for middle circle
    inner: str | None = None  # optional inner dot color (dictation overlay)
    tooltip: str = ""
```

### ICON_REGISTRY

```python
ICON_REGISTRY: dict[str, IconSpec] = {
    # Idle
    "idle":                           IconSpec("#808080", "#808080", tooltip="Idle"),

    # Meeting states
    "recording.meeting":              IconSpec("#CC3333", "#FF4444", tooltip="Recording meeting..."),
    "recording.meeting.speaker_fail": IconSpec("#FFAA00", "#FF4444", tooltip="Recording (speaker issue)"),

    # Dictation states
    "dictation":                      IconSpec("#CCCCCC", "#4488FF", tooltip="Dictating..."),
    "dictation.overlay.meeting":              IconSpec("#CC3333", "#FF4444", "#4488FF", tooltip="Dictating (meeting)..."),
    "dictation.overlay.meeting.speaker_fail": IconSpec("#FFAA00", "#FF4444", "#4488FF", tooltip="Dictating (meeting, speaker issue)..."),
    "dictation.overlay.transcribing":         IconSpec("#CC8800", "#FFAA00", "#4488FF", tooltip="Dictating (transcribing)..."),

    # Pipeline states
    "saving":                         IconSpec("#CC8800", "#FFAA00", tooltip="Saving audio..."),
    "transcribing":                   IconSpec("#CC8800", "#FFAA00", tooltip="Transcribing..."),
    "summarizing":                    IconSpec("#9944CC", "#CC66FF", tooltip="Summarizing..."),
    "queued":                         IconSpec("#CC6600", "#FF8800", tooltip="Queued..."),

    # Terminal states
    "done":                           IconSpec("#44CC44", "#66FF66", tooltip="Done!"),
    "error":                          IconSpec("#CC44CC", "#FF66FF", tooltip="Error"),

    # Animation frames
    "flash":                          IconSpec("#FFCC00", "#FFCC00", tooltip="Loading..."),
}
```

### build_icon function

```python
def build_icon(spec: IconSpec, progress: float | None = None, size: int = 64) -> Image.Image:
    """Build icon from spec. If progress is 0.0-1.0, outer ring draws as clockwise arc."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    margin = 2
    ring_width = 3
    gap = 2
    outer_r = size - margin

    if progress is not None and 0.0 < progress < 1.0:
        # Partial outer ring arc (clockwise from 12 o'clock)
        start_angle = -90  # 12 o'clock in PIL's coordinate system
        sweep = 360 * progress
        draw.arc(
            [margin, margin, outer_r - 1, outer_r - 1],
            start_angle, start_angle + sweep,
            fill=spec.outer, width=ring_width
        )
    elif progress is None or progress >= 1.0:
        # Full outer ring (existing logic)
        draw.ellipse([margin, margin, outer_r - 1, outer_r - 1], fill=spec.outer)
        inner_of_ring = margin + ring_width
        draw.ellipse(
            [inner_of_ring, inner_of_ring, outer_r - 1 - ring_width, outer_r - 1 - ring_width],
            fill=(0, 0, 0, 0),
        )
    # else: progress == 0.0, no outer ring drawn

    # Middle filled circle (after gap)
    mid_start = margin + ring_width + gap
    mid_end = size - margin - ring_width - gap
    if mid_end > mid_start:
        draw.ellipse([mid_start, mid_start, mid_end - 1, mid_end - 1], fill=spec.middle)

    # Inner dot (overlay dictation indicator)
    if spec.inner is not None:
        dot_radius = 4
        cx, cy = size // 2, size // 2
        draw.ellipse(
            [cx - dot_radius, cy - dot_radius, cx + dot_radius - 1, cy + dot_radius - 1],
            fill=spec.inner,
        )

    return img
```

### resolve_icon_key function

Maps the composite AppState to a registry key:

```python
def resolve_icon_key(state: AppState) -> str:
    """Determine the icon registry key from current app state."""
    if state.dictation_overlay:
        if state.mode == "meeting":
            # C1 fix: speaker_fail variant for overlay dictation during meeting
            return "dictation.overlay.meeting.speaker_fail" if not state.speaker_ok else "dictation.overlay.meeting"
        elif state.meeting_transcribing:
            return "dictation.overlay.transcribing"
        else:
            return "dictation"

    if state.mode == "meeting":
        return "recording.meeting" if state.speaker_ok else "recording.meeting.speaker_fail"
    elif state.mode:
        return state.mode  # "dictation", "saving", "transcribing", "done", "error"
    elif state.meeting_transcribing:
        return "transcribing"
    else:
        return "idle"
```

### IconListener (built-in state subscriber)

```python
class IconListener:
    """Subscribes to state events and updates the tray icon."""

    def __init__(self, tray, config):
        self._tray = tray
        self._config = config

    def __call__(self, event: StateEvent):
        key = resolve_icon_key(event.new_state)
        spec = ICON_REGISTRY[key]
        icon = build_icon(spec, progress=event.new_state.progress)
        self._tray.icon = icon
        self._tray.title = f"WhisperSync: {spec.tooltip}"
```

### IconAnimator

```python
class IconAnimator:
    """Handles frame-by-frame icon animations in background threads."""

    def __init__(self, tray):
        self._tray = tray
        self._cancel = threading.Event()

    def flash(self, count=2, interval_ms=150):
        """Yellow double-flash (loading/queuing signal)."""
        self._cancel.clear()
        def _run():
            original = self._tray.icon
            flash_spec = ICON_REGISTRY["flash"]
            flash_img = build_icon(flash_spec)
            for _ in range(count):
                if self._cancel.is_set(): break
                self._tray.icon = flash_img
                time.sleep(interval_ms / 1000)
                self._tray.icon = original
                time.sleep(interval_ms / 1000)
        threading.Thread(target=_run, daemon=True).start()

    def flash_between(self, key_a: str, key_b: str, count=2, interval_ms=150):
        """I4 fix: Alternating flash between two icon states (e.g., queued/transcribing)."""
        self._cancel.clear()
        def _run():
            img_a = build_icon(ICON_REGISTRY[key_a])
            img_b = build_icon(ICON_REGISTRY[key_b])
            for _ in range(count):
                if self._cancel.is_set(): break
                self._tray.icon = img_a
                self._tray.title = f"WhisperSync: {ICON_REGISTRY[key_a].tooltip}"
                time.sleep(interval_ms / 1000)
                self._tray.icon = img_b
                self._tray.title = f"WhisperSync: {ICON_REGISTRY[key_b].tooltip}"
                time.sleep(interval_ms / 1000)
        threading.Thread(target=_run, daemon=True).start()

    def cancel(self):
        self._cancel.set()
```

### Backward compatibility

Existing icon functions (`idle_icon()`, `recording_icon()`, etc.) become thin wrappers:

```python
def idle_icon() -> Image.Image:
    return build_icon(ICON_REGISTRY["idle"])

def recording_icon(speaker_ok: bool = True) -> Image.Image:
    key = "recording.meeting" if speaker_ok else "recording.meeting.speaker_fail"
    return build_icon(ICON_REGISTRY[key])
# ... etc
```

These are deprecated but kept for any external consumers during migration.

---

## 3. Toast Notification System (Configurable)

**Modified file**: `whisper_sync/notifications.py`

### ToastListener (built-in state subscriber)

```python
class ToastListener:
    """Subscribes to state events and shows configurable Windows toasts."""

    DEFAULT_TOAST_EVENTS = {"meeting_completed", "error", "pr_status_changed"}

    def __init__(self, config):
        self._config = config

    def __call__(self, event: StateEvent):
        enabled_events = set(self._config.get("toast_events", self.DEFAULT_TOAST_EVENTS))
        if event.type not in enabled_events:
            return

        toast_config = TOAST_REGISTRY.get(event.type)
        if not toast_config:
            return

        title = toast_config["title"].format(**event.data)
        body = toast_config["body"].format(**event.data) if toast_config.get("body") else ""
        notify(title, body, buttons=toast_config.get("buttons"))
```

### TOAST_REGISTRY

```python
TOAST_REGISTRY = {
    "meeting_completed": {
        "title": "Meeting transcribed",
        "body": "{words} words, {speakers} speakers",
        "buttons": [{"label": "Open Folder", "action": "open_folder"}],
    },
    "dictation_completed": {
        "title": "Dictation complete",
        "body": "{words} words",
    },
    "error": {
        "title": "WhisperSync Error",
        "body": "{message}",
    },
    "pr_status_changed": {
        # S3 fix: event.data must flatten PR fields, not nest under "pr" object
        # Emitter flattens: data={"number": pr.number, "review_state": pr.review_state, "title": pr.title}
        "title": "PR #{number}: {review_state}",
        "body": "{title}",
    },
    "transcription_progress": {
        "tag": "transcription",  # in-place update
        "progress": True,
    },
}
```

### Config schema additions

```json
{
    "toast_events": ["meeting_completed", "error", "pr_status_changed"]
}
```

**I3 fix**: Add `"toast_events"` to `_VALID_KEYS` in `config.py:22-29`. Stored as a JSON list (not set). `DEFAULT_TOAST_EVENTS` in code is a list, converted to set at runtime for fast lookup.

The simplified `toast_on_dictation` and `toast_on_progress` booleans are replaced by the single `toast_events` list - users add/remove event type strings. This is more flexible and avoids multiplying boolean config keys.

Users toggle toast event types via Settings > Notifications submenu. Default: only completion and errors.

### notify_update (in-place toast updates)

The existing `notify_progress()` function already supports progress bars. New addition:

```python
def notify_update(tag: str, title: str, body: str, *, progress=None):
    """Update an existing toast in-place by tag. Creates if not exists."""
    if not _available:
        return
    toast = Toast([title, body])
    toast.tag = tag
    toast.group = "whispersync"
    if progress is not None and _has_progress_bar:
        toast.progress_bar = ToastProgressBar("", "", progress=progress)
    _toaster.show_toast(toast)  # replaces existing toast with same tag+group
```

---

## 4. Migration Strategy

### Phase 1: Create StateManager + IconSpec (non-breaking)
1. Create `state_manager.py` with StateManager, AppState, StateEvent, event constants
2. Refactor `icons.py`: add IconSpec, ICON_REGISTRY, build_icon, resolve_icon_key
3. Keep all existing icon functions as backward-compat wrappers
4. Add IconListener, ToastListener, IconAnimator

### Phase 2: Wire StateManager into __main__.py (shim)
1. Instantiate StateManager in `WhisperSync.__init__()` after tray creation
2. Keep existing `self.mode` assignments. Rewrite `_update_icon()` to delegate rendering:
   ```python
   def _update_icon(self):
       # Shim: read old-style state, render via new icon system
       state = AppState(
           mode=self.mode,
           meeting_transcribing=self._meeting_transcribing,
           dictation_overlay=self._dictation_overlay,
           speaker_ok=getattr(self.recorder, "speaker_loopback_active", True),
       )
       key = resolve_icon_key(state)
       spec = ICON_REGISTRY[key]
       icon = build_icon(spec, progress=getattr(self, '_progress', None))
       self.tray.icon = icon
       self.tray.title = f"WhisperSync: {spec.tooltip}"
   ```
3. This validates the icon registry against all existing states without changing any call sites

### Phase 3: Migrate call sites (incremental)
Replace `self.mode = X; self._update_icon()` with `self.state.emit(EVENT, ...)` one flow at a time:
1. Dictation flow (start, stop, transcribe, paste, done)
2. Meeting flow (start, stop, save, transcribe, summarize, done)
3. Error handling
4. GitHub PR polling
5. Model loading / yellow flash
6. Queued dictation during meeting

### Phase 4: Cleanup
1. Remove `self.mode`, `self._meeting_transcribing`, `self._dictation_overlay` from WhisperSync
2. Remove `_update_icon()` shim
3. Remove deprecated icon functions from icons.py
4. Update docs: `.claude/rules/ui-patterns.md`, `docs/ui-spec.md`

### Phase 5: Toast configuration UI
1. Add "Notifications" submenu to Settings menu
2. Checkboxes for each toast event type
3. Persist to config.json

---

## 5. Evolution Paths (B and C)

### Path to Approach C: Web Status API

When the PM dashboard need materializes:

1. `pip install fastapi uvicorn`
2. Create `whisper_sync/api_server.py` (~200 lines)
3. Register as a global listener: `self.state.on_any(api_server.broadcast)`
4. Expose endpoints:
   - `GET /state` - current AppState as JSON
   - `GET /history` - event log (ringbuffer)
   - `WebSocket /ws` - real-time state stream
   - `POST /action` - trigger actions (toggle_meeting, etc.)
5. Start uvicorn in a daemon thread during WhisperSync init

**Effort**: ~1 session. StateManager's `on_any` hook makes this trivial.

### Path to Approach B: Tauri/Electron Web Shell

When full web UI is needed:

1. Create `tray-shell/` (Tauri or Electron project)
2. The web shell connects to the API from Approach C
3. Replaces pystray for icon management (CSS animations, SVG icons)
4. Python backend stays untouched - all communication via the API
5. Notifications become HTML/CSS popups instead of Windows toasts

**Effort**: 3-5 sessions. Requires Approach C as prerequisite.

### Path to Claude/MCP Integration

The StateManager event system is directly hookable:

```python
# Future: register a Claude MCP tool that listens to events
self.state.on(MEETING_COMPLETED, lambda e: claude_agent.generate_minutes(e.data["folder"]))
self.state.on(ERROR, lambda e: claude_agent.diagnose(e.data["message"]))
```

---

## 6. Files to Create/Modify

| File | Action | Lines (est.) |
|------|--------|-------------|
| `whisper_sync/state_manager.py` | CREATE | ~200 |
| `whisper_sync/icons.py` | REFACTOR | ~180 (was 145) |
| `whisper_sync/notifications.py` | EXTEND | ~200 (was 142) |
| `whisper_sync/__main__.py` | REFACTOR | ~2700 (net reduction ~80 lines) |
| `.claude/rules/ui-patterns.md` | UPDATE | add StateManager docs |
| `docs/ui-spec.md` | UPDATE | new state machine section |
| `docs/specs/2026-03-24-state-manager-design.md` | CREATE | this spec |

**No new dependencies.** pystray, Pillow, windows-toasts remain.

---

## 7. Verification

### Manual testing checklist
1. Start app - idle icon (gray circles)
2. Start dictation - icon changes to blue
3. Stop dictation - transcribing (amber), progress ring fills clockwise, done (green), idle
4. Start meeting - red icon
5. Start dictation during meeting - inner blue dot appears
6. Stop meeting - saving (amber) -> transcribing with progress ring -> summarizing (purple) -> done (green)
7. Trigger error - magenta icon, toast appears (if enabled)
8. Yellow flash - press dictation hotkey while model loading
9. Settings > Notifications - toggle toast events on/off
10. Check `state.history` - verify event log captures all transitions

### Cross-platform (Mac)
1. pystray icon rendering works (static icons only, no progress ring animation concern since PIL renders the frame)
2. Toasts silently degrade (log-only, no crash)

### Regression
1. All existing flows work identically (dictation, meeting, GitHub PRs)
2. Tooltip text matches ui-spec.md
3. Config persistence unchanged

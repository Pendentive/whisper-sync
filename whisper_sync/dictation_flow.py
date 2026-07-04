"""Dictation workflow - normal, feature-suggest, and overlay dictation.

Extracted from __main__.py (hardening round item 6, architecture spec
A2). DictationFlow owns the dictation lifecycle end to end: hotkey
toggles, start/stop, the auto-stop memory cap, overlay dictation during
meetings, left-click discard, crash recovery, feature-suggestion
formatting, and the recent-dictation history.

The flow keeps a back-reference to the application object for shared
services (cfg, state manager, recorder, worker, backup transcriber,
GPU guard, session stats, tray helpers) - the same composition pattern
as meeting_job.MeetingJob. The app stays the wiring surface; behavior
lives here.

Feature-suggest routing state: the old ``_feature_suggest_active`` flag
on the app (toggled at 15+ call sites) is folded into
``AppState.feature_suggest``. Feature intent is passed DOWN as a
parameter and enters state atomically with DICTATION_STARTED; it clears
with the completion/discard/idle emit. Start attempts that fail before
DICTATION_STARTED never touch state, which eliminates the old
set-then-unwind pattern entirely.
"""

import threading
from datetime import datetime
from pathlib import Path

from . import dictation_log
from . import feature_log
from . import weekly_stats
from .executors import DICTATION, IO, submit_or_spawn
from .logger import logger, log_dictation_result
from .notifications import notify
from .paths import get_dictation_log_dir
from .state_manager import (
    DICTATION_STARTED, DICTATION_COMPLETED, DICTATION_DISCARDED,
    TRANSCRIPTION_STARTED, ERROR, IDLE,
)
from .worker_manager import WorkerCrashedError

# In-memory history shown in the Recent Dictations menu.
HISTORY_LIMIT = 10


def safe_unlink(path: Path | None, retries: int = 2, delay: float = 0.5):
    """Delete a file, retrying on PermissionError (Windows file locking)."""
    import time
    for attempt in range(retries + 1):
        try:
            if path and path.exists():
                path.unlink(missing_ok=True)
            return
        except PermissionError:
            if attempt < retries:
                time.sleep(delay)
            else:
                logger.debug(f"Could not delete {path} - will clean up on next restart")


class DictationFlow:
    """Owns dictation behavior; reads shared services through ``app``.

    Thread model: toggles and discard run under ``app._lock`` (shared
    with the meeting flow so mode checks and starts are atomic across
    workflows). Transcription work is offloaded to the DICTATION
    executor lane. History is guarded by its own lock because dictation
    and overlay worker threads append while menu builds read.
    """

    def __init__(self, app):
        self.app = app
        self._wav_path: Path | None = None
        self._overlay_recorder = None  # separate AudioRecorder during meetings
        self._overlay_wav_path: Path | None = None
        self._cap_handle = None
        self._history = dictation_log.load_recent(HISTORY_LIMIT)
        self._history_lock = threading.Lock()

    # -- Public API (hotkeys, clicks, menu, startup recovery) ---------------

    @property
    def overlay_active(self) -> bool:
        """True while an overlay dictation recorder exists."""
        return self._overlay_recorder is not None

    def toggle(self):
        with self.app._lock:
            current = self.app.state.current if self.app.state else None
            mode = current.mode if current else None
            overlay = current.dictation_overlay if current else False
            meeting_tx = current.meeting_transcribing if current else False

            # Handle overlay dictation during meetings
            if overlay:
                self._stop_overlay()
                return

            if current and current.sleeping:
                # Model is unloaded (auto_sleep): wake it and KEEP GOING.
                # Dictation streams to disk, so recording starts now and
                # transcription waits for the model at stop time.
                self.app.auto_sleep.wake(reason="dictation_hotkey")

            if mode == "dictation":
                self._stop()
            elif mode == "meeting" or (mode is None and meeting_tx):
                # Dictation during meeting recording or meeting transcription
                self._toggle_during_meeting(feature=False)
            elif mode == "saving":
                logger.debug("Dictation ignored - meeting is saving")
            elif self.app._can_record():
                self._start(feature=False)

    def toggle_feature_suggest(self):
        """Toggle feature suggestion recording (dictation routed to the feature log)."""
        with self.app._lock:
            current = self.app.state.current if self.app.state else None
            mode = current.mode if current else None
            overlay = current.dictation_overlay if current else False
            meeting_tx = current.meeting_transcribing if current else False
            feature_active = current.feature_suggest if current else False

            # If already recording a feature suggestion, stop it
            if overlay and feature_active:
                self._stop_overlay()
                return
            if overlay:
                # A NORMAL overlay dictation is recording - ignore, exactly
                # like the feature hotkey during a normal dictation below.
                # Starting a second overlay here would overwrite
                # _overlay_recorder and double-open the mic (review catch on
                # the extraction; the bug predates it).
                logger.debug("Feature suggest ignored - overlay dictation in progress")
                return
            if mode == "dictation" and feature_active:
                self._stop()
                return
            if mode == "dictation":
                # Already recording a normal dictation - ignore
                logger.debug("Feature suggest ignored - dictation in progress")
                return
            if current and current.sleeping:
                self.app.auto_sleep.wake(reason="feature_hotkey")

            if mode == "meeting" or (mode is None and meeting_tx):
                self._toggle_during_meeting(feature=True)
            elif mode == "saving":
                logger.debug("Feature suggest ignored - meeting is saving")
            elif self.app._can_record():
                self._start(feature=True)

    def discard(self):
        """Discard current dictation - stop recording, throw away audio, return to idle."""
        with self.app._lock:
            _overlay = self.app.state.current.dictation_overlay if self.app.state else False
            if _overlay and self._overlay_recorder:
                self._overlay_recorder.stop()
                self._overlay_recorder.stop_streaming()
                self._overlay_recorder = None
                discarded_wav = self._overlay_wav_path
                self._overlay_wav_path = None
                if discarded_wav:
                    safe_unlink(discarded_wav)
                logger.info("Overlay dictation discarded (left-click)", extra={"secondary": True})
                self.app.state.emit(DICTATION_DISCARDED, dictation_overlay=False,
                                    feature_suggest=False)
                return

            if (self.app.state.current.mode if self.app.state else None) != "dictation":
                return
            self.app.recorder.stop()  # stop recording, discard the audio
            self.app.recorder.stop_streaming()
            if self._wav_path and self._wav_path.exists():
                self._wav_path.unlink(missing_ok=True)
            logger.info("Dictation discarded (left-click)")
            self.app.state.emit(DICTATION_DISCARDED, mode=None, feature_suggest=False)

    def recent_history(self) -> list[dict]:
        """Snapshot of the recent-dictation history (newest last)."""
        with self._history_lock:
            return list(self._history)

    def clear_history(self):
        """Clear the in-memory dictation history (menu only, logs on disk are preserved)."""
        with self._history_lock:
            self._history.clear()
        self.app._refresh_menu()

    # -- Start/stop ----------------------------------------------------------

    def _toggle_during_meeting(self, feature: bool):
        """Route a mid-meeting dictation request to the overlay path if allowed."""
        label = "Feature suggest" if feature else "Dictation"
        if self.app.cfg.get("always_available_dictation", True):
            if self.app._backup.is_loading:
                logger.debug("Backup model still loading, triggering yellow flash",
                             extra={"secondary": True})
                self.app._yellow_flash()
                return
            self._start_overlay(feature=feature)
        else:
            logger.info(f"{label} unavailable during meeting "
                        "(always_available_dictation disabled)", extra={"secondary": True})
            notify(f"{label} unavailable", "Enable always-available dictation in settings")

    def _start(self, feature: bool):
        # Lazy: capture/backup_worker import numpy; keep this module
        # importable on the dependency-light system python (CI suite).
        from .backup_worker import BackupTranscriber
        from .capture import get_default_devices

        app = self.app
        _meeting_tx = app.state.current.meeting_transcribing if app.state else False
        if not app.worker.is_ready():
            has_backup = _meeting_tx and BackupTranscriber.is_enabled(app.cfg)
            if app.cfg.get("incognito", False) and not has_backup:
                # RAM-only capture has no crash net and nothing to
                # transcribe it promptly - keep the old refuse behavior.
                logger.warning("Worker not ready and whisper mode is on - "
                               "ignoring dictation request")
                app._yellow_flash()
                return
            # Disk-first recording works fine without the model: record
            # now, transcribe when it finishes loading (stop path waits).
            logger.info("Dictation recording while the model loads (disk-first)")
        # Atomic claim: only start if mode is still startable. A worker
        # completion (or a double-fired hotkey on another thread) changing
        # mode between the toggle's check and this start is rejected here
        # instead of producing two concurrent recording sessions. Feature
        # intent enters state WITH the started event.
        if not app.state.try_transition(
            (None, "transcribing", "done", "error"),
            DICTATION_STARTED, mode="dictation", feature_suggest=feature,
        ):
            logger.debug("dictation start rejected: mode changed concurrently")
            return
        mic = app.cfg.get("mic_device")
        if app.cfg.get("use_system_devices", True):
            # Explicitly use the WASAPI default instead of falling through
            # to sd.default.device[0], which on Windows resolves to an MME
            # device and often rejects float32 @ 16 kHz with MME error 32.
            mic = get_default_devices().get("input")
        try:
            app.recorder.start(mic_device=mic)
        except Exception as e:
            logger.error("Failed to start mic for dictation: %s", e, exc_info=True)
            notify("Dictation unavailable", f"Mic could not be opened: {e}")
            app.state.emit(IDLE, mode=None, feature_suggest=False)
            return
        # Stream to disk for crash recovery -- skip when incognito (RAM only)
        if app.cfg.get("incognito", False):
            self._wav_path = None
        else:
            log_dir = get_dictation_log_dir()
            log_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            prefix = "feature_" if feature else ""
            self._wav_path = log_dir / f"{prefix}{ts}.wav"
            try:
                app.recorder.start_streaming(self._wav_path)
            except Exception as e:
                logger.warning("Dictation disk streaming disabled: %s", e)
                self._wav_path = None

        # Memory protection: dictation mic audio accumulates in RAM
        # (~230 MB/hour @16k float32). A forgotten hotkey used to grow
        # unbounded. Auto-stop at the configured cap; the audio captured
        # so far is transcribed normally, nothing is lost.
        self._arm_cap()

    def _arm_cap(self):
        """Schedule the dictation auto-stop cap for this session."""
        cap_min = float(self.app.cfg.get("dictation_max_minutes", 30) or 0)
        if cap_min <= 0:
            return
        from .scheduler import scheduler

        def _cap_hit():
            mode = self.app.state.current.mode if self.app.state else None
            if mode != "dictation":
                return  # session already ended normally

            def _do_stop():
                logger.warning(
                    "Dictation auto-stopped at %.0f min cap "
                    "(dictation_max_minutes; audio so far is transcribed)",
                    cap_min,
                )
                try:
                    notify(
                        "Dictation auto-stopped",
                        f"Hit the {cap_min:.0f} min cap; transcribing what was recorded",
                    )
                except Exception:
                    pass
                # toggle takes the app lock and routes by current mode, so
                # a user stop racing this is benign (mode check repeats
                # under the lock inside toggle).
                self.toggle()

            # Scheduler jobs must stay short (scheduler.py contract):
            # toggle acquires the app lock and stops the recorder, so
            # offload it instead of blocking other timer jobs (menu
            # refresh, idle GC).
            submit_or_spawn(DICTATION, "dictation-cap-stop", _do_stop)

        self._cancel_cap()
        self._cap_handle = scheduler.call_later(
            cap_min * 60.0, _cap_hit, label="dictation-cap"
        )

    def _cancel_cap(self):
        if self._cap_handle is not None:
            self._cap_handle.cancel()
            self._cap_handle = None

    def _stop(self):
        from .backup_worker import BackupTranscriber
        from .paste import paste

        app = self.app
        self._cancel_cap()
        audio = app.recorder.stop()
        app.recorder.stop_streaming()

        if "mic" not in audio:
            app.state.emit(IDLE, mode=None, feature_suggest=False)
            return

        # Capture-and-clear: read feature intent, then clear it in the same
        # transition that leaves dictation mode. All mutators of
        # feature_suggest run under app._lock (which every caller of _stop
        # holds), so the read-then-emit pair is atomic exactly like the old
        # locked flag capture.
        is_feature = app.state.current.feature_suggest
        app.state.emit(TRANSCRIPTION_STARTED, mode="transcribing", feature_suggest=False)

        dictation_model = app._gpu_guard.effective_model(
            app.cfg.get("dictation_model", app.cfg["model"]))
        use_backup = (app.state.current.meeting_transcribing
                      and BackupTranscriber.is_enabled(app.cfg))

        def _process():
            import time as _time

            t0 = _time.perf_counter()
            try:
                if not use_backup and not app.worker.is_ready():
                    # Started while the model was loading (wake from sleep
                    # or app startup). The yellow transcribing state simply
                    # lasts longer; the audio is already safe on disk.
                    logger.info("Waiting for the model to load before "
                                "transcribing dictation...")
                    if not app.worker.wait_ready(timeout=180):
                        raise RuntimeError(
                            "Model did not load in time; dictation audio "
                            f"preserved at: {self._wav_path}")
                text = None
                used_backup = False
                if use_backup:
                    # Meeting is transcribing - use lightweight backup model
                    # instead of queuing on the busy worker
                    try:
                        text = app._backup.transcribe(audio["mic"])
                        used_backup = True
                        t1 = _time.perf_counter()
                        backup_model = app.cfg.get("backup_model", "base")
                        backup_device = app.cfg.get("backup_device", "cpu")
                        logger.info(
                            f"Dictation (backup, {backup_device} {backup_model}): "
                            f"{t1 - t0:.2f}s",
                            extra={"secondary": True},
                        )
                    except Exception as backup_err:
                        # Backup failed - fall back to queuing on the main worker
                        logger.warning(f"Backup transcriber failed: {backup_err}",
                                       extra={"secondary": True})
                        logger.info("Falling back to main worker (queued)",
                                    extra={"secondary": True})
                        app._flash_queued()
                        timeout = 180
                        text = app.worker.transcribe_fast(
                            audio["mic"], model_override=dictation_model, timeout=timeout)
                        t1 = _time.perf_counter()
                        logger.debug(f"transcribe_fast (fallback): {t1 - t0:.2f}s")
                else:
                    # Normal path or backup disabled
                    if app.state.current.meeting_transcribing:
                        app._flash_queued()
                    timeout = 180 if app.state.current.meeting_transcribing else 60
                    text = app.worker.transcribe_fast(
                        audio["mic"], model_override=dictation_model, timeout=timeout)
                    t1 = _time.perf_counter()
                    logger.debug(f"transcribe_fast: {t1 - t0:.2f}s")
                t2 = _time.perf_counter()
                char_count = len(text) if text else 0

                if is_feature:
                    self._save_feature(text, t2 - t0, overlay=False)
                else:
                    # Normal dictation mode: paste + log
                    if text:
                        paste(text, app.cfg["paste_method"],
                              restore=not app.cfg.get("incognito", False))
                    delivery = "pasted" if app.cfg["paste_method"] == "keystrokes" else "clipboard"
                    if app.cfg.get("incognito"):
                        logger.info(f"Dictation: {t2 - t0:.2f}s -- {delivery} ({char_count} chars)")
                    else:
                        log_dictation_result(text or "", t2 - t0, delivery, char_count,
                                             secondary=used_backup)
                    effective_model = (app.cfg.get("backup_model", "base")
                                       if used_backup else dictation_model)
                    logger.debug(f"total (stop -> paste): {t2 - t0:.2f}s, "
                                 f"model={effective_model}{' (backup)' if used_backup else ''}")
                    # Update session stats
                    app._stats.record_dictation(char_count, t2 - t0)
                    weekly_stats.record_dictation(char_count, t2 - t0)
                    if text and not app.cfg.get("incognito", False):
                        dictation_log.append(text, t2 - t0, model=effective_model)
                        self._record_history(text)
                # Success -- remove crash-safety WAV (text is in the log)
                app.recorder.stop_streaming()  # defensive: ensure writer closed
                if self._wav_path:
                    safe_unlink(self._wav_path)
                app.state.emit(DICTATION_COMPLETED, mode="done")
            except WorkerCrashedError:
                app._gpu_guard.note_pressure_trigger("worker_crash_dictation")
                logger.error("Worker crashed during dictation -- respawning...")
                if self._wav_path:
                    logger.info(f"Dictation audio preserved at: {self._wav_path}")
                app.worker.restart()
                app.state.emit(ERROR, mode="error",
                               data={"message": "Worker crashed during dictation",
                                     "recoverable": True})
            except Exception as e:
                logger.error(f"Dictation error: {e}")
                import traceback
                logger.debug(traceback.format_exc())
                app.state.emit(ERROR, mode="error",
                               data={"message": str(e), "recoverable": False})
            finally:
                app._schedule_idle(2)

        submit_or_spawn(DICTATION, "dictation-process", _process)

    # -- Overlay dictation (dictation during meeting recording/transcription) --

    def _start_overlay(self, feature: bool):
        """Start dictation using a separate audio stream while meeting continues.

        Uses an independent AudioRecorder so the meeting recording is never
        interrupted. Transcription uses the backup model (CPU or secondary GPU).
        """
        # Note: always_available_dictation config flag already gates the call
        # to this method in _toggle_during_meeting, so no need to re-check
        # is_enabled() here.
        from .capture import AudioRecorder, get_default_devices

        app = self.app
        meeting_state = ("recording"
                         if (app.state.current.mode if app.state else None) == "meeting"
                         else "transcribing")
        backup_model = app.cfg.get("backup_model", "base")
        backup_device = app.cfg.get("backup_device", "cpu")
        logger.info(f"Dictation requested during meeting {meeting_state} "
                    "(using backup model)", extra={"secondary": True})
        logger.info(f"Backup model: {backup_model} on {backup_device}",
                    extra={"secondary": True})

        # Create a separate recorder for dictation audio (mic only)
        self._overlay_recorder = AudioRecorder(sample_rate=app.cfg["sample_rate"])
        mic = app.cfg.get("mic_device")
        if app.cfg.get("use_system_devices", True):
            mic = get_default_devices().get("input")
        try:
            self._overlay_recorder.start(mic_device=mic)
        except Exception as e:
            logger.error("Failed to start overlay dictation mic: %s", e, exc_info=True)
            notify("Dictation unavailable", f"Mic could not be opened: {e}")
            self._overlay_recorder = None
            return
        # Disk-first like normal dictation (hardware-resilience H3):
        # overlay audio was the last RAM-only capture path, so a crash
        # mid-overlay lost it. Incognito intentionally stays RAM-only.
        self._overlay_wav_path = None
        if not app.cfg.get("incognito", False):
            log_dir = get_dictation_log_dir()
            log_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            prefix = "feature_" if feature else ""
            self._overlay_wav_path = log_dir / f"overlay_{prefix}{ts}.wav"
            try:
                self._overlay_recorder.start_streaming(self._overlay_wav_path)
            except Exception as e:
                logger.warning("Overlay disk streaming disabled: %s", e)
                self._overlay_wav_path = None
        logger.info("Dictation during meeting: recording started", extra={"secondary": True})
        app.state.emit(DICTATION_STARTED, dictation_overlay=True, feature_suggest=feature)

    def _stop_overlay(self):
        """Stop overlay dictation, transcribe with backup model, paste result."""
        app = self.app
        if not self._overlay_recorder:
            app.state.emit(IDLE, dictation_overlay=False, feature_suggest=False)
            return

        audio = self._overlay_recorder.stop()
        # Finalize the streaming WAV (header + handle) so the file can be
        # deleted on success or preserved intact on failure - stop() does
        # not close the mic writer (same discipline as normal dictation).
        self._overlay_recorder.stop_streaming()
        # Capture-and-clear under app._lock, same rationale as _stop.
        is_feature = app.state.current.feature_suggest
        app.state.emit(DICTATION_COMPLETED, dictation_overlay=False, feature_suggest=False)

        if "mic" not in audio:
            logger.debug("Overlay dictation stopped - no audio captured",
                         extra={"secondary": True})
            self._overlay_recorder = None
            return

        overlay_audio = audio["mic"]
        self._overlay_recorder = None
        overlay_wav = self._overlay_wav_path
        self._overlay_wav_path = None

        logger.info("Dictation during meeting: transcribing...", extra={"secondary": True})

        def _process_overlay():
            import time as _time

            transcribed = False
            t0 = _time.perf_counter()
            try:
                text = app._backup.transcribe(overlay_audio)
                transcribed = True
                t1 = _time.perf_counter()
                duration = t1 - t0
                char_count = len(text) if text else 0
                backup_model = app.cfg.get("backup_model", "base")
                backup_device = app.cfg.get("backup_device", "cpu")
                logger.info(
                    f"Dictation during meeting: {duration:.1f}s, {char_count} chars "
                    f"(backup {backup_device} {backup_model})",
                    extra={"secondary": True},
                )
                if is_feature:
                    self._save_feature(text, duration, overlay=True)
                else:
                    self._deliver_overlay(text, duration, model=backup_model)
            except Exception as e:
                logger.error(f"Overlay dictation error: {e}", extra={"secondary": True})
                import traceback
                logger.debug(traceback.format_exc())
                # Fall back to queuing on main worker if backup fails
                try:
                    logger.info("Falling back to main worker for overlay dictation",
                                extra={"secondary": True})
                    dictation_model = app._gpu_guard.effective_model(
                        app.cfg.get("dictation_model", app.cfg["model"]))
                    timeout = 180
                    text = app.worker.transcribe_fast(
                        overlay_audio, model_override=dictation_model, timeout=timeout)
                    transcribed = True
                    duration = _time.perf_counter() - t0
                    char_count = len(text or "")
                    logger.info(f"Overlay dictation fallback: {duration:.2f}s, "
                                f"{char_count} chars", extra={"secondary": True})
                    self._deliver_overlay(text, duration, model=dictation_model)
                except Exception as fallback_err:
                    logger.error(f"Overlay dictation fallback also failed: {fallback_err}",
                                 extra={"secondary": True})

            if overlay_wav:
                if transcribed:
                    safe_unlink(overlay_wav)
                else:
                    logger.info(f"Overlay dictation audio preserved at: {overlay_wav}",
                                extra={"secondary": True})

        submit_or_spawn(DICTATION, "overlay-process", _process_overlay)

    def _deliver_overlay(self, text: str | None, duration: float, *, model: str):
        """Paste + log + stats for an overlay dictation result.

        Shared by the backup path and the main-worker fallback (their
        bookkeeping was duplicated before the extraction).
        """
        from .paste import paste

        app = self.app
        char_count = len(text) if text else 0
        if text:
            paste(text, app.cfg["paste_method"],
                  restore=not app.cfg.get("incognito", False))

        # Update session stats
        app._stats.record_dictation(char_count, duration)
        weekly_stats.record_dictation(char_count, duration)

        incognito = app.cfg.get("incognito", False)
        if text and not incognito:
            dictation_log.append(text, duration, model=model)
            self._record_history(text)

        delivery = "pasted" if app.cfg["paste_method"] == "keystrokes" else "clipboard"
        if incognito:
            logger.info(f"Overlay dictation: {duration:.2f}s - {delivery} "
                        f"({char_count} chars)", extra={"secondary": True})
        else:
            log_dictation_result(text or "", duration, delivery, char_count, secondary=True)

    # -- Feature suggestions -------------------------------------------------

    def _save_feature(self, text: str | None, duration: float, *, overlay: bool):
        """Route a feature-suggest transcription to the feature log."""
        app = self.app
        tag = " (overlay)" if overlay else ""
        secondary = {"extra": {"secondary": True}} if overlay else {}
        if not text:
            logger.info(f"Feature suggestion{tag} discarded (no speech detected)",
                        **secondary)
        elif app.cfg.get("incognito", False):
            logger.info("Feature suggestion skipped: whisper mode enabled", **secondary)
            notify("Feature not saved",
                   "Whisper mode is on; suggestions are not stored on disk")
        else:
            char_count = len(text)
            entry_id = feature_log.append_raw(text, duration)
            logger.info(f"Feature suggestion{tag} saved: {char_count} chars "
                        f"in {duration:.2f}s", **secondary)
            notify("Feature saved", f"Suggestion recorded ({char_count} chars)")
            app._stats.record_feature_suggestion()
            weekly_stats.record_feature_suggestion()
            # Format asynchronously via Claude CLI
            submit_or_spawn(
                IO, "feature-format",
                lambda t=text, e=entry_id: self._format_feature_async(t, e),
                native=True,
            )

    def _format_feature_async(self, raw_text: str, entry_id: str):
        """Format a feature suggestion via Claude CLI (background thread)."""
        import subprocess as _sp

        prompt_file = Path(__file__).parent / "feature_prompt.md"
        if not prompt_file.exists():
            logger.warning(f"Feature prompt template not found: {prompt_file}")
            return

        prompt_text = prompt_file.read_text(encoding="utf-8")
        prompt_text = prompt_text.replace("{TRANSCRIPTION}", raw_text)

        logger.info("Formatting feature suggestion via Claude CLI...")
        try:
            from .executors import native_call
            with native_call("claude-feature-format"):
                result = _sp.run(
                    ["claude", "-p", "--model", "haiku"],
                    input=prompt_text,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
            if result.returncode == 0 and result.stdout.strip():
                feature_log.update_consolidated(entry_id, result.stdout.strip())
                logger.info("Feature suggestion formatted successfully")
                notify("Feature formatted", "Claude formatted your feature suggestion")
            else:
                logger.warning(f"Claude CLI returned code {result.returncode}")
                if result.stderr:
                    logger.debug(f"stderr: {result.stderr[:500]}")
        except FileNotFoundError:
            logger.warning("Claude CLI not found - raw feature saved without formatting")
        except _sp.TimeoutExpired:
            logger.warning("Claude CLI timed out formatting feature (60s limit)")

    # -- Crash recovery ------------------------------------------------------

    def recover_dictation(self, wav_path: str):
        """Transcribe a recovered dictation WAV from a previous crash.

        Puts the text on the clipboard (not auto-paste - wrong window may be
        focused after a restart). The user can Ctrl+V when ready.
        """
        import pyperclip
        app = self.app
        logger.info(f"Recovering crashed dictation from: {wav_path}")
        try:
            audio_np = self._load_wav(wav_path)
            dictation_model = app._gpu_guard.effective_model(
                app.cfg.get("dictation_model", app.cfg["model"]))
            text = app.worker.transcribe_fast(audio_np, model_override=dictation_model)
            if text:
                pyperclip.copy(text)
                dictation_log.append(text, 0, model=dictation_model)
                logger.info(f"Crash-recovered dictation copied to clipboard: {text[:80]}...")
                # #38: Toast with recovered text info and Copy button
                try:
                    _recovered_text = text

                    def _copy_recovered(t=_recovered_text):
                        import pyperclip as _pc
                        _pc.copy(t)
                    notify(
                        "Dictation recovered",
                        f"{len(text)} chars recovered from crash",
                        buttons=[{"label": "Copy to Clipboard", "action": _copy_recovered}],
                    )
                except Exception:
                    logger.debug("Recovery toast failed", exc_info=True)
            else:
                logger.info("Crash-recovered dictation produced no text")
            # Clean up the WAV now that text is on clipboard + in the .md log
            Path(wav_path).unlink(missing_ok=True)
        except Exception as e:
            logger.error(f"Failed to recover dictation: {e}")
            logger.info(f"Audio preserved at: {wav_path}")

    def recover_feature(self, wav_path: str):
        """Recover a crashed feature suggestion with interactive notification.

        Unlike dictation recovery (clipboard only), feature recovery routes
        the transcription to the feature log and applies Claude formatting.
        Shows a toast with Recover / Cancel options.
        """
        app = self.app
        logger.info(f"Recovering crashed feature suggestion from: {wav_path}")
        try:
            audio_np = self._load_wav(wav_path)
            dictation_model = app._gpu_guard.effective_model(
                app.cfg.get("dictation_model", app.cfg["model"]))
            text = app.worker.transcribe_fast(audio_np, model_override=dictation_model)
            if not text:
                logger.info("Crashed feature suggestion produced no text, cleaning up")
                Path(wav_path).unlink(missing_ok=True)
                return

            logger.info(f"Recovered feature text ({len(text)} chars): {text[:80]}...")

            # Show interactive notification with Recover / Cancel options
            def _do_recover():
                entry_id = feature_log.append_raw(text, 0)
                logger.info(f"Feature suggestion recovered to feature log: {entry_id}")
                # Format via Claude CLI in background
                submit_or_spawn(
                    IO, "feature-format",
                    lambda t=text, e=entry_id: self._format_feature_async(t, e),
                    native=True,
                )
                notify("Feature recovered", f"{len(text)} chars saved to feature log")

            def _do_cancel():
                logger.info("Feature recovery cancelled by user")

            try:
                preview = f'"{text[:120]}..."' if len(text) > 120 else f'"{text}"'
                notify(
                    "Recover feature suggestion?",
                    preview,
                    buttons=[
                        {"label": "Recover", "action": _do_recover},
                        {"label": "Cancel", "action": _do_cancel},
                    ],
                )
            except Exception:
                # If notification fails, auto-recover silently
                logger.debug("Feature recovery notification failed, auto-recovering",
                             exc_info=True)
                _do_recover()

            # Clean up WAV after showing notification (actions handle the rest)
            Path(wav_path).unlink(missing_ok=True)
        except Exception as e:
            logger.error(f"Failed to recover feature suggestion: {e}")
            logger.info(f"Audio preserved at: {wav_path}")

    # -- Helpers ---------------------------------------------------------------

    @staticmethod
    def _load_wav(wav_path: str):
        """Read a 16-bit WAV into the float32 array transcribe_fast expects."""
        import numpy as np
        import wave
        with wave.open(wav_path, "r") as wf:
            frames = wf.readframes(wf.getnframes())
            return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32767.0

    def _record_history(self, text: str):
        """Append to the recent-dictation history (menu source) and refresh."""
        with self._history_lock:
            self._history.append({
                "text": text,
                "timestamp": datetime.now().strftime("%H:%M"),
                "chars": len(text),
            })
            if len(self._history) > HISTORY_LIMIT:
                self._history = self._history[-HISTORY_LIMIT:]
        self.app._refresh_menu()

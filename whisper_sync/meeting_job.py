"""Step-based meeting job queue.

Each meeting becomes a self-contained MeetingJob with discrete processing
steps.  A single daemon thread pulls jobs and executes steps sequentially.
Recording start/stop is NEVER touched by the queue; only post-processing
runs here.

State management rules:
- step_transcribe sets meeting_transcribing = True at start
- step_complete sets meeting_transcribing = False but checks recorder first
- If recording is active when step_complete runs: only clear
  meeting_transcribing, do NOT change mode or emit MEETING_COMPLETED
- If recording is NOT active: clear meeting_transcribing and emit
  MEETING_COMPLETED with mode="done"
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger("whisper_sync.meeting_job")


class MeetingJob:
    """A meeting recording with its own state and processing steps."""

    def __init__(
        self,
        app,  # WhisperSync instance
        wav_path: Path,
        meeting_dir: Path,
        name: str,
        summarize: bool,
        date_time_str: str,
        week_dir: str,
        folder_name: str,
        diarize_method: str | None = None,
    ):
        self.app = app
        self.wav_path = wav_path
        self.meeting_dir = meeting_dir
        self.name = name
        self.summarize = summarize
        self.date_time_str = date_time_str
        self.week_dir = week_dir
        self.folder_name = folder_name
        self.diarize_method = diarize_method  # None = use config defaults

        # Populated during processing
        self.transcript_result = None  # dict from worker.transcribe()
        # Full parsed transcript dict, retained in memory after step_transcribe
        # so step_speaker_id can pass it to write_speaker_map and avoid a
        # background-thread json.load (which has caused 0x80000003 crashes).
        self.transcript_data: dict | None = None
        self.speakers_confirmed = None  # dict or None
        self.llm_ok = False  # whether Claude CLI is available

        self._steps = [
            self.step_transcribe,
            self.step_speaker_id,
            self.step_flatten,
            self.step_minutes,
            self.step_rename,
            self.step_index,
            self.step_notify,
            self.step_complete,
        ]
        self._current_step = 0

    @property
    def total_steps(self) -> int:
        return len(self._steps)

    @property
    def is_complete(self) -> bool:
        return self._current_step >= len(self._steps)

    @property
    def current_step_name(self) -> str:
        if self.is_complete:
            return "complete"
        return self._steps[self._current_step].__name__

    def execute_next_step(self):
        """Execute the next step. Returns True if more steps remain.

        Logs the step name at INFO before and after execution so a silent
        death mid-step can be pinpointed from the log. The step index is
        advanced only on successful completion so a failed step is not
        silently 'consumed' if any caller inspects/retries the job.
        """
        import time as _time
        if self.is_complete:
            return False
        step = self._steps[self._current_step]
        step_name = step.__name__
        job_label = self.name or "meeting"
        logger.info("step start: %s job=%s", step_name, job_label)
        started = _time.monotonic()
        try:
            step()
        except Exception:
            elapsed = _time.monotonic() - started
            # logger.exception emits the full traceback at the point of
            # failure so forensic logs pin which step failed AND why,
            # even if the outer handler only catches and re-labels.
            logger.exception(
                "step failed: %s job=%s elapsed=%.2fs",
                step_name, job_label, elapsed,
            )
            raise
        elapsed = _time.monotonic() - started
        logger.info(
            "step done: %s job=%s elapsed=%.2fs",
            step_name, job_label, elapsed,
        )
        self._current_step += 1
        return not self.is_complete

    # ------------------------------------------------------------------
    # Steps
    # ------------------------------------------------------------------

    def step_transcribe(self):
        """Transcribe the WAV file via the shared worker process."""
        from .state_manager import TRANSCRIPTION_STARTED
        from .worker_manager import WorkerCrashedError

        self.app.state.emit(TRANSCRIPTION_STARTED, mode=None, meeting_transcribing=True)

        if not self.app.worker.is_alive():
            logger.warning("Worker not alive, restarting...")
            self.app.worker.restart()
            if not self.app.worker.wait_ready(timeout=120):
                raise RuntimeError("Worker failed to restart")

        self.transcript_result = self.app.worker.transcribe(
            str(self.wav_path), diarize=True,
            diarize_method=self.diarize_method,
        )
        logger.debug(
            "Transcript saved: %s",
            self.transcript_result.get("json_path", self.wav_path),
        )

        # Pop the full parsed transcript dict off the result and stash it on
        # self. step_speaker_id passes this to write_speaker_map so the write
        # never has to re-read transcript.json on a background thread.
        # See speakers.write_speaker_map for the crash-mode rationale.
        self.transcript_data = self.transcript_result.pop("transcript_data", None)

        # Structured meeting result logging
        from .logger import log_meeting_result, log_transcript_preview
        from . import weekly_stats

        words = self.transcript_result.get("word_count", 0)
        speakers = self.transcript_result.get("num_speakers", 0)
        duration = self.transcript_result.get("duration", 0)
        folder_label = f"{self.week_dir}/{self.folder_name}/"
        log_meeting_result(
            self.name or "meeting", duration, words, speakers, folder_label
        )

        # Session stats
        self.app._stats["meetings"] += 1
        self.app._stats["total_meeting_seconds"] += int(duration)
        self.app._stats["total_meeting_words"] += words
        weekly_stats.record_meeting(int(duration), words)

        # Speaker segment previews
        segments = self.transcript_result.get("speaker_segments")
        if segments:
            log_transcript_preview("", speakers=segments)

        # Cache LLM availability for later steps
        self.llm_ok = self.app._is_claude_cli_available()

    def step_speaker_id(self):
        """Identify and confirm speakers via Claude CLI + tkinter dialog.

        If Claude times out or fails, still shows the dialog with empty names
        so the user can manually enter speaker names.

        FAILSAFE: Any failure in this step (Claude error, tkinter heap
        corruption, dialog crash, user skip, unexpected exception) results
        in placeholder speaker assignment so subsequent steps (flatten,
        minutes, rename, index, notify, complete) still run. The user can
        re-edit speakers later via the existing Meetings tray menu recovery
        flow (see __main__.py recovery handlers).
        """
        json_path = self.transcript_result.get(
            "json_path", str(self.meeting_dir / "transcript.json")
        ) if self.transcript_result else str(self.meeting_dir / "transcript.json")

        # Imports are inside the try/except so an import-time failure in
        # .speakers (or its transitive imports) also triggers the failsafe
        # path rather than aborting the pipeline before the catch-all.
        write_speaker_map = None
        try:
            from .speakers import (
                identify_speakers, write_speaker_map, update_config,
                get_config_path, build_manual_stub,
            )

            id_result = None
            if self.llm_ok:
                try:
                    cfg_path = get_config_path()
                    id_result = identify_speakers(json_path, cfg_path, self.folder_name)
                except Exception as e:
                    logger.warning("Speaker identification failed (non-fatal): %s", e)

            if not id_result or not id_result.get("speaker_map"):
                # Tailor reasoning and notification based on cause
                if self.llm_ok:
                    reason = "Auto-identification failed - enter name manually"
                    toast_title = "Speaker ID Failed"
                    toast_body = "Automatic speaker identification failed; enter names manually in the dialog."
                else:
                    reason = "Auto-identification unavailable (Claude CLI not available) - enter name manually"
                    toast_title = "Speaker ID Unavailable"
                    toast_body = "Speaker identification requires Claude CLI, which is not available. Enter speaker names manually in the dialog."

                id_result = build_manual_stub(json_path, reason)
                if id_result:
                    try:
                        from .notifications import notify
                        notify(toast_title, toast_body)
                    except Exception:
                        pass
                    logger.warning("Speaker ID not applied, showing manual entry dialog")

            if id_result and id_result.get("speaker_map"):
                try:
                    self.app._current_meeting_json_path = json_path
                    confirmation = self.app._ask_speaker_confirmation(id_result)
                    if confirmation:
                        if isinstance(confirmation, tuple):
                            confirmed_map, boundaries = confirmation
                        else:
                            confirmed_map = confirmation
                            boundaries = None

                        write_speaker_map(
                            json_path,
                            confirmed_map,
                            transcript_data=self.transcript_data,
                        )
                        # Release the large transcript dict for GC. After
                        # write_speaker_map returns, no other step in the job
                        # reads self.transcript_data, so keeping it alive only
                        # adds GC pressure on the post-processing thread.
                        self.transcript_data = None
                        cfg_path = get_config_path()
                        # config_updates from initial (light) identification.
                        # Deep mode config_updates are applied via the Meetings recovery flow.
                        update_config(
                            cfg_path, confirmed_map, id_result.get("config_updates")
                        )
                        logger.info("Speakers confirmed: %s", confirmed_map)
                        self.speakers_confirmed = confirmed_map

                        if boundaries:
                            logger.info(f"Meeting boundaries detected: {boundaries}")
                            self._detected_boundaries = boundaries
                            # Boundaries are informational - user can split via Meetings tray menu.
                            # Automatic splitting will be added in a future update.
                    else:
                        logger.info("Speaker identification skipped by user")
                except Exception as e:
                    logger.warning("Speaker confirmation dialog failed: %s", e)
            else:
                logger.info("No speakers found in transcript")
        except Exception as e:
            # Top-level catch: never let speaker_id failures break the
            # pipeline. tkinter heap corruption (speakers.py:541) and other
            # crashes have prevented steps 3-8 from running, leaving meetings
            # without minutes.md. Fail open: log, apply placeholders, move on.
            logger.warning(
                "Speaker ID failed/skipped - using placeholders, edit later via Meetings tray menu: %s",
                e,
            )

        # Failsafe: ensure placeholder speakers are assigned if confirmation
        # did not complete. This guarantees later steps have something to
        # work with and that transcript.json has a speaker_map written so
        # downstream consumers (flatten, minutes) behave consistently.
        if not self.speakers_confirmed:
            placeholder_map = self._build_placeholder_speaker_map(json_path)
            if placeholder_map:
                # Resolve write_speaker_map lazily here too in case the
                # earlier import inside the try block failed.
                _write_speaker_map = write_speaker_map
                if _write_speaker_map is None:
                    try:
                        from .speakers import write_speaker_map as _wsm
                        _write_speaker_map = _wsm
                    except Exception as e:
                        logger.warning(
                            "Could not import write_speaker_map for placeholder write: %s",
                            e,
                        )
                if _write_speaker_map is None:
                    logger.warning(
                        "Placeholder speakers NOT applied: speakers module unavailable. "
                        "Downstream steps will see speakers_confirmed unset."
                    )
                else:
                    try:
                        _write_speaker_map(
                            json_path,
                            placeholder_map,
                            transcript_data=self.transcript_data,
                        )
                    except Exception as e:
                        logger.warning(
                            "Could not write placeholder speaker map (leaving speakers_confirmed unset): %s",
                            e,
                        )
                    else:
                        self.speakers_confirmed = placeholder_map
                        logger.warning(
                            "Placeholder speakers applied: %s. Re-edit via Meetings tray menu recovery flow.",
                            placeholder_map,
                        )
                    finally:
                        # Release the transcript dict regardless of success.
                        # Downstream steps do not consume self.transcript_data;
                        # holding it just inflates the post-processing thread.
                        self.transcript_data = None

    def _build_placeholder_speaker_map(self, json_path: str) -> dict[str, str]:
        """Read SPEAKER_XX labels from transcript and produce a placeholder map.

        Tries build_manual_stub first (preferred path). Falls back to a
        minimal hard-coded map if even that fails (e.g., transcript.json
        missing or unreadable). Never raises.
        """
        try:
            from .speakers import build_manual_stub
            stub = build_manual_stub(
                json_path,
                "Auto-applied placeholder - edit via Meetings tray menu",
            )
            if stub and stub.get("speaker_map"):
                # Convert empty-name stub to "Speaker N" placeholders so
                # downstream rendering has readable labels.
                spk_map = stub["speaker_map"]
                return {
                    spk: f"Speaker {i + 1}"
                    for i, spk in enumerate(sorted(spk_map.keys()))
                }
        except Exception as e:
            logger.warning("build_manual_stub failed for placeholder map: %s", e)

        # Minimal fallback if transcript could not be read at all.
        return {"SPEAKER_00": "Speaker 1", "SPEAKER_01": "Speaker 2"}

    def step_flatten(self):
        """Flatten transcript JSON to readable text."""
        from .flatten import flatten as flatten_transcript

        try:
            json_path = self.transcript_result.get("json_path") if self.transcript_result else None
            if json_path:
                readable_path = flatten_transcript(json_path)
                if readable_path:
                    logger.info("Flattened transcript: %s", readable_path)
        except Exception as e:
            logger.warning("Auto-flatten failed (non-fatal): %s", e)

    def step_minutes(self):
        """Generate minutes via Claude CLI (only if summarize was requested)."""
        if not self.summarize:
            logger.debug("Summarize not requested, minutes step skipped")
            return

        if not self.llm_ok:
            # Show LLM warning only when user chose Summarize but CLI is missing
            suppress = self.app.cfg.get("suppress_llm_warning", False)
            if not suppress:
                from . import config
                dont_show = self.app._show_llm_unavailable()
                if dont_show:
                    self.app.cfg["suppress_llm_warning"] = True
                    config.save(self.app.cfg)
            logger.warning("Claude CLI not available, skipping summarize")
            return

        try:
            readable_file = self.meeting_dir / "transcript-readable.txt"
            minutes_file = self.meeting_dir / "minutes.md"
            if readable_file.exists() and not minutes_file.exists():
                self.app._generate_minutes(
                    self.meeting_dir, readable_file, minutes_file
                )
        except Exception as e:
            logger.warning("Auto-minutes failed (non-fatal): %s", e)

    def step_rename(self):
        """Offer rename via toast notification (only if summarize was requested)."""
        if not self.summarize:
            logger.debug("Summarize not requested, rename step skipped")
            return

        if not self.llm_ok:
            return

        try:
            minutes_file = self.meeting_dir / "minutes.md"
            if minutes_file.exists():
                summary = None
                for line in minutes_file.read_text(encoding="utf-8").splitlines():
                    if line.startswith("> Summary:"):
                        summary = line[len("> Summary:"):].strip()
                        break
                if summary:
                    self.app._ask_rename_suggestion(
                        self.name or "meeting",
                        summary,
                        meeting_dir=self.meeting_dir,
                        date_time_str=self.date_time_str,
                    )
                else:
                    logger.info(
                        "No > Summary: line found in minutes, rename skipped"
                    )
            else:
                logger.info("No minutes.md found, rename skipped")
        except Exception as e:
            logger.warning("Rename suggestion failed (non-fatal): %s", e)

    def step_index(self):
        """Rebuild week + root INDEX.md files."""
        from .rebuild_index import rebuild_root_index

        try:
            rebuild_root_index(self.app._output_dir())
        except Exception as e:
            logger.warning("Index rebuild failed (non-fatal): %s", e)

    def step_notify(self):
        """Show toast notification with meeting stats and Open Folder button.

        Honors the user's ``toast_events`` config (tray menu toggle). This
        toast is dispatched directly rather than through the TOAST_REGISTRY
        template so it can attach an Open Folder button, but it still must
        respect the configurable toggle — otherwise disabling 'Meeting
        Complete' in the tray menu has no effect.
        """
        from .notifications import notify, is_toast_enabled

        try:
            cfg = getattr(self.app, "cfg", None)
            if not is_toast_enabled("meeting_completed", cfg):
                return

            words = self.transcript_result.get("word_count", 0) if self.transcript_result else 0
            speakers = self.transcript_result.get("num_speakers", 0) if self.transcript_result else 0
            body = f"{words} words, {speakers} speakers"
            folder_path = str(self.meeting_dir)

            def _open_meeting_folder(p=folder_path):
                import subprocess as _sp
                _sp.Popen(["explorer", p])

            notify(
                "Meeting transcribed",
                body,
                buttons=[{"label": "Open Folder", "action": _open_meeting_folder}],
            )
        except Exception as e:
            logger.debug("Meeting toast failed (non-fatal): %s", e)

    def step_complete(self):
        """Final step: update state safely, respecting active recordings.

        CRITICAL: If a recording is active, we must NOT change mode or emit
        MEETING_COMPLETED.  We only clear meeting_transcribing so the icon
        updates correctly.
        """
        from .state_manager import MEETING_COMPLETED, IDLE

        recording_active = self.app.recorder.is_recording
        current_mode = self.app.state.current.mode

        if recording_active:
            # A new meeting/dictation is recording. Only clear the
            # transcribing flag; do NOT touch mode.
            logger.info(
                "Recording active during step_complete, preserving mode '%s'",
                current_mode,
            )
            self.app.state.emit(
                IDLE, meeting_transcribing=False
            )
        else:
            # No recording active. Safe to show completion animation.
            self.app.state.emit(
                MEETING_COMPLETED, meeting_transcribing=False, mode="done"
            )
            self.app._schedule_idle(3, blink=True)

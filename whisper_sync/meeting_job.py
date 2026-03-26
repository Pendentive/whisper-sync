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
    ):
        self.app = app
        self.wav_path = wav_path
        self.meeting_dir = meeting_dir
        self.name = name
        self.summarize = summarize
        self.date_time_str = date_time_str
        self.week_dir = week_dir
        self.folder_name = folder_name

        # Populated during processing
        self.transcript_result = None  # dict from worker.transcribe()
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
        """Execute the next step. Returns True if more steps remain."""
        if self.is_complete:
            return False
        step = self._steps[self._current_step]
        step()
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
            str(self.wav_path), diarize=True
        )
        logger.debug(
            "Transcript saved: %s",
            self.transcript_result.get("json_path", self.wav_path),
        )

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
        """Identify and confirm speakers via Claude CLI + tkinter dialog."""
        from .speakers import identify_speakers, write_speaker_map, update_config, get_config_path

        if not self.llm_ok:
            logger.info("Claude CLI not available, speaker identification skipped")
            return

        try:
            cfg_path = get_config_path()
            json_path = self.transcript_result.get(
                "json_path", str(self.meeting_dir / "transcript.json")
            )
            id_result = identify_speakers(json_path, cfg_path, self.folder_name)
            if id_result and id_result.get("speaker_map"):
                confirmed_map = self.app._ask_speaker_confirmation(id_result)
                if confirmed_map:
                    write_speaker_map(json_path, confirmed_map)
                    update_config(
                        cfg_path, confirmed_map, id_result.get("config_updates")
                    )
                    logger.info("Speakers confirmed: %s", confirmed_map)
                    self.speakers_confirmed = confirmed_map
                else:
                    logger.info("Speaker identification skipped by user")
            else:
                logger.info("No speakers identified from transcript")
        except Exception as e:
            logger.warning("Speaker identification failed (non-fatal): %s", e)

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
        """Show toast notification with meeting stats and Open Folder button."""
        from .notifications import notify

        try:
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

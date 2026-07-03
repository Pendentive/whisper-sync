"""Meeting workflow - record, save, post-process, recover.

Hardening round item 6 (architecture spec A2), extraction 2b.
MeetingFlow owns the meeting lifecycle end to end: the hotkey toggle,
start/stop, the save-and-enqueue path, the sequential post-processing
worker that drives MeetingJob steps, crash recovery for orphaned
meeting audio, the re-run speaker-ID flow, rename suggestions, and
minutes generation via the Claude CLI.

Composition matches DictationFlow and MeetingDialogs: the flow keeps an
app back-reference for shared services (cfg, state manager, recorder,
worker, GPU guard, dialogs, tray helpers); toggles run under the shared
app lock so mode checks stay atomic across workflows. Behavior is a
direct move from __main__.py.
"""

import queue
import threading
from datetime import datetime
from pathlib import Path

from .executors import IO, submit_or_spawn
from .flatten import flatten as flatten_transcript
from .logger import logger
from .meeting_dialogs import ABORT, sanitize_name
from .meeting_job import MeetingJob
from .notifications import notify
from .rebuild_index import rebuild_root_index
from .speakers import get_config_path
from .state_manager import (
    MEETING_STARTED, MEETING_STOPPED, MEETING_COMPLETED,
    TRANSCRIPTION_STARTED, ERROR, IDLE,
)


class MeetingFlow:
    """Owns meeting behavior; reads shared services through ``app``."""

    def __init__(self, app):
        self.app = app
        self._start_time: datetime | None = None
        self._post_queue = queue.Queue()  # completed meetings awaiting steps
        self._post_worker_thread = None   # started via start_post_worker()
        self._recovered_paths = []        # orphaned meeting WAVs found at startup
        self._recovering_meetings: set[str] = set()  # per-meeting re-entry guard

    # -- Post-processing pipeline lifecycle ----------------------------------

    def start_post_worker(self):
        """Start the single sequential post-processing thread (from run())."""
        self._post_worker_thread = threading.Thread(
            target=self._post_worker_loop, daemon=True, name="post-process-worker")
        self._post_worker_thread.start()
        logger.info("Post-processing worker started")

    def shutdown_post_worker(self):
        """Signal the post-processing thread to exit (from quit())."""
        self._post_queue.put(None)

    def pipeline_idle(self) -> bool:
        """True when no meeting job is queued or in flight (idle-GC gate)."""
        return self._post_queue.unfinished_tasks == 0

    # -- Startup recovery scan ------------------------------------------------

    def scan_recovered_temp(self):
        """Find orphaned meeting temp WAVs from a previous crash (from run())."""
        # Lazy: streaming_wav imports numpy; keep this module importable
        # on the dependency-light system python (CI suite).
        from .streaming_wav import fix_orphan

        self._recovered_paths = []
        temp_dir = self.temp_dir()
        for name in ("mic-temp.wav", "speaker-temp.wav"):
            temp_path = temp_dir / name
            if temp_path.exists():
                dur = fix_orphan(temp_path)
                if dur is not None:
                    logger.warning(
                        f"Recovered {dur:.0f}s of {name.split('-')[0]} audio "
                        f"from previous crash - file at {temp_dir / name}"
                    )
                    if name == "mic-temp.wav":
                        self._recovered_paths.append((str(temp_path), dur))
                else:
                    logger.info(f"Cleaned up stale temp file: {name}")

    def toggle(self):
        with self.app._lock:
            mode = self.app.state.current.mode if self.app.state else None
            if mode == "meeting":
                self._stop()
            elif self.app._can_record():
                self._start()

    def _start(self):
        # Lazy: capture imports numpy (system-python testability).
        from .capture import get_default_devices

        # Atomic claim, same rationale as the dictation start.
        if not self.app.state.try_transition(
            (None, "transcribing", "done", "error"),
            MEETING_STARTED, mode="meeting",
        ):
            logger.debug("meeting start rejected: mode changed concurrently")
            return
        self._start_time = datetime.now()
        mic = self.app.cfg.get("mic_device")
        speaker = self.app.cfg.get("speaker_device")
        if self.app.cfg.get("use_system_devices", True):
            defaults = get_default_devices()
            # Route mic through WASAPI too (not sd.default.device[0]'s MME)
            mic = defaults.get("input")
            speaker = defaults["output"]
        elif speaker is None:
            defaults = get_default_devices()
            speaker = defaults["output"]
        try:
            self.app.recorder.start(mic_device=mic, speaker_device=speaker)
        except Exception as e:
            logger.error("Failed to start mic for meeting: %s", e, exc_info=True)
            notify("Meeting unavailable", f"Mic could not be opened: {e}")
            self.app.state.emit(IDLE, mode=None)
            return
        if self.app.recorder.speaker_loopback_active:
            logger.info("Meeting started: mic + speaker loopback")
        else:
            logger.warning("Meeting started: mic only (speaker loopback unavailable)")
        temp = self.temp_dir()
        mic_temp = temp / "mic-temp.wav"
        try:
            self.app.recorder.start_streaming(mic_temp, disk_only=True)
        except Exception as e:
            logger.warning("Meeting streaming disabled: %s", e)
        if self.app.cfg.get("always_available_dictation", True):
            self.app._backup.preload()

    def _stop(self):
        audio = self.app.recorder.stop()

        if "mic" not in audio and "mic_path" not in audio:
            self.app.state.emit(IDLE, mode=None)
            return

        # Log meeting duration
        if self._start_time:
            _elapsed = (datetime.now() - self._start_time).total_seconds()
            _mins = int(_elapsed // 60)
            _secs = int(_elapsed % 60)
            logger.info(f"Meeting stopped: {_mins}m {_secs:02d}s recorded")

        # Stay in a processing state so clicks are ignored
        self.app.state.emit(MEETING_STOPPED, mode="saving")

        # Save WAV and enqueue for post-processing (never block recording thread)
        def _save_and_enqueue():
            from .capture import save_wav, save_stereo_wav
            dialog_result = self.app.dialogs.ask_meeting_name()

            if dialog_result is ABORT:
                logger.info("Recording discarded")
                self.app.recorder.discard_streaming()
                self.app.state.emit(IDLE, mode=None)
                return

            meeting_name, do_summarize, diarize_method = dialog_result
            if diarize_method:
                from .transcribe import DIARIZE_METHODS
                logger.info(f"Meeting saved: {meeting_name or 'meeting'} (summarize={do_summarize}, diarize={DIARIZE_METHODS.get(diarize_method, diarize_method)})")
            else:
                logger.info(f"Meeting saved: {meeting_name or 'meeting'} (summarize={do_summarize})")

            self.app.recorder.stop_streaming()
            try:
                start = self._start_time or datetime.now()
                week_dir = f"{start.strftime('%m')}-w{(start.day - 1) // 7 + 1}"
                date_time_str = start.strftime("%m%d_%H%M")
                folder_name = f"{date_time_str}_{meeting_name}" if meeting_name else f"{date_time_str}_meeting"
                meeting_dir = self.app._output_dir() / week_dir / folder_name
                meeting_dir.mkdir(parents=True, exist_ok=True)

                wav_path = meeting_dir / "recording.wav"

                # Speaker channel may arrive as a disk path (disk-streamed
                # at target rate - the flat-RAM path) or as an in-memory
                # array (legacy/RAM fallback). Normalize to an array here;
                # this read is the only transient large allocation left.
                speaker_arr = None
                if "speaker_path" in audio:
                    from .streaming_wav import StreamingWavWriter
                    speaker_arr = StreamingWavWriter.read_audio_from(
                        audio["speaker_path"]
                    ).reshape(-1, 1)
                elif "speaker" in audio:
                    speaker_arr = audio["speaker"]

                if "mic_path" in audio:
                    # Disk-only mode: mic audio already on disk as streaming WAV
                    mic_wav_path = audio["mic_path"]
                    if speaker_arr is not None:
                        from .streaming_wav import StreamingWavWriter
                        mic_array = StreamingWavWriter.read_audio_from(mic_wav_path)
                        save_stereo_wav(str(wav_path), mic_array.reshape(-1, 1), speaker_arr, self.app.cfg["sample_rate"])
                    else:
                        import shutil
                        shutil.move(str(mic_wav_path), str(wav_path))
                elif speaker_arr is not None:
                    save_stereo_wav(str(wav_path), audio["mic"], speaker_arr, self.app.cfg["sample_rate"])
                else:
                    save_wav(str(wav_path), audio["mic"], self.app.cfg["sample_rate"])

                logger.info(f"WAV saved: {wav_path}")
                from .streaming_wav import cleanup_temp_files
                cleanup_temp_files(self.temp_dir())

                # Enqueue for post-processing (transcription, speaker ID, etc.)
                job = MeetingJob(
                    app=self.app,
                    wav_path=wav_path,
                    meeting_dir=meeting_dir,
                    name=meeting_name,
                    summarize=do_summarize,
                    date_time_str=date_time_str,
                    week_dir=week_dir,
                    folder_name=folder_name,
                    diarize_method=diarize_method,
                )
                self._post_queue.put(job)
                qsize = self._post_queue.qsize()
                logger.info(f"Meeting queued: {meeting_name or 'meeting'} ({qsize} in queue)")

                if qsize > 1:
                    notify("Meeting queued", f"'{meeting_name or 'meeting'}' will transcribe when the current meeting finishes")

                # Release mode so user can start another meeting or dictate
                if self.app.state.current.mode == "saving":
                    self.app.state.emit(IDLE, mode=None, meeting_transcribing=True)

            except Exception as e:
                logger.error(f"Failed to save meeting WAV: {e}")
                import traceback
                logger.debug(traceback.format_exc())
                self.app.state.emit(ERROR, meeting_transcribing=False, mode="error", data={"message": str(e), "recoverable": False})
                self.app._schedule_idle(3)

        submit_or_spawn(IO, "meeting-save-enqueue", _save_and_enqueue)

    def _post_worker_loop(self):
        """Process meeting job steps sequentially.

        Runs as a daemon thread started in run(). Each meeting is a
        MeetingJob with discrete steps. The worker pulls a job and
        executes steps one at a time. If a step fails, the job is
        abandoned but the worker continues to the next job.

        Recording start/stop is NEVER touched here.
        """
        while True:
            job = self._post_queue.get()
            if job is None:
                logger.info("Post-processing worker shutting down")
                break  # Shutdown signal
            try:
                self._run_meeting_job(job)
            except Exception as e:
                logger.error(f"Post-processing failed for {job.name}: {e}", exc_info=True)
            finally:
                self._post_queue.task_done()
                # NOTE: do NOT call gc.collect() here. The cycle collector
                # is process-wide and runs on the calling thread's stack;
                # it can race with native C calls in any OTHER thread
                # (subprocess.communicate for Claude CLI, multiprocessing
                # queue feeders, pystray menu build) and crash with
                # 0x80000003. See PR #134 which introduced this regression.

    def _run_meeting_job(self, job: MeetingJob):
        """Execute all steps of a MeetingJob with error recovery.

        Each step is independent. If a step fails with a fatal error
        (WorkerCrashedError, PermissionError, FileNotFoundError), the
        job is abandoned and state is set to error. Non-fatal errors
        are handled within each step method.
        """
        from .worker_manager import WorkerCrashedError

        logger.info(f"Processing meeting: {job.name or 'meeting'}")

        try:
            while not job.is_complete:
                step_num = job._current_step + 1
                step_name = job.current_step_name
                logger.debug(
                    f"Step {step_num}/{job.total_steps}: {step_name} "
                    f"for {job.name or 'meeting'}"
                )
                job.execute_next_step()

            logger.info(f"Meeting done: {job.name or 'meeting'}")

        except WorkerCrashedError as e:
            self.app._gpu_guard.note_pressure_trigger("worker_crash_meeting")
            logger.error("Worker crashed during meeting, respawning...")
            logger.info(f"Audio is preserved at: {job.wav_path}")
            self.app.worker.restart()
            self._emit_error_safe(str(e))
        except PermissionError as e:
            logger.error(str(e))
            self.app._show_error_popup("Diarization Model Access", str(e))
            self._emit_error_safe(str(e))
        except FileNotFoundError as e:
            logger.error(str(e))
            err_str = str(e).lower()
            if "winerror 2" in err_str or "ffmpeg" in err_str:
                import shutil
                if not shutil.which("ffmpeg"):
                    self.app._show_error_popup(
                        "FFmpeg Not Found",
                        "FFmpeg is required for audio processing but is not installed or not on PATH.\n\n"
                        "Install with: winget install Gyan.FFmpeg\n"
                        "Then restart WhisperSync.",
                    )
                else:
                    self.app._show_error_popup("File Not Found", str(e))
            elif "hf" in err_str or "huggingface" in err_str or "token" in err_str:
                self.app._show_error_popup("Hugging Face Token Missing", str(e))
            else:
                self.app._show_error_popup("File Not Found", str(e))
            self._emit_error_safe(str(e))
        except Exception as e:
            logger.error(f"Meeting transcription error: {e}")
            import traceback
            logger.debug(traceback.format_exc())
            self._emit_error_safe(str(e))

    def _emit_error_safe(self, message: str):
        """Emit an error state, preserving mode if a recording is active."""
        current_mode = self.app.state.current.mode
        # If recording is active, keep the current mode
        safe_mode = current_mode if self.app.recorder.is_recording else "error"
        if safe_mode is None:
            safe_mode = "error"
        self.app.state.emit(
            ERROR,
            meeting_transcribing=False,
            mode=safe_mode,
            data={"message": message, "recoverable": False},
        )
        if safe_mode == "error":
            self.app._schedule_idle(3)

    def recover_meetings(self):
        """Show a dialog for each recovered meeting WAV, let user name and place it."""
        for wav_path, duration in self._recovered_paths:
            mins = int(duration // 60)
            secs = int(duration % 60)
            name = self.app.dialogs.ask_recovery_name(wav_path, f"{mins}m {secs}s")
            if name is ABORT:
                logger.info(f"Recovery skipped for {wav_path} - file preserved")
                continue
            # Move to local-transcriptions (month-based, MMDD_HHMM naming)
            from datetime import datetime as _dt, timedelta as _td
            mtime = _dt.fromtimestamp(Path(wav_path).stat().st_mtime)
            # Estimate start time from WAV duration
            try:
                import wave as _wave
                with _wave.open(wav_path, 'rb') as _wf:
                    _dur = _wf.getnframes() / _wf.getframerate()
                start = mtime - _td(seconds=_dur)
            except Exception:
                start = mtime
            week_dir = f"{start.strftime('%m')}-w{(start.day - 1) // 7 + 1}"
            date_time_str = start.strftime("%m%d_%H%M")
            folder_name = f"{date_time_str}_{name}" if name else f"{date_time_str}_recovered-meeting"
            meeting_dir = self.app._output_dir() / week_dir / folder_name
            meeting_dir.mkdir(parents=True, exist_ok=True)
            dest = meeting_dir / "recording.wav"
            Path(wav_path).rename(dest)
            logger.info(f"Recovered meeting moved to: {dest}")
            # Transcribe in background
            def _transcribe(path=str(dest)):
                try:
                    self.app.state.emit(TRANSCRIPTION_STARTED, meeting_transcribing=True)
                    result = self.app.worker.transcribe(path, diarize=True)
                    logger.info(f"Recovery transcript saved: {result.get('json_path', path)}")
                    try:
                        json_path = result.get('json_path')
                        if json_path:
                            flatten_transcript(json_path)
                    except Exception:
                        pass  # Non-fatal for recovery
                except Exception as e:
                    logger.error(f"Recovery transcription failed: {e}")
                    logger.info(f"Audio preserved at: {path}")
                finally:
                    if self.app.state.current.mode is None:
                        self.app.state.emit(MEETING_COMPLETED, meeting_transcribing=False, mode="done")
                        self.app._schedule_idle(3, blink=True)
                    else:
                        self.app.state.emit(IDLE, meeting_transcribing=False)
            threading.Thread(target=_transcribe, daemon=True).start()
        self._recovered_paths = []

    def recover_meeting_speakers(self, meeting_dir: Path):
        """Re-enter the speaker ID flow for a past meeting."""
        import json as _json
        from .speakers import identify_speakers, write_speaker_map, update_config, get_config_path, build_manual_stub
        from .flatten import flatten as flatten_transcript

        json_path = meeting_dir / "transcript.json"
        if not json_path.exists():
            logger.warning(f"No transcript.json in {meeting_dir}")
            return

        # Prevent duplicate clicks on the same meeting
        meeting_key = str(meeting_dir)
        if meeting_key in self._recovering_meetings:
            logger.info(f"Recovery already in progress for {meeting_dir.name}")
            return
        self._recovering_meetings.add(meeting_key)

        def _run():
            try:
                # Load transcript.json ONCE at thread entry, before any other
                # allocations or GC pressure. write_speaker_map requires a
                # pre-parsed dict on background threads to avoid the Windows
                # json.load / GC interleave crash (0x80000003). See
                # speakers.write_speaker_map docstring for the full rationale.
                transcript_data = None
                try:
                    with open(json_path) as _f:
                        transcript_data = _json.load(_f)
                except Exception as e:
                    logger.warning(
                        f"Recovery: could not pre-load transcript.json for {meeting_dir.name}: {e}"
                    )
                    return

                # Immediate feedback
                try:
                    from .notifications import notify
                    notify("Identifying speakers", f"Processing {meeting_dir.name}...")
                except Exception:
                    pass
                logger.info(f"Recovery: identifying speakers for {meeting_dir.name}")

                cfg_path = get_config_path()

                # Try Claude identification first
                id_result = None
                if self.is_claude_cli_available():
                    try:
                        id_result = identify_speakers(
                            str(json_path), cfg_path, meeting_dir.name
                        )
                    except Exception as e:
                        logger.warning(f"Speaker ID failed for recovery: {e}")

                # Fall back to manual stub
                if not id_result or not id_result.get("speaker_map"):
                    id_result = build_manual_stub(str(json_path))
                    if not id_result:
                        return

                if not id_result or not id_result.get("speaker_map"):
                    logger.warning("No speakers to identify")
                    return

                self.app._current_meeting_json_path = str(json_path)
                confirmed_map = self.app.dialogs.ask_speaker_confirmation(id_result)
                if not confirmed_map:
                    logger.info("Recovery: speaker identification skipped by user")
                    return

                # Write speaker map. Pass the pre-parsed dict so write_speaker_map
                # does not perform json.load on this background thread.
                write_speaker_map(str(json_path), confirmed_map, transcript_data=transcript_data)
                update_config(cfg_path, confirmed_map, id_result.get("config_updates"))
                logger.info(f"Recovery: speakers confirmed for {meeting_dir.name}: {confirmed_map}")

                # Re-flatten
                try:
                    flatten_transcript(str(json_path))
                    logger.info(f"Recovery: re-flattened {meeting_dir.name}")
                except Exception as e:
                    logger.warning(f"Recovery: flatten failed: {e}")

                # Regenerate minutes with updated speaker names
                readable_file = meeting_dir / "transcript-readable.txt"
                minutes_file = meeting_dir / "minutes.md"
                if readable_file.exists() and self.is_claude_cli_available():
                    try:
                        from .notifications import notify
                        notify(
                            f"Speakers updated: {meeting_dir.name}",
                            "Regenerating minutes with updated speaker names.",
                        )
                    except Exception:
                        pass
                    try:
                        self.generate_minutes(meeting_dir, readable_file, minutes_file)
                        logger.info(f"Recovery: minutes regenerated for {meeting_dir.name}")
                    except Exception as e:
                        logger.warning(f"Recovery: minutes generation failed: {e}")

                self.app._refresh_menu()

            finally:
                self._recovering_meetings.discard(meeting_key)

        threading.Thread(target=_run, daemon=True).start()

    def is_claude_cli_available(self) -> bool:
        """Check if Claude CLI is available for minutes generation."""
        import subprocess as _sp
        from .executors import native_call
        try:
            with native_call("claude-version"):
                r = _sp.run(["claude", "--version"], capture_output=True, text=True, timeout=5)
            return r.returncode == 0
        except (FileNotFoundError, _sp.TimeoutExpired):
            return False

    def _generate_name_suggestions(self, summary: str, current_name: str) -> list[str]:
        """Generate 3 meeting name suggestions via Claude CLI.

        Falls back to simple word extraction if CLI fails.
        """
        import subprocess as _sp
        import re

        prompt = (
            "Generate exactly 3 short meeting folder names based on this meeting summary. "
            "Rules:\n"
            "- Each name should be 2-4 words, kebab-case (e.g., architecture-soft-reset, migration-go-live-planning)\n"
            "- Names should capture WHAT the meeting was about, not WHO was in it\n"
            "- No dates, no generic words like 'meeting' or 'discussion' or 'sync'\n"
            "- Think like a PM labeling a folder they'll scan later\n"
            "- Output ONLY the 3 names, one per line, nothing else\n\n"
            f"Current name: {current_name}\n"
            f"Summary: {summary}"
        )

        try:
            from .executors import native_call
            with native_call("claude-name-suggest"):
                result = _sp.run(
                    ["claude", "-p", "--model", "haiku"],
                    input=prompt, capture_output=True, text=True, timeout=30,
                )
            if result.returncode == 0 and result.stdout.strip():
                lines = [l.strip().strip("-").strip() for l in result.stdout.strip().splitlines()]
                # Sanitize and filter
                suggestions = []
                for line in lines:
                    name = sanitize_name(line.lower())
                    if name and len(name) > 3 and name != current_name:
                        suggestions.append(name)
                if suggestions:
                    return suggestions[:3]
        except Exception:
            pass

        # Fallback: simple word extraction
        words = re.sub(r'[^a-zA-Z0-9\s]', '', summary).lower().split()
        stopwords = {'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for',
                     'of', 'with', 'by', 'is', 'was', 'are', 'were', 'be', 'been', 'has',
                     'had', 'have', 'that', 'this', 'from', 'not', 'all', 'can', 'will',
                     'meeting', 'discussion', 'sync', 'call'}
        meaningful = [w for w in words if w not in stopwords and len(w) > 2][:4]
        return ["-".join(meaningful)] if meaningful else [current_name]

    def _do_rename(self, meeting_dir: Path, date_time_str: str, new_name: str):
        """Perform the folder rename for a meeting directory."""
        import shutil
        new_folder_name = f"{date_time_str}_{new_name}"
        new_meeting_dir = meeting_dir.parent / new_folder_name
        if not new_meeting_dir.exists():
            shutil.move(str(meeting_dir), str(new_meeting_dir))
            logger.info(f"Renamed: {meeting_dir.name} -> {new_folder_name}")
            try:
                rebuild_root_index(self.app._output_dir())
            except Exception:
                pass
        else:
            logger.warning(f"Rename skipped - folder already exists: {new_folder_name}")

    def ask_rename_suggestion(self, current_name: str, summary: str,
                               meeting_dir=None, date_time_str: str | None = None):
        """Offer a meeting rename via toast notification (no tkinter).

        Uses a toast with an Accept button for the top suggestion.
        Dismissing or ignoring the toast keeps the original name.
        """
        suggestions = self._generate_name_suggestions(summary, current_name)
        suggested = suggestions[0] if suggestions else current_name

        if not meeting_dir or not date_time_str:
            return None

        if suggested == current_name:
            logger.info("Suggested name matches current name, rename skipped")
            return None

        def _accept_rename():
            self._do_rename(Path(meeting_dir), date_time_str, suggested)

        notify(
            "Rename meeting?",
            f"Suggested: {suggested}",
            buttons=[
                {"label": "Accept", "action": _accept_rename},
            ],
        )
        return None

    def generate_minutes(
        self,
        meeting_dir: Path,
        readable_file: Path,
        minutes_file: Path,
        transcript_data: dict | None = None,
    ):
        """Generate minutes.md via Claude CLI (claude -p) using the shared prompt template.

        When ``transcript_data`` is provided, the parsed dict is used for the
        speaker_map lookup and ``json.load`` is skipped. This avoids a Windows
        fatal exception (``0x80000003``) that fires when ``json.load`` runs on
        a background thread (the post-processing pipeline thread) and CPython's
        GC interleaves with the C-level decoder.
        """
        import json
        import subprocess as _sp

        prompt_file = Path(__file__).parent / "minutes_prompt.md"
        if not prompt_file.exists():
            logger.warning(f"Minutes prompt template not found: {prompt_file}")
            return

        prompt_text = prompt_file.read_text(encoding="utf-8")
        transcript_text = readable_file.read_text(encoding="utf-8")

        # Build speaker context from transcript.json speaker_map + config roles.
        # Use the in-memory dict when supplied; otherwise fall back to a disk
        # read (e.g., when called from the recovery flow on a fresh thread).
        speaker_context = ""
        try:
            if transcript_data is not None:
                tdata = transcript_data
                smap = tdata.get("speaker_map", {})
            else:
                json_path = meeting_dir / "transcript.json"
                if json_path.exists():
                    with open(json_path) as f:
                        tdata = json.load(f)
                    smap = tdata.get("speaker_map", {})
                else:
                    smap = {}
            if smap:
                cfg_path = Path(get_config_path())
                roles = {}
                if cfg_path.exists():
                    for line in cfg_path.read_text(encoding="utf-8").splitlines():
                        if line.startswith("| ") and " | " in line and "ID" not in line and "---" not in line:
                            parts = [p.strip() for p in line.split("|") if p.strip()]
                            if len(parts) >= 3:
                                roles[parts[1].lower()] = parts[2]
                ctx_lines = []
                for spk_id, name in smap.items():
                    role = roles.get(name.lower(), "")
                    ctx_lines.append(f"  {spk_id} = {name}" + (f" ({role})" if role else ""))
                speaker_context = "\n".join(ctx_lines)
        except Exception:
            pass

        # Inject speaker context into prompt
        prompt_text = prompt_text.replace(
            "{SPEAKER_CONTEXT}",
            speaker_context or "No speaker identification available - use context clues from the transcript."
        )

        # Build the full prompt: template + transcript
        full_prompt = (
            f"{prompt_text}\n\n"
            f"---\n\n"
            f"Meeting folder: {meeting_dir.name}\n\n"
            f"Transcript:\n\n{transcript_text}"
        )

        logger.info(f"Generating minutes via Claude CLI for: {meeting_dir.name}")
        try:
            from .executors import native_call
            with native_call("claude-minutes"):
                result = _sp.run(
                    ["claude", "-p", "--model", "sonnet"],
                    input=full_prompt,
                    capture_output=True,
                    text=True,
                    timeout=300,  # 5 minutes max
                    cwd=str(Path(__file__).parent.parent.parent),  # repo root
                )
            if result.returncode == 0 and result.stdout.strip():
                minutes_file.write_text(result.stdout, encoding="utf-8")
                logger.info(f"Minutes saved: {minutes_file}")
            else:
                logger.warning(f"Claude CLI returned code {result.returncode}")
                if result.stderr:
                    logger.debug(f"stderr: {result.stderr[:500]}")
        except FileNotFoundError:
            logger.warning("Claude CLI not found - minutes generation skipped. Install: npm i -g @anthropic-ai/claude-code")
        except _sp.TimeoutExpired:
            logger.warning("Claude CLI timed out generating minutes (5 min limit)")

    def temp_dir(self) -> Path:
        return Path(__file__).parent / "logs" / "data" / "meeting"

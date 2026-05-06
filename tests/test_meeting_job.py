"""Tests for whisper_sync.meeting_job.MeetingJob.execute_next_step.

Only the step-driver behavior is exercised here. The actual step
implementations touch the worker subprocess / filesystem / state manager
and are out of scope.
"""

import sys
import types
import unittest
from pathlib import Path
from unittest import mock


class _StubApp:
    """Minimal stand-in for the WhisperSync application object."""

    def __init__(self):
        self._current_meeting_json_path = None

    def _ask_speaker_confirmation(self, id_result):
        # Default: skip confirmation (user clicked cancel).
        return None


def _make_job(steps):
    from whisper_sync.meeting_job import MeetingJob

    job = MeetingJob(
        app=_StubApp(),
        wav_path=Path("/tmp/x.wav"),
        meeting_dir=Path("/tmp/x"),
        name="unit-test",
        summarize=False,
        date_time_str="0101_0000",
        week_dir="01-w1",
        folder_name="0101_0000_unit-test",
    )
    # Replace real steps with cheap callables so we can exercise the driver
    # without touching the worker/filesystem.
    job._steps = list(steps)
    return job


class ExecuteNextStepTests(unittest.TestCase):
    def test_successful_step_advances_index(self):
        calls = []
        job = _make_job([lambda: calls.append("a"), lambda: calls.append("b")])
        self.assertEqual(job._current_step, 0)
        job.execute_next_step()
        self.assertEqual(job._current_step, 1)
        job.execute_next_step()
        self.assertEqual(job._current_step, 2)
        self.assertTrue(job.is_complete)

    def test_failed_step_does_not_advance_index(self):
        # Regression for review #2: the step index must not be 'consumed'
        # when a step raises — otherwise a retry/inspection would think
        # the step completed.
        def _boom():
            raise RuntimeError("kaboom")
        job = _make_job([_boom, lambda: None])
        self.assertEqual(job._current_step, 0)
        with self.assertRaises(RuntimeError):
            job.execute_next_step()
        self.assertEqual(
            job._current_step, 0,
            "failed step should leave _current_step unchanged",
        )

    def test_returns_true_when_more_steps_remain(self):
        job = _make_job([lambda: None, lambda: None])
        self.assertTrue(job.execute_next_step())

    def test_returns_false_when_complete(self):
        job = _make_job([lambda: None])
        self.assertFalse(job.execute_next_step())


def _make_speaker_id_job(app=None):
    """Build a MeetingJob suitable for exercising step_speaker_id.

    Sets transcript_result so step_speaker_id has a json_path and llm_ok=True
    so identify_speakers gets called.
    """
    from whisper_sync.meeting_job import MeetingJob

    job = MeetingJob(
        app=app or _StubApp(),
        wav_path=Path("/tmp/x.wav"),
        meeting_dir=Path("/tmp/x"),
        name="speaker-id-test",
        summarize=False,
        date_time_str="0101_0000",
        week_dir="01-w1",
        folder_name="0101_0000_speaker-id-test",
    )
    job.transcript_result = {"json_path": "/tmp/x/transcript.json"}
    job.llm_ok = True
    return job


def _install_fake_speakers_module(
    identify_side_effect=None,
    build_stub_returns=None,
    write_side_effect=None,
):
    """Install a fake whisper_sync.speakers module and return the writes list.

    Returns a dict with 'writes' (list of (path, map) tuples for successful
    write_speaker_map calls) and the patcher context manager.
    """
    fake = types.ModuleType("whisper_sync.speakers")
    writes = []

    def _identify_speakers(json_path, cfg_path, folder_name):
        if identify_side_effect is not None:
            raise identify_side_effect
        return {"speaker_map": {"SPEAKER_00": "Alice"}}

    def _write_speaker_map(json_path, speaker_map, transcript_data=None):
        if write_side_effect is not None:
            raise write_side_effect
        writes.append((json_path, dict(speaker_map)))

    def _update_config(cfg_path, speaker_map, config_updates=None):
        return None

    def _get_config_path():
        return Path("/tmp/cfg.json")

    def _build_manual_stub(json_path, reason):
        if build_stub_returns is not None:
            return build_stub_returns
        return {"speaker_map": {"SPEAKER_00": "", "SPEAKER_01": ""}}

    fake.identify_speakers = _identify_speakers
    fake.write_speaker_map = _write_speaker_map
    fake.update_config = _update_config
    fake.get_config_path = _get_config_path
    fake.build_manual_stub = _build_manual_stub

    return fake, writes


class StepSpeakerIdFailsafeTests(unittest.TestCase):
    """Regression coverage for the step_speaker_id failsafe path.

    These confirm that the step does not propagate exceptions from the
    speaker identification or confirmation dialog, and that placeholders
    are applied (and persisted via write_speaker_map) so downstream steps
    have a usable speaker map.
    """

    def test_step_speaker_id_does_not_raise_when_identify_fails(self):
        # identify_speakers raises - failsafe should kick in, dialog is
        # bypassed (build_manual_stub provides id_result), confirmation
        # returns None, placeholder map is written.
        fake, writes = _install_fake_speakers_module(
            identify_side_effect=RuntimeError("claude exploded"),
        )
        with mock.patch.dict(sys.modules, {"whisper_sync.speakers": fake}):
            job = _make_speaker_id_job()
            # Should NOT raise.
            job.step_speaker_id()

        # Placeholder map should be applied and persisted.
        self.assertIsNotNone(
            job.speakers_confirmed,
            "speakers_confirmed should be set to a placeholder map",
        )
        self.assertIsInstance(job.speakers_confirmed, dict)
        self.assertTrue(len(job.speakers_confirmed) >= 1)
        # The placeholder write should have hit write_speaker_map at least once.
        self.assertTrue(
            any(w[1] == job.speakers_confirmed for w in writes),
            f"placeholder map should have been written; writes={writes}",
        )

    def test_step_speaker_id_does_not_raise_when_dialog_fails(self):
        # identify_speakers succeeds, but the confirmation dialog raises.
        # The step must still complete; placeholder map should be applied
        # since speakers_confirmed never got set by the confirmation path.
        class _BoomApp(_StubApp):
            def _ask_speaker_confirmation(self, id_result):
                raise RuntimeError("tkinter heap corruption")

        fake, writes = _install_fake_speakers_module()
        with mock.patch.dict(sys.modules, {"whisper_sync.speakers": fake}):
            job = _make_speaker_id_job(app=_BoomApp())
            # Should NOT raise.
            job.step_speaker_id()

        self.assertIsNotNone(
            job.speakers_confirmed,
            "speakers_confirmed should be set to a placeholder map after dialog failure",
        )
        self.assertIsInstance(job.speakers_confirmed, dict)

    def test_step_speaker_id_retains_transcript_data_for_downstream_steps(self):
        # Regression: step_flatten and step_minutes must reuse the in-memory
        # transcript dict to avoid background-thread json.load (0x80000003
        # crash). Therefore step_speaker_id must NOT release the dict.
        # Confirmed-write path.
        class _AcceptApp(_StubApp):
            def _ask_speaker_confirmation(self, id_result):
                return {"SPEAKER_00": "Alice"}

        fake, writes = _install_fake_speakers_module()
        with mock.patch.dict(sys.modules, {"whisper_sync.speakers": fake}):
            job = _make_speaker_id_job(app=_AcceptApp())
            tdata = {"segments": [{"speaker": "SPEAKER_00"}]}
            job.transcript_data = tdata
            job.step_speaker_id()

        self.assertEqual(
            job.speakers_confirmed, {"SPEAKER_00": "Alice"},
            "confirmed map should be applied",
        )
        self.assertIs(
            job.transcript_data, tdata,
            "transcript_data must persist past speaker_id for flatten/minutes",
        )

    def test_step_speaker_id_retains_transcript_data_on_placeholder_path(self):
        # Same regression, placeholder-write path (dialog skipped).
        fake, _writes = _install_fake_speakers_module(
            identify_side_effect=RuntimeError("claude exploded"),
        )
        with mock.patch.dict(sys.modules, {"whisper_sync.speakers": fake}):
            job = _make_speaker_id_job()
            tdata = {"segments": [{"speaker": "SPEAKER_00"}]}
            job.transcript_data = tdata
            job.step_speaker_id()

        self.assertIsNotNone(job.speakers_confirmed)
        self.assertIs(
            job.transcript_data, tdata,
            "transcript_data must persist past placeholder write",
        )

    def test_step_speaker_id_leaves_speakers_unset_when_write_fails(self):
        # If even the placeholder write fails, speakers_confirmed should
        # remain None so downstream steps see the same state as before
        # the failsafe existed (rather than logs/memory claiming a map
        # exists when transcript.json was never updated).
        fake, _writes = _install_fake_speakers_module(
            identify_side_effect=RuntimeError("claude exploded"),
            write_side_effect=OSError("disk full"),
        )
        with mock.patch.dict(sys.modules, {"whisper_sync.speakers": fake}):
            job = _make_speaker_id_job()
            job.step_speaker_id()

        self.assertIsNone(
            job.speakers_confirmed,
            "speakers_confirmed must remain unset when write_speaker_map fails",
        )


class StepFlattenAndCompleteTests(unittest.TestCase):
    """Regression coverage: step_flatten must pass the in-memory transcript
    dict through to ``flatten`` (avoids 0x80000003), and step_complete must
    release the dict at the end of the pipeline.
    """

    def test_step_flatten_passes_transcript_data(self):
        fake = types.ModuleType("whisper_sync.flatten")
        captured = {}

        def _flatten(json_path, transcript_data=None):
            captured["json_path"] = json_path
            captured["transcript_data"] = transcript_data
            return "/tmp/x/transcript-readable.txt"

        fake.flatten = _flatten

        with mock.patch.dict(sys.modules, {"whisper_sync.flatten": fake}):
            job = _make_speaker_id_job()
            tdata = {"segments": [{"speaker": "SPEAKER_00"}]}
            job.transcript_data = tdata
            job.step_flatten()

        self.assertEqual(captured["json_path"], "/tmp/x/transcript.json")
        self.assertIs(
            captured["transcript_data"], tdata,
            "step_flatten must forward in-memory transcript dict to flatten",
        )

    def test_step_complete_releases_transcript_data(self):
        # Build a minimal stub app with the surface step_complete needs.
        class _CompleteApp(_StubApp):
            def __init__(self):
                super().__init__()
                self.recorder = types.SimpleNamespace(is_recording=False)
                self.state = types.SimpleNamespace(
                    current=types.SimpleNamespace(mode="meeting"),
                    emit=lambda *a, **kw: None,
                )

            def _schedule_idle(self, *a, **kw):
                return None

        from whisper_sync.meeting_job import MeetingJob

        job = MeetingJob(
            app=_CompleteApp(),
            wav_path=Path("/tmp/x.wav"),
            meeting_dir=Path("/tmp/x"),
            name="complete-test",
            summarize=False,
            date_time_str="0101_0000",
            week_dir="01-w1",
            folder_name="0101_0000_complete-test",
        )
        job.transcript_data = {"segments": []}
        job.step_complete()
        self.assertIsNone(
            job.transcript_data,
            "step_complete must release transcript_data so the dict can be GC'd",
        )


if __name__ == "__main__":
    unittest.main()

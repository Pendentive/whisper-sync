"""Tests for whisper_sync.meeting_job.MeetingJob.execute_next_step.

Only the step-driver behavior is exercised here. The actual step
implementations touch the worker subprocess / filesystem / state manager
and are out of scope.
"""

import unittest
from pathlib import Path


class _StubApp:
    """Minimal stand-in for the WhisperSync application object."""


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


if __name__ == "__main__":
    unittest.main()

"""Tests for whisper_sync.split_meeting - the split pipeline on synthetic audio.

Regression focus (2026-07-03 production bug): portion [1] starts at t=0,
so its MMDD_HHMM prefix equals the source folder's; reusing the source's
name made dest == source and the copy crashed. The staging rename must
make that case work, and pre-flight checks must refuse duplicate or
already-existing destinations before any file is touched.
"""

import json
import os
import struct
import unittest
import tempfile
import wave
from datetime import datetime, timedelta
from pathlib import Path

from whisper_sync.split_meeting import split_meeting


def _write_wav(path: Path, seconds: float, rate: int = 16000):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(struct.pack(f"<{int(rate * seconds)}h",
                                  *([0] * int(rate * seconds))))


def _transcript(duration: float) -> dict:
    return {
        "speaker_map": {"SPEAKER_00": "Alice"},
        "segments": [
            {"speaker": "SPEAKER_00", "text": "First half.",
             "start": 0.5, "end": min(1.5, duration)},
            {"speaker": "SPEAKER_00", "text": "Second half.",
             "start": min(duration - 1.0, 2.5), "end": duration - 0.1},
        ],
    }


class _SplitHarness(unittest.TestCase):
    DURATION = 4.0

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _make_source(self, name_suffix: str):
        """Create a source meeting folder placed exactly where portion [1]
        with the same suffix would land, so name reuse == full collision."""
        scratch = self.root / "scratch"
        scratch.mkdir()
        wav = scratch / "recording.wav"
        _write_wav(wav, self.DURATION)
        # Pin the mtime to second 30 of the current minute: portion
        # prefixes derive from mtime minus offsets inside DURATION, and a
        # real mtime within a couple seconds of a minute boundary gave
        # portions DIFFERENT MMDD_HHMM prefixes - the duplicate-name test
        # then saw distinct destinations and flaked (CI 2026-07-04).
        pinned = datetime.now().replace(second=30, microsecond=0).timestamp()
        os.utime(str(wav), (pinned, pinned))
        # Recreate split_meeting's own math: portion 1 start time derives
        # from the wav mtime minus total duration.
        mtime = datetime.fromtimestamp(os.path.getmtime(str(wav)))
        start = mtime - timedelta(seconds=self.DURATION)
        week = f"{start.strftime('%m')}-w{(start.day - 1) // 7 + 1}"
        folder_name = f"{start.strftime('%m%d_%H%M')}_{name_suffix}"
        source = self.root / week / folder_name
        source.parent.mkdir(parents=True, exist_ok=True)
        os.rename(str(scratch), str(source))
        (source / "transcript.json").write_text(json.dumps(_transcript(self.DURATION)))
        return source

    def _portions(self):
        """All meeting folders under the temp root (post-split state)."""
        return sorted(
            p for p in self.root.glob("*/*")
            if p.is_dir() and not p.name.endswith(".splitting")
        )


class NameReuseTests(_SplitHarness):
    def test_portion_one_may_reuse_the_source_name(self):
        source = self._make_source("Patrick")
        split_meeting(source, [2.0], ["Patrick", "SecondPart"])

        portions = self._portions()
        self.assertEqual(len(portions), 2, f"expected 2 portions, got {portions}")
        for p in portions:
            for f in ("recording.wav", "transcript.json", "transcript-readable.txt"):
                self.assertTrue((p / f).exists(), f"{p.name} missing {f}")
        # The reused name is one of the portions and holds TRIMMED audio.
        reused = [p for p in portions if p.name.endswith("_Patrick")]
        self.assertEqual(len(reused), 1)
        with wave.open(str(reused[0] / "recording.wav"), "rb") as w:
            dur = w.getnframes() / w.getframerate()
        self.assertLess(dur, 3.0, "portion audio must be trimmed, not the full source")
        self.assertFalse(
            any(p.name.endswith(".splitting") for p in self.root.glob("*/*")),
            "staging folder must be removed after a successful split",
        )

    def test_distinct_names_still_work(self):
        source = self._make_source("Original")
        split_meeting(source, [2.0], ["PartA", "PartB"])
        self.assertEqual(len(self._portions()), 2)
        self.assertFalse(source.exists(), "source must be removed after success")


class PreflightTests(_SplitHarness):
    def test_duplicate_portion_names_rejected_before_any_write(self):
        source = self._make_source("Original")
        with self.assertRaises(ValueError):
            split_meeting(source, [2.0], ["Same", "Same"])
        self.assertTrue(source.exists(), "source untouched on pre-flight failure")
        self.assertTrue((source / "recording.wav").exists())

    def test_existing_unrelated_destination_rejected(self):
        source = self._make_source("Original")
        # Fabricate a folder exactly where portion 1 named "Occupied" lands.
        mtime = datetime.fromtimestamp(
            os.path.getmtime(str(source / "recording.wav")))
        start = mtime - timedelta(seconds=self.DURATION)
        week = f"{start.strftime('%m')}-w{(start.day - 1) // 7 + 1}"
        occupied = self.root / week / f"{start.strftime('%m%d_%H%M')}_Occupied"
        occupied.mkdir(parents=True)
        (occupied / "keep.txt").write_text("existing meeting")

        with self.assertRaises(FileExistsError):
            split_meeting(source, [2.0], ["Occupied", "Other"])
        self.assertTrue((occupied / "keep.txt").exists(),
                        "existing folder must never be clobbered")
        self.assertTrue(source.exists(), "source untouched on pre-flight failure")

    def test_mid_split_failure_rolls_staging_back(self):
        # Regression for PR review: an unexpected error inside the split
        # loop must rename the staged source back to its original name.
        from unittest import mock
        import whisper_sync.split_meeting as sm
        source = self._make_source("Original")
        with mock.patch.object(sm, "trim_wav_inplace",
                               side_effect=RuntimeError("disk full")):
            with self.assertRaises(RuntimeError):
                split_meeting(source, [2.0], ["PartA", "PartB"])
        self.assertTrue(source.exists(), "source must be restored on failure")
        self.assertTrue((source / "transcript.json").exists())
        self.assertFalse(
            source.with_name(source.name + ".splitting").exists(),
            "staging must not linger after rollback",
        )

    def test_leftover_staging_folder_rejected(self):
        source = self._make_source("Original")
        staging = source.with_name(source.name + ".splitting")
        staging.mkdir()
        with self.assertRaises(FileExistsError):
            split_meeting(source, [2.0], ["A", "B"])
        self.assertTrue(source.exists())


if __name__ == "__main__":
    unittest.main()

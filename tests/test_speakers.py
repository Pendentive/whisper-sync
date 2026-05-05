"""Tests for whisper_sync.speakers.write_speaker_map.

Specifically the in-memory path that avoids json.load on background threads,
which is the root cause of the recurring Windows 0x80000003 crashes.
"""

import json
import tempfile
import unittest
from pathlib import Path


class WriteSpeakerMapTests(unittest.TestCase):
    """Cover both the in-memory path and the disk-fallback path."""

    def test_uses_in_memory_dict_when_provided(self):
        """If transcript_data is provided, do not read the file at all.

        We write a minimal stub to disk and an entirely DIFFERENT in-memory
        dict. The persisted result must reflect the in-memory dict (with
        the new speaker_map), proving the disk read was skipped.
        """
        from whisper_sync.speakers import write_speaker_map

        with tempfile.TemporaryDirectory() as tmp:
            json_path = Path(tmp) / "transcript.json"
            # On-disk content is intentionally stale / different.
            json_path.write_text(json.dumps({"segments": [{"text": "STALE"}]}))

            in_memory = {
                "segments": [{"text": "FRESH"}],
                "language": "en",
            }
            speaker_map = {"SPEAKER_00": "Alice", "SPEAKER_01": "Bob"}

            write_speaker_map(
                str(json_path), speaker_map, transcript_data=in_memory,
            )

            written = json.loads(json_path.read_text())
            self.assertEqual(written["speaker_map"], speaker_map)
            # The in-memory dict (FRESH) should have been written, not STALE.
            self.assertEqual(written["segments"], [{"text": "FRESH"}])
            self.assertEqual(written["language"], "en")
            # In-memory dict was mutated in place.
            self.assertEqual(in_memory["speaker_map"], speaker_map)

    def test_falls_back_to_disk_when_no_dict_provided(self):
        """Backward-compat: if transcript_data is None, read from disk."""
        from whisper_sync.speakers import write_speaker_map

        with tempfile.TemporaryDirectory() as tmp:
            json_path = Path(tmp) / "transcript.json"
            original = {
                "segments": [{"text": "hello"}],
                "language": "en",
            }
            json_path.write_text(json.dumps(original))

            speaker_map = {"SPEAKER_00": "Alice"}
            write_speaker_map(str(json_path), speaker_map)

            written = json.loads(json_path.read_text())
            self.assertEqual(written["speaker_map"], speaker_map)
            # Original keys preserved.
            self.assertEqual(written["segments"], [{"text": "hello"}])
            self.assertEqual(written["language"], "en")

    def test_default_str_handles_non_serializable(self):
        """The dump uses default=str so numpy-ish floats from whisperX work.

        We can't easily instantiate a real numpy float here without numpy,
        so we use a custom object with __str__. The point is just that
        json.dump(..., default=str) does not raise on types it would
        otherwise reject.
        """
        from whisper_sync.speakers import write_speaker_map

        class Weird:
            def __str__(self):
                return "weird-value"

        with tempfile.TemporaryDirectory() as tmp:
            json_path = Path(tmp) / "transcript.json"
            json_path.write_text("{}")

            in_memory = {"odd_field": Weird()}
            speaker_map = {"SPEAKER_00": "Alice"}

            # Should not raise.
            write_speaker_map(
                str(json_path), speaker_map, transcript_data=in_memory,
            )

            written = json.loads(json_path.read_text())
            self.assertEqual(written["speaker_map"], speaker_map)
            self.assertEqual(written["odd_field"], "weird-value")


if __name__ == "__main__":
    unittest.main()

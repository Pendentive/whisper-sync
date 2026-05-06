"""Tests for whisper_sync.flatten.

Covers the in-memory ``transcript_data`` argument that lets the
post-processing pipeline avoid a second ``json.load`` on the background
thread (root cause of the 0x80000003 crash mode).
"""

import json
import tempfile
import unittest
from pathlib import Path


SAMPLE_TRANSCRIPT = {
    "speaker_map": {"SPEAKER_00": "Alice", "SPEAKER_01": "Bob"},
    "segments": [
        {"speaker": "SPEAKER_00", "text": "Hello there.", "start": 0.0, "end": 2.0},
        {"speaker": "SPEAKER_01", "text": "Hi Alice.", "start": 2.5, "end": 4.0},
        {"speaker": "SPEAKER_00", "text": "How are you?", "start": 4.5, "end": 6.0},
    ],
}


class FlattenTests(unittest.TestCase):
    def test_flatten_uses_transcript_data_when_provided(self):
        from whisper_sync.flatten import flatten

        with tempfile.TemporaryDirectory() as tmp:
            json_path = Path(tmp) / "transcript.json"
            # Intentionally write garbage to disk to prove the in-memory dict
            # was used (no json.load happened on the file).
            json_path.write_text("{not valid json", encoding="utf-8")

            out_path = flatten(str(json_path), transcript_data=SAMPLE_TRANSCRIPT)

            self.assertTrue(out_path)
            content = Path(out_path).read_text(encoding="utf-8")
            self.assertIn("[Alice]", content)
            self.assertIn("[Bob]", content)
            self.assertIn("Hello there.", content)

    def test_flatten_falls_back_to_disk_when_no_transcript_data(self):
        # Default behavior (CLI / recovery flow) still needs to work.
        from whisper_sync.flatten import flatten

        with tempfile.TemporaryDirectory() as tmp:
            json_path = Path(tmp) / "transcript.json"
            json_path.write_text(
                json.dumps(SAMPLE_TRANSCRIPT), encoding="utf-8"
            )

            out_path = flatten(str(json_path))

            self.assertTrue(out_path)
            content = Path(out_path).read_text(encoding="utf-8")
            self.assertIn("[Alice]", content)
            self.assertIn("[Bob]", content)


if __name__ == "__main__":
    unittest.main()

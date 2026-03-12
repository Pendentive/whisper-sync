"""Streaming WAV writer — writes PCM chunks to disk as they arrive.

Used as a crash safety net for meeting recordings. Audio is written
incrementally so that a crash preserves all data up to that point.
On recovery, the WAV header is fixed to reflect the actual data size.
"""

import struct
import threading
from pathlib import Path

import numpy as np


class StreamingWavWriter:
    """Incrementally write float32 audio chunks as int16 PCM WAV."""

    def __init__(self, path: Path | str, channels: int = 1, rate: int = 16000):
        self._path = Path(path)
        self._channels = channels
        self._rate = rate
        self._bytes_written = 0
        self._lock = threading.Lock()
        self._file = None

        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self._path, "wb")
        self._write_header(0)  # placeholder — fixed on close

    def _write_header(self, data_size: int) -> None:
        """Write a standard 44-byte WAV header."""
        # RIFF header
        self._file.write(b"RIFF")
        self._file.write(struct.pack("<I", 36 + data_size))  # file size - 8
        self._file.write(b"WAVE")
        # fmt sub-chunk
        self._file.write(b"fmt ")
        self._file.write(struct.pack("<I", 16))  # sub-chunk size
        self._file.write(struct.pack("<H", 1))   # PCM format
        self._file.write(struct.pack("<H", self._channels))
        self._file.write(struct.pack("<I", self._rate))
        bytes_per_sec = self._rate * self._channels * 2  # 2 bytes per int16
        self._file.write(struct.pack("<I", bytes_per_sec))
        self._file.write(struct.pack("<H", self._channels * 2))  # block align
        self._file.write(struct.pack("<H", 16))  # bits per sample
        # data sub-chunk
        self._file.write(b"data")
        self._file.write(struct.pack("<I", data_size))

    def write(self, chunk: np.ndarray) -> None:
        """Convert float32 chunk to int16 and append to file.

        Safe to call from audio callbacks — uses a lock internally.
        """
        int_data = (chunk * 32767).astype(np.int16).tobytes()
        with self._lock:
            if self._file is None:
                return
            self._file.write(int_data)
            self._bytes_written += len(int_data)

    def close(self) -> None:
        """Finalize: fix the WAV header with actual data size, close file."""
        if self._file is None:
            return
        with self._lock:
            # Seek back and rewrite header with correct sizes
            self._file.seek(0)
            self._write_header(self._bytes_written)
            self._file.close()
            self._file = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def bytes_written(self) -> int:
        return self._bytes_written


def fix_orphan(path: Path | str, rate: int = 16000, channels: int = 1) -> float | None:
    """Fix an orphaned temp WAV file left by a crash.

    Reads the raw file size, rewrites the WAV header with correct sizes.
    Returns the duration in seconds, or None if the file is too short (<5s).
    """
    path = Path(path)
    if not path.exists():
        return None

    file_size = path.stat().st_size
    if file_size <= 44:
        path.unlink(missing_ok=True)
        return None

    data_size = file_size - 44
    duration = data_size / (rate * channels * 2)  # int16 = 2 bytes

    if duration < 5.0:
        path.unlink(missing_ok=True)
        return None

    # Rewrite header in place
    with open(path, "r+b") as f:
        f.seek(0)
        # RIFF header
        f.write(b"RIFF")
        f.write(struct.pack("<I", 36 + data_size))
        f.write(b"WAVE")
        # fmt sub-chunk
        f.write(b"fmt ")
        f.write(struct.pack("<I", 16))
        f.write(struct.pack("<H", 1))  # PCM
        f.write(struct.pack("<H", channels))
        f.write(struct.pack("<I", rate))
        f.write(struct.pack("<I", rate * channels * 2))
        f.write(struct.pack("<H", channels * 2))
        f.write(struct.pack("<H", 16))
        # data sub-chunk
        f.write(b"data")
        f.write(struct.pack("<I", data_size))

    return duration


def cleanup_temp_files(meeting_dir: Path) -> None:
    """Delete temp WAV files in the meeting data directory."""
    for name in ("mic-temp.wav", "speaker-temp.wav"):
        p = meeting_dir / name
        p.unlink(missing_ok=True)

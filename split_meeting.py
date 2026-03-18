"""Split a meeting recording into multiple separate meetings.

Preserves original Windows file metadata (creation date, modification date)
so the temporal provenance of each recording is accurate.

Usage (from repo root):
    python scripts/whisper_sync/split_meeting.py <meeting_folder> <split_seconds> <name1> <name2> [<name3> ...]

Example — split a 90-minute recording at 30:00 and 60:00 into three meetings:
    python scripts/whisper_sync/split_meeting.py \\
        meetings/local-transcriptions/03/0317_1020_Open-Claw \\
        1800,3600 \\
        "phone-call-abhi" "group-architecture" "dinesh-migration"

Split points are in seconds (comma-separated). N split points produce N+1 meetings.
Names are positional — one per output meeting.

What it does:
  1. Copies the original recording.wav to each new folder (preserving OS metadata)
  2. Trims each copy to its portion only (in-place WAV rewrite)
  3. Restores original creation time and sets mtime = portion end time via Windows API
  4. Splits transcript.json by timestamp ranges (re-zeros timestamps per portion)
  5. Runs flatten.py on each split transcript for immediate readability
  6. Verifies each split folder has recording.wav, transcript.json, transcript-readable.txt
  7. Removes the original folder after verification passes
  8. Does NOT generate minutes — that's a separate step (Claude or pm-transcribe-recording)
"""

import ctypes
import ctypes.wintypes
import json
import os
import shutil
import sys
import wave
from datetime import datetime, timedelta
from pathlib import Path


# --- Windows file time manipulation ---

def _filetime_from_datetime(dt: datetime) -> ctypes.wintypes.FILETIME:
    """Convert a Python datetime to a Windows FILETIME struct."""
    # Windows epoch: 1601-01-01, Python epoch: 1970-01-01
    # Difference: 116444736000000000 100-nanosecond intervals
    EPOCH_DIFF = 116444736000000000
    timestamp = int(dt.timestamp() * 10_000_000) + EPOCH_DIFF
    ft = ctypes.wintypes.FILETIME()
    ft.dwLowDateTime = timestamp & 0xFFFFFFFF
    ft.dwHighDateTime = (timestamp >> 32) & 0xFFFFFFFF
    return ft


def set_file_times(path: str, creation_time: datetime, modification_time: datetime):
    """Set both creation time and modification time on a Windows file."""
    # Open file handle with write attributes permission
    GENERIC_WRITE = 0x40000000
    FILE_SHARE_READ = 0x00000001
    OPEN_EXISTING = 3
    FILE_ATTRIBUTE_NORMAL = 0x80

    handle = ctypes.windll.kernel32.CreateFileW(
        str(path), GENERIC_WRITE, FILE_SHARE_READ, None,
        OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None
    )
    if handle == -1:
        raise OSError(f"Cannot open file for timestamp update: {path}")

    try:
        ctime_ft = _filetime_from_datetime(creation_time)
        mtime_ft = _filetime_from_datetime(modification_time)
        # SetFileTime(handle, lpCreationTime, lpLastAccessTime, lpLastWriteTime)
        success = ctypes.windll.kernel32.SetFileTime(
            handle,
            ctypes.byref(ctime_ft),
            None,  # don't change access time
            ctypes.byref(mtime_ft),
        )
        if not success:
            raise OSError(f"SetFileTime failed for: {path}")
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


# --- WAV operations ---

def get_wav_info(wav_path: str) -> dict:
    """Get WAV metadata: sample rate, channels, sample width, total frames, duration."""
    with wave.open(wav_path, "rb") as w:
        return {
            "rate": w.getframerate(),
            "channels": w.getnchannels(),
            "sampwidth": w.getsampwidth(),
            "frames": w.getnframes(),
            "duration": w.getnframes() / w.getframerate(),
            "params": w.getparams(),
        }


def trim_wav_inplace(wav_path: str, start_sec: float, end_sec: float):
    """Rewrite a WAV file to contain only the audio between start_sec and end_sec."""
    with wave.open(wav_path, "rb") as w:
        rate = w.getframerate()
        params = w.getparams()
        start_frame = int(start_sec * rate)
        end_frame = int(end_sec * rate)
        end_frame = min(end_frame, w.getnframes())

        w.setpos(start_frame)
        data = w.readframes(end_frame - start_frame)

    with wave.open(wav_path, "wb") as w:
        w.setparams(params)
        w.writeframes(data)


# --- Transcript operations ---

def split_transcript(transcript: dict, start_sec: float, end_sec: float) -> dict:
    """Extract segments within [start_sec, end_sec) and re-zero timestamps."""
    segments = []
    for s in transcript.get("segments", []):
        if s["start"] >= start_sec and s["end"] <= end_sec:
            ns = dict(s)
            ns["start"] -= start_sec
            ns["end"] -= start_sec
            if "words" in ns:
                ns["words"] = [
                    dict(w, start=w["start"] - start_sec, end=w["end"] - start_sec)
                    for w in ns["words"]
                ]
            segments.append(ns)

    word_segments = []
    for w in transcript.get("word_segments", []):
        if w.get("start", 0) >= start_sec and w.get("end", 0) <= end_sec:
            nw = dict(w)
            nw["start"] -= start_sec
            nw["end"] -= start_sec
            word_segments.append(nw)

    result = {"segments": segments, "word_segments": word_segments}
    # Preserve speaker_map if present
    if "speaker_map" in transcript:
        result["speaker_map"] = transcript["speaker_map"]
    return result


# --- Main split logic ---

def split_meeting(source_folder: Path, split_points: list[float], names: list[str]):
    """Split a meeting folder into multiple meetings.

    Args:
        source_folder: Path to the meeting folder (must contain recording.wav + transcript.json)
        split_points: List of split points in seconds (sorted ascending)
        names: List of names for output folders (len = len(split_points) + 1)
    """
    wav_path = source_folder / "recording.wav"
    json_path = source_folder / "transcript.json"

    if not wav_path.exists():
        raise FileNotFoundError(f"No recording.wav in {source_folder}")
    if not json_path.exists():
        raise FileNotFoundError(f"No transcript.json in {source_folder}")

    # Get original file metadata
    wav_info = get_wav_info(str(wav_path))
    total_duration = wav_info["duration"]
    original_ctime = datetime.fromtimestamp(os.path.getctime(str(wav_path)))
    original_mtime = datetime.fromtimestamp(os.path.getmtime(str(wav_path)))
    # Estimate original recording start time
    original_start = original_mtime - timedelta(seconds=total_duration)

    # Load transcript
    with open(json_path) as f:
        transcript = json.load(f)

    # Build time ranges: [0, split1], [split1, split2], ..., [splitN, total_duration]
    boundaries = [0.0] + sorted(split_points) + [total_duration]
    ranges = [(boundaries[i], boundaries[i + 1]) for i in range(len(boundaries) - 1)]

    if len(names) != len(ranges):
        raise ValueError(
            f"Expected {len(ranges)} names for {len(split_points)} split points, "
            f"got {len(names)}"
        )

    # Determine output parent (same month folder as source)
    parent = source_folder.parent

    print(f"Source: {source_folder.name} ({total_duration / 60:.1f} min)")
    print(f"Original recorded: {original_start.strftime('%Y-%m-%d %H:%M')} - {original_mtime.strftime('%H:%M')}")
    print(f"Splitting into {len(ranges)} meetings:")

    # Import flatten — works both as module (-m whisper_sync.split_meeting) and direct script
    try:
        from .flatten import flatten as flatten_transcript
    except ImportError:
        from pathlib import Path as _P
        import importlib.util
        _spec = importlib.util.spec_from_file_location("flatten", _P(__file__).parent / "flatten.py")
        _mod = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        flatten_transcript = _mod.flatten

    for i, ((start_sec, end_sec), name) in enumerate(zip(ranges, names)):
        portion_start = original_start + timedelta(seconds=start_sec)
        portion_end = original_start + timedelta(seconds=end_sec)
        duration = end_sec - start_sec

        # Build folder name: MMDD_HHMM_name inside MM-wN week folder
        week_dir = f"{portion_start.strftime('%m')}-w{(portion_start.day - 1) // 7 + 1}"
        folder_name = f"{portion_start.strftime('%m%d_%H%M')}_{name}"
        dest_folder = parent.parent / week_dir / folder_name

        print(f"\n  [{i + 1}] {folder_name}")
        print(f"      {portion_start.strftime('%H:%M')} - {portion_end.strftime('%H:%M')} ({duration / 60:.1f} min)")

        # Create folder
        dest_folder.mkdir(parents=True, exist_ok=True)

        # Copy original WAV (preserves some OS metadata as baseline)
        dest_wav = dest_folder / "recording.wav"
        shutil.copy2(str(wav_path), str(dest_wav))

        # Trim to this portion
        trim_wav_inplace(str(dest_wav), start_sec, end_sec)

        # Set correct Windows file times:
        #   creation time = original file creation time (when recording started)
        #   modification time = portion end time (so mtime - wav_duration = portion start)
        set_file_times(str(dest_wav), original_ctime, portion_end)
        print(f"      WAV: {os.path.getsize(str(dest_wav)) / 1024 / 1024:.1f} MB, "
              f"ctime preserved, mtime={portion_end.strftime('%H:%M')}")

        # Split transcript
        portion_transcript = split_transcript(transcript, start_sec, end_sec)
        dest_json = dest_folder / "transcript.json"
        with open(dest_json, "w") as f:
            json.dump(portion_transcript, f, indent=2)
        print(f"      Transcript: {len(portion_transcript['segments'])} segments")

        # Flatten
        try:
            readable = flatten_transcript(str(dest_json))
            print(f"      Flattened: {readable}")
        except Exception as e:
            print(f"      Flatten failed (non-fatal): {e}")

    # --- Verify all splits ---
    print("\nVerifying splits...")
    required_files = ["recording.wav", "transcript.json", "transcript-readable.txt"]
    all_ok = True
    created_folders = []
    for i, ((start_sec, end_sec), name) in enumerate(zip(ranges, names)):
        portion_start = original_start + timedelta(seconds=start_sec)
        week_dir = f"{portion_start.strftime('%m')}-w{(portion_start.day - 1) // 7 + 1}"
        folder_name = f"{portion_start.strftime('%m%d_%H%M')}_{name}"
        dest_folder = parent.parent / week_dir / folder_name
        created_folders.append(dest_folder)

        missing = [f for f in required_files if not (dest_folder / f).exists()]
        if missing:
            print(f"  FAIL [{i + 1}] {folder_name}: missing {missing}")
            all_ok = False
        else:
            # Verify WAV is readable and has correct duration
            try:
                info = get_wav_info(str(dest_folder / "recording.wav"))
                expected_dur = end_sec - start_sec
                if abs(info["duration"] - expected_dur) > 2:  # 2-second tolerance
                    print(f"  WARN [{i + 1}] {folder_name}: WAV duration {info['duration']:.0f}s "
                          f"vs expected {expected_dur:.0f}s")
                else:
                    print(f"  OK   [{i + 1}] {folder_name} ({info['duration'] / 60:.1f} min)")
            except Exception as e:
                print(f"  FAIL [{i + 1}] {folder_name}: WAV unreadable: {e}")
                all_ok = False

    if not all_ok:
        print("\nVerification FAILED — original folder preserved for recovery.")
        print(f"Original: {source_folder}")
        return

    # --- Remove original folder ---
    import shutil as _shutil
    _shutil.rmtree(str(source_folder))
    print(f"\nOriginal folder removed: {source_folder}")

    # Rebuild INDEX.md files
    try:
        try:
            from .rebuild_index import rebuild_root_index
        except ImportError:
            from pathlib import Path as _P
            import importlib.util
            _spec = importlib.util.spec_from_file_location("rebuild_index", _P(__file__).parent / "rebuild_index.py")
            _mod = importlib.util.module_from_spec(_spec)
            _spec.loader.exec_module(_mod)
            rebuild_root_index = _mod.rebuild_root_index
        rebuild_root_index(source_folder.parent.parent)
        print("INDEX.md files rebuilt.")
    except Exception as e:
        print(f"Index rebuild failed (non-fatal): {e}")

    print("Minutes (minutes.md) must be generated separately for each split.")


def main():
    if len(sys.argv) < 4:
        print(__doc__)
        sys.exit(1)

    folder = Path(sys.argv[1])
    split_points = [float(x) for x in sys.argv[2].split(",")]
    names = sys.argv[3:]

    if len(names) != len(split_points) + 1:
        print(f"Error: {len(split_points)} split points require {len(split_points) + 1} names, got {len(names)}")
        sys.exit(1)

    split_meeting(folder, split_points, names)


if __name__ == "__main__":
    main()

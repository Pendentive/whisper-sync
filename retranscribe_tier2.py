"""Re-transcribe a recording using forced Tier 2 (RMS-balanced mono + PyAnnote).

Usage:
    python retranscribe_tier2.py <path_to_recording.wav> [--output <path>]

Saves transcript-tier2.json alongside the original recording if no --output given.
"""

import argparse
import json
import sys
import os

# Add whisper_sync package to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main():
    parser = argparse.ArgumentParser(description="Force Tier 2 re-transcription")
    parser.add_argument("audio", help="Path to recording.wav")
    parser.add_argument("--output", help="Output JSON path (default: transcript-tier2.json next to input)")
    args = parser.parse_args()

    audio_path = os.path.normpath(args.audio)
    if not os.path.exists(audio_path):
        print(f"Error: {audio_path} not found")
        sys.exit(1)

    output_path = args.output or os.path.join(
        os.path.dirname(audio_path), "transcript-tier2.json"
    )

    print(f"Audio: {audio_path}")
    print(f"Output: {output_path}")
    print()

    from whisper_sync import transcribe

    # Step 1: Create RMS-balanced mono
    print("[1/5] Creating RMS-balanced mono mix...")
    balanced_path = transcribe._create_balanced_mono(audio_path)
    if balanced_path:
        print(f"  Balanced mono saved to temp: {balanced_path}")
    else:
        print("  No balancing needed (mono input or identical channels), using original")

    diarize_path = balanced_path or audio_path

    try:
        # Step 2: Prepare (load models + audio). Transcription and
        # alignment run on the ORIGINAL recording, matching the main
        # pipeline; the balanced mono is only a diarization input.
        print("[2/5] Loading models and preparing audio...")
        ctx = transcribe.stage_prepare(audio_path)

        # Step 3: Transcribe
        print("[3/5] Transcribing (this is the longest step)...")
        result = transcribe.stage_transcribe(ctx)

        # Step 4: Align
        print("[4/5] Aligning word-level timestamps...")
        result = transcribe.stage_align(ctx, result)

        # Step 5: Diarize with PyAnnote on the balanced mono
        print("[5/5] Running PyAnnote diarization on balanced mono...")
        with transcribe._lock:
            pipeline = transcribe._load_diarize_pipeline()
        diarize_result = pipeline(diarize_path)

        # Assign speakers
        import whisperx
        result = whisperx.assign_word_speakers(diarize_result, result)
    finally:
        # Clean up temp balanced file even when a stage raises
        if balanced_path and os.path.exists(balanced_path):
            os.unlink(balanced_path)
            print("  Cleaned up temp file")

    # Save
    with open(output_path, "w") as f:
        json.dump(result, f, indent=2, default=str)
    print(f"\nDone! Saved to: {output_path}")

    # Quick summary
    from collections import Counter
    segments = result.get("segments", [])
    speaker_counts = Counter(seg.get("speaker", "UNKNOWN") for seg in segments)
    print(f"\nSpeakers found: {len(speaker_counts)}")
    for spk, count in sorted(speaker_counts.items()):
        words = sum(len(seg.get("text", "").split()) for seg in segments if seg.get("speaker") == spk)
        print(f"  {spk}: {count} segments, ~{words} words")


if __name__ == "__main__":
    main()

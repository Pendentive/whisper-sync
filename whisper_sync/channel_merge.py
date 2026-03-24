"""Per-channel transcription and diarization with confidence fusion.

For stereo recordings (mic + loopback), runs the full transcription
pipeline on each channel independently, then merges results using
energy ratio, timestamp overlap, and text similarity.
"""

import os
import wave
import tempfile
import numpy as np
from difflib import SequenceMatcher
from .logger import logger


# Tunable parameters
ENERGY_THRESHOLD = 2.0       # ratio for dominant channel classification
OVERLAP_THRESHOLD = 0.5      # seconds of overlap for "same utterance"
SIMILARITY_THRESHOLD = 0.6   # text similarity for cross-channel match
DEDUP_OVERLAP_RATIO = 0.7    # fraction overlap to consider duplicate
MIN_CONFIDENCE = 0.3         # below this, fall back to balanced mono
MIN_WORDS_PER_MINUTE = 20    # below this, too much content lost


def is_stereo(audio_path: str) -> bool:
    """Check if audio file is stereo."""
    with wave.open(audio_path, "rb") as wf:
        return wf.getnchannels() >= 2


def split_channels(audio_path: str) -> tuple[str, str]:
    """Split stereo WAV into two temp mono WAVs. Returns (ch0_path, ch1_path)."""
    with wave.open(audio_path, "rb") as wf:
        sr = wf.getframerate()
        sw = wf.getsampwidth()
        raw = wf.readframes(wf.getnframes())

    samples = np.frombuffer(raw, dtype=np.int16)
    ch0 = samples[0::2]
    ch1 = samples[1::2]

    paths = []
    for ch_data in (ch0, ch1):
        fd, tmp = tempfile.mkstemp(suffix=".wav")
        with wave.open(tmp, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(sw)
            wf.setframerate(sr)
            wf.writeframes(ch_data.tobytes())
        os.close(fd)
        paths.append(tmp)

    return paths[0], paths[1]


def load_channel_audio(audio_path: str) -> tuple[np.ndarray, np.ndarray, int]:
    """Load stereo WAV and return (ch0_float, ch1_float, sample_rate)."""
    with wave.open(audio_path, "rb") as wf:
        sr = wf.getframerate()
        raw = wf.readframes(wf.getnframes())
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float64)
    return samples[0::2], samples[1::2], sr


def compute_energy_ratio(ch0_audio, ch1_audio, start, end, sr):
    """Compute energy ratio between channels for a time range."""
    s = int(start * sr)
    e = int(end * sr)
    ch0_slice = ch0_audio[s:e]
    ch1_slice = ch1_audio[s:e]

    ch0_rms = np.sqrt(np.mean(ch0_slice ** 2)) + 1e-10 if len(ch0_slice) > 0 else 1e-10
    ch1_rms = np.sqrt(np.mean(ch1_slice ** 2)) + 1e-10 if len(ch1_slice) > 0 else 1e-10

    ratio = ch0_rms / ch1_rms

    if ratio > ENERGY_THRESHOLD:
        dominant = "local"
    elif ratio < 1.0 / ENERGY_THRESHOLD:
        dominant = "remote"
    else:
        dominant = "ambiguous"

    return ch0_rms, ch1_rms, ratio, dominant


def text_similarity(text_a, text_b):
    """Normalized text similarity."""
    if not text_a or not text_b:
        return 0.0
    return SequenceMatcher(None, text_a.lower().strip(), text_b.lower().strip()).ratio()


def tag_segments(segments, source_channel, ch0_audio, ch1_audio, sr):
    """Tag each segment with energy-based origin and source channel."""
    tagged = []
    for seg in segments:
        start = seg.get("start", 0)
        end = seg.get("end", 0)
        ch0_rms, ch1_rms, ratio, dominant = compute_energy_ratio(
            ch0_audio, ch1_audio, start, end, sr
        )
        tagged.append({
            **seg,
            "source_channel": source_channel,
            "energy_ratio": ratio,
            "origin": dominant,
            "confidence": 0.5,  # base confidence
            "ch0_rms": ch0_rms,
            "ch1_rms": ch1_rms,
            "is_bleed": False,
            "cross_channel_confirmed": False,
        })
    return tagged


def apply_cross_channel_confidence(tagged_segments):
    """Boost confidence when same speech appears on both channels (bleed = confirmation)."""
    for i, seg_a in enumerate(tagged_segments):
        for j, seg_b in enumerate(tagged_segments):
            if i >= j or seg_a["source_channel"] == seg_b["source_channel"]:
                continue
            if seg_a.get("is_bleed") or seg_b.get("is_bleed"):
                continue

            # Check timestamp overlap
            overlap_start = max(seg_a["start"], seg_b["start"])
            overlap_end = min(seg_a["end"], seg_b["end"])
            overlap = max(0, overlap_end - overlap_start)
            if overlap < OVERLAP_THRESHOLD:
                continue

            # Check text similarity
            sim = text_similarity(seg_a.get("text", ""), seg_b.get("text", ""))
            if sim < SIMILARITY_THRESHOLD:
                continue

            # Same speech on both channels. The dominant channel's version wins.
            if seg_a["origin"] == "local" and seg_a["source_channel"] == 0:
                seg_a["confidence"] += 0.3
                seg_a["cross_channel_confirmed"] = True
                seg_b["is_bleed"] = True
            elif seg_b["origin"] == "remote" and seg_b["source_channel"] == 1:
                seg_b["confidence"] += 0.3
                seg_b["cross_channel_confirmed"] = True
                seg_a["is_bleed"] = True
            elif seg_a["origin"] == "remote" and seg_a["source_channel"] == 1:
                seg_a["confidence"] += 0.3
                seg_a["cross_channel_confirmed"] = True
                seg_b["is_bleed"] = True
            elif seg_b["origin"] == "local" and seg_b["source_channel"] == 0:
                seg_b["confidence"] += 0.3
                seg_b["cross_channel_confirmed"] = True
                seg_a["is_bleed"] = True
            else:
                # Ambiguous, mild boost to channel-appropriate side
                if seg_a["source_channel"] == 0 and seg_a["energy_ratio"] > 1:
                    seg_a["confidence"] += 0.1
                    seg_b["is_bleed"] = True
                elif seg_b["source_channel"] == 1 and seg_b["energy_ratio"] < 1:
                    seg_b["confidence"] += 0.1
                    seg_a["is_bleed"] = True

    return tagged_segments


def deduplicate_segments(tagged_segments):
    """Remove bleed segments and deduplicate overlapping content."""
    # Remove explicit bleed
    non_bleed = [s for s in tagged_segments if not s.get("is_bleed", False)]

    # For remaining overlaps from different channels, keep higher confidence
    result = []
    for seg in non_bleed:
        dominated = False
        for existing in result:
            if existing["source_channel"] == seg["source_channel"]:
                continue
            overlap_start = max(seg["start"], existing["start"])
            overlap_end = min(seg["end"], existing["end"])
            overlap = max(0, overlap_end - overlap_start)
            seg_duration = seg["end"] - seg["start"]
            if seg_duration > 0 and overlap / seg_duration > DEDUP_OVERLAP_RATIO:
                if seg["confidence"] <= existing["confidence"]:
                    dominated = True
                    break
        if not dominated:
            result.append(seg)

    result.sort(key=lambda s: s["start"])
    return result


def unify_speaker_labels(segments):
    """Map per-channel speaker labels to unified SPEAKER_NN namespace."""
    # Collect unique speakers per origin
    local_speakers = set()
    remote_speakers = set()

    for seg in segments:
        spk = seg.get("speaker", "UNKNOWN")
        if seg["origin"] == "local" or (seg["origin"] == "ambiguous" and seg["source_channel"] == 0):
            local_speakers.add(spk)
        else:
            remote_speakers.add(spk)

    # Build label map
    label_map = {}
    for i, spk in enumerate(sorted(local_speakers)):
        label_map[("local", spk)] = f"SPEAKER_{i:02d}"

    offset = len(local_speakers)
    for i, spk in enumerate(sorted(remote_speakers)):
        label_map[("remote", spk)] = f"SPEAKER_{offset + i:02d}"

    # Apply
    for seg in segments:
        spk = seg.get("speaker", "UNKNOWN")
        origin = seg["origin"]
        if origin == "ambiguous":
            origin = "local" if seg["source_channel"] == 0 else "remote"
        key = (origin, spk)
        seg["speaker"] = label_map.get(key, seg["speaker"])

    return segments


def compute_final_confidence(segments):
    """Compute composite confidence score for each segment."""
    for seg in segments:
        score = seg.get("confidence", 0.5)

        ratio = seg.get("energy_ratio", 1.0)
        if ratio > 3.0 or ratio < 0.33:
            score += 0.2
        elif ratio > 2.0 or ratio < 0.5:
            score += 0.1

        # Word-level confidence from WhisperX
        words = seg.get("words", [])
        if words:
            word_scores = [w.get("score", 0.5) for w in words if "score" in w]
            if word_scores:
                score += (np.mean(word_scores) - 0.5) * 0.2

        seg["confidence"] = min(1.0, max(0.0, score))

    return segments


def merge_channel_results(segments_ch0, segments_ch1, ch0_audio, ch1_audio, sr, duration):
    """Full merge pipeline: tag, boost confidence, deduplicate, unify labels."""
    logger.info("Merging per-channel results...")

    tagged_ch0 = tag_segments(segments_ch0, 0, ch0_audio, ch1_audio, sr)
    tagged_ch1 = tag_segments(segments_ch1, 1, ch0_audio, ch1_audio, sr)

    all_tagged = tagged_ch0 + tagged_ch1
    all_tagged.sort(key=lambda s: s["start"])

    # Cross-channel confidence boosting
    all_tagged = apply_cross_channel_confidence(all_tagged)

    # Deduplicate
    merged = deduplicate_segments(all_tagged)

    # Compute final confidence
    merged = compute_final_confidence(merged)

    # Unify speaker labels
    merged = unify_speaker_labels(merged)

    # Quality check
    total_words = sum(len(s.get("text", "").split()) for s in merged)
    duration_min = max(duration / 60, 0.1)
    wpm = total_words / duration_min
    avg_conf = np.mean([s["confidence"] for s in merged]) if merged else 0

    n_speakers = len(set(s.get("speaker") for s in merged))
    logger.info(f"Per-channel merge: {n_speakers} speakers, {len(merged)} segments, "
                f"avg confidence {avg_conf:.2f}, {wpm:.0f} words/min")

    quality_ok = avg_conf >= MIN_CONFIDENCE and wpm >= MIN_WORDS_PER_MINUTE and len(merged) > 0

    # Strip internal metadata before returning
    for seg in merged:
        for key in ("source_channel", "energy_ratio", "origin", "ch0_rms", "ch1_rms",
                     "is_bleed", "cross_channel_confirmed"):
            seg.pop(key, None)

    return merged, quality_ok

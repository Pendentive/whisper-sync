"""Audio capture - mic and system audio (WASAPI loopback on Windows)."""

import threading
import wave
from math import gcd
from pathlib import Path

import numpy as np
import sounddevice as sd
from scipy.signal import resample_poly

try:
    import pyaudiowpatch as pyaudio
except ImportError:
    pyaudio = None

from .logger import logger
from .streaming_wav import StreamingWavWriter


def get_host_apis() -> list[dict]:
    """Return list of available host APIs with index and name."""
    return [{"id": i, "name": h["name"]} for i, h in enumerate(sd.query_hostapis())]


def list_devices(api_filter: str | None = "WASAPI") -> dict:
    """List audio devices, optionally filtered to a specific host API."""
    devices = sd.query_devices()
    hostapis = sd.query_hostapis()
    filter_idx = None
    if api_filter:
        filter_idx = next((i for i, h in enumerate(hostapis) if api_filter in h["name"]), None)
    inputs, outputs = [], []
    for i, d in enumerate(devices):
        if filter_idx is not None and d["hostapi"] != filter_idx:
            continue
        if d["max_input_channels"] > 0:
            inputs.append({"id": i, "name": d["name"], "channels": d["max_input_channels"]})
        if d["max_output_channels"] > 0:
            outputs.append({"id": i, "name": d["name"], "channels": d["max_output_channels"]})
    return {"inputs": inputs, "outputs": outputs}


def get_default_devices(api_filter: str | None = "WASAPI") -> dict:
    """Return default input/output device IDs for the given API (or global defaults)."""
    hostapis = sd.query_hostapis()
    if api_filter:
        idx = next((i for i, h in enumerate(hostapis) if api_filter in h["name"]), None)
        if idx is not None:
            api = hostapis[idx]
            return {"input": api["default_input_device"], "output": api["default_output_device"]}
    defaults = sd.default.device
    return {"input": defaults[0], "output": defaults[1]}


# Cache of (device_key, target_samplerate, target_dtype, channels) -> (effective_rate, effective_dtype)
# Lets us skip the probe-and-fail sequence on subsequent opens for the same
# device that already rejected the target rate once. On Windows this avoids
# logging "mic open attempt failed (rate=16000 ...)" + "16000 Hz float32 rejected"
# on every dictation/meeting start.
_MIC_FORMAT_CACHE: dict[tuple, tuple[int, str]] = {}
_MIC_FORMAT_CACHE_LOCK = threading.Lock()


def _open_input_stream(*, device, target_samplerate: int, channels: int,
                       dtype: str, callback):
    """Open ``sd.InputStream`` with a fallback ladder for format rejections.

    On Windows, ``device=None`` often resolves to an MME device that rejects
    ``float32 @ 16000 Hz`` with ``PaErrorCode -9999 / MME error 32`` even
    though the hardware supports the format under WASAPI. Without this
    retry, a single failed open propagates out of the hotkey handler and
    kills the keyboard dispatcher thread, bricking all further hotkeys.

    Retry ladder:
      1. Caller-requested ``target_samplerate`` at ``dtype``.
      2. Device's native ``default_samplerate`` at the same ``dtype``.
      3. Device's native ``default_samplerate`` at ``int16``.

    Returns ``(stream, effective_samplerate)``. Callers are responsible
    for resampling downstream audio when the effective rate differs from
    the target.

    Reraises the last ``PortAudioError`` if every attempt fails.

    A successful (rate, dtype) combination is cached per device so future
    opens skip the probe and avoid logging the same fallback every session.
    The cache key includes the resolved device name (not the raw ``device``
    arg) so that ``device=None`` callers don't get a stale entry if the OS
    default input device changes between calls.
    """
    # Resolve a stable device identity for the cache key. When the caller
    # passes device=None, sd.query_devices(None) returns info about the
    # current default device; including its name in the key means the
    # cache automatically invalidates when the user swaps default mic.
    try:
        device_info = sd.query_devices(device)
        device_name = device_info.get("name", "")
    except Exception:
        device_info = None
        device_name = ""
    cache_key = (device_name, target_samplerate, dtype, channels)
    with _MIC_FORMAT_CACHE_LOCK:
        cached = _MIC_FORMAT_CACHE.get(cache_key)

    if cached is not None:
        cached_rate, cached_dtype = cached
        try:
            stream = sd.InputStream(
                samplerate=cached_rate,
                channels=channels,
                dtype=cached_dtype,
                device=device,
                callback=callback,
            )
            return stream, cached_rate
        except sd.PortAudioError:
            # Device state changed; fall through to full probe.
            with _MIC_FORMAT_CACHE_LOCK:
                _MIC_FORMAT_CACHE.pop(cache_key, None)

    attempts = [(target_samplerate, dtype)]
    try:
        # Reuse the device_info we already queried above, if available.
        info = device_info if device_info is not None else sd.query_devices(device)
        native = int(info["default_samplerate"])
    except Exception:
        native = None
    if native and native != target_samplerate:
        attempts.append((native, dtype))
        attempts.append((native, "int16"))
    else:
        attempts.append((target_samplerate, "int16"))

    last_err = None
    for rate, this_dtype in attempts:
        try:
            stream = sd.InputStream(
                samplerate=rate,
                channels=channels,
                dtype=this_dtype,
                device=device,
                callback=callback,
            )
            fell_back = (rate != target_samplerate or this_dtype != dtype)
            if fell_back:
                # Only log the fallback the FIRST time we discover it; cache
                # hits below skip this branch. Use INFO not WARNING because
                # the fallback succeeds and downstream code resamples.
                logger.info(
                    "mic: %d Hz %s rejected; using %d Hz %s "
                    "(caching for this device)",
                    target_samplerate, dtype, rate, this_dtype,
                )
            with _MIC_FORMAT_CACHE_LOCK:
                _MIC_FORMAT_CACHE[cache_key] = (rate, this_dtype)
            return stream, rate
        except sd.PortAudioError as e:
            last_err = e
            logger.debug("mic open attempt failed (rate=%s dtype=%s): %s", rate, this_dtype, e)
    assert last_err is not None
    raise last_err


class AudioRecorder:
    def __init__(self, sample_rate: int = 16000):
        self.sample_rate = sample_rate
        # Effective mic samplerate for the current session. Usually matches
        # ``sample_rate``, but ``_open_input_stream`` may fall back to the
        # device's native rate if the requested rate is rejected.
        self._mic_effective_rate: int = sample_rate
        # Precomputed resample ratio for the current session. Set in
        # ``start()`` alongside the stream so the audio callback never
        # recomputes it per buffer.
        self._mic_resample_up: int = 1
        self._mic_resample_down: int = 1
        self._mic_data: list[np.ndarray] = []
        self._speaker_data: list[np.ndarray] = []
        self._mic_stream = None
        self._speaker_stream = None
        self._recording = False
        self._lock = threading.Lock()
        self._disk_only = False
        self._mic_writer: StreamingWavWriter | None = None
        self._speaker_writer: StreamingWavWriter | None = None
        # PyAudioWPatch loopback state
        self._pyaudio = None
        self._speaker_pa_stream = None
        self._speaker_native_rate: int | None = None
        # Block buffer for in-callback speaker resampling (disk streaming).
        # Chunks accumulate to ~1s blocks before each resample+write so the
        # per-callback cost stays trivial and FIR edge effects are bounded
        # to block boundaries (inaudible for ASR).
        self._speaker_block: list[np.ndarray] = []
        self._speaker_block_frames = 0

    def _mic_callback(self, indata, frames, time_info, status):
        try:
            if not self._recording:
                return

            # Normalize to float32 in [-1, 1] regardless of what dtype the
            # device actually produced. Integer samples must be scaled
            # BEFORE resampling, otherwise downstream audio stays in the
            # ~[-32768, 32767] range and clips catastrophically when
            # written to disk or fed to the transcriber.
            if indata.dtype == np.int16:
                normalized = indata.astype(np.float32) / 32768.0
            elif indata.dtype != np.float32:
                normalized = indata.astype(np.float32)
            else:
                normalized = indata

            # Resample to the canonical sample_rate only when the device
            # fell back to its native rate. up/down are precomputed in
            # start() so the callback is a single resample_poly call.
            if self._mic_resample_up != self._mic_resample_down:
                # Mono view; reshape(-1) avoids the copy flatten() would do.
                mono = normalized.reshape(-1)
                resampled = resample_poly(mono, self._mic_resample_up, self._mic_resample_down)
                data = resampled.reshape(-1, 1).astype(np.float32, copy=False)
            else:
                data = normalized

            if not self._disk_only:
                # Always copy into the accumulator - PortAudio reuses the
                # indata buffer for the next callback, and normalized may
                # alias indata on the float32 no-op path.
                self._mic_data.append(data.copy() if data is indata else data)
            if self._mic_writer is not None:
                self._mic_writer.write(data)
        except Exception as e:
            # Never let exceptions escape into PortAudio's C thread
            if not getattr(self, "_mic_error_logged", False):
                self._mic_error_logged = True
                logger.warning(f"mic callback error (suppressed): {e}")

    def _speaker_callback(self, indata, frames, time_info, status):
        try:
            if self._recording:
                self._speaker_data.append(indata.copy())
                if self._speaker_writer is not None:
                    self._speaker_writer.write(indata)
        except Exception as e:
            if not getattr(self, "_speaker_error_logged", False):
                self._speaker_error_logged = True
                logger.warning(f"speaker callback error (suppressed): {e}")

    def start(self, mic_device: int | None = None, speaker_device: int | None = None):
        with self._lock:
            self._mic_data = []
            self._speaker_data = []
            self._mic_error_logged = False
            self._speaker_error_logged = False

            # Transactional: the recorder only becomes "recording" AFTER
            # the mic stream is fully open and started. On any failure we
            # close a partially created stream and leave ``_recording``
            # False so stop()/subsequent starts see a clean state.
            stream = None
            try:
                stream, effective_rate = _open_input_stream(
                    device=mic_device,
                    target_samplerate=self.sample_rate,
                    channels=1,
                    dtype="float32",
                    callback=self._mic_callback,
                )
                stream.start()
            except Exception:
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        pass
                raise

            self._mic_stream = stream
            self._mic_effective_rate = effective_rate
            # Precompute the resample ratio once per session.
            if effective_rate != self.sample_rate:
                g = gcd(self.sample_rate, effective_rate)
                self._mic_resample_up = self.sample_rate // g
                self._mic_resample_down = effective_rate // g
            else:
                self._mic_resample_up = 1
                self._mic_resample_down = 1
            self._recording = True

            if speaker_device is not None:
                self._start_speaker_loopback()

    def _start_speaker_loopback(self):
        """Start WASAPI loopback capture via PyAudioWPatch."""
        if pyaudio is None:
            logger.warning("pyaudiowpatch not installed -- speaker loopback disabled")
            return
        try:
            p = pyaudio.PyAudio()
            self._pyaudio = p
            wasapi_info = p.get_host_api_info_by_type(pyaudio.paWASAPI)
            default_output = p.get_device_info_by_index(wasapi_info["defaultOutputDevice"])

            # Find the loopback device matching the default output
            loopback_device = None
            for i in range(p.get_device_count()):
                dev = p.get_device_info_by_index(i)
                if (dev.get("isLoopbackDevice", False)
                        and dev["maxInputChannels"] > 0
                        and default_output["name"] in dev["name"]):
                    loopback_device = dev
                    break

            if loopback_device is None:
                raise RuntimeError("No WASAPI loopback device found for default output")

            native_rate = int(loopback_device["defaultSampleRate"])
            native_channels = max(loopback_device["maxInputChannels"], 1)
            self._speaker_native_rate = native_rate

            def _pa_callback(in_data, frame_count, time_info, status):
                try:
                    if self._recording:
                        audio = np.frombuffer(in_data, dtype=np.float32)
                        # Downmix to mono if multi-channel
                        if native_channels > 1:
                            audio = audio.reshape(-1, native_channels).mean(axis=1)
                        mono = audio.reshape(-1, 1)
                        self._ingest_speaker_chunk(mono)
                except Exception:
                    pass
                return (None, pyaudio.paContinue)

            self._speaker_pa_stream = p.open(
                format=pyaudio.paFloat32,
                channels=native_channels,
                rate=native_rate,
                input=True,
                input_device_index=loopback_device["index"],
                stream_callback=_pa_callback,
                frames_per_buffer=1024,
            )
            self._speaker_pa_stream.start_stream()
            logger.info(f"Speaker loopback: {loopback_device['name']} @ {native_rate} Hz")
        except Exception as e:
            logger.warning(f"Speaker loopback failed: {e}")
            self._close_pyaudio()

    def _ingest_speaker_chunk(self, mono: np.ndarray) -> None:
        """Route a mono float32 speaker chunk to disk or RAM.

        When a speaker writer is open (meeting disk-streaming mode), chunks
        accumulate into ~1 second blocks, are resampled to the target rate,
        and stream to disk — RAM stays flat for the whole meeting. Without
        a writer (legacy/RAM mode), chunks accumulate in RAM as before.

        Historically the speaker channel was ALWAYS RAM-accumulated at the
        device's native rate: 48 kHz x 4 bytes ~= 691 MB/hour held for the
        entire meeting. The mic channel got disk streaming; this gives the
        speaker channel the same treatment.
        """
        if self._speaker_writer is None:
            self._speaker_data.append(mono.copy())
            return

        # Loopback capture starts in start() BEFORE start_streaming() opens
        # the writer, so the first chunks land in _speaker_data. Migrate
        # that backlog into the block buffer here (we ARE the callback
        # thread — single consumer, no race) so meeting-start audio is not
        # silently dropped when stop() returns only speaker_path.
        self._migrate_speaker_backlog()

        self._speaker_block.append(mono.copy())
        self._speaker_block_frames += len(mono)
        native = self._speaker_native_rate or self.sample_rate
        if self._speaker_block_frames >= native:  # ~1 second buffered
            self._flush_speaker_block()

    def _migrate_speaker_backlog(self) -> None:
        """Move pre-writer RAM chunks into the disk-streaming block buffer.

        Chunks captured between loopback start and writer creation are at
        the same native rate the block buffer expects, so they prepend
        cleanly. No-op when there is no backlog.
        """
        if not self._speaker_data:
            return
        backlog, self._speaker_data = self._speaker_data, []
        # Prepend in order: backlog audio came first.
        self._speaker_block = backlog + self._speaker_block
        self._speaker_block_frames += sum(len(c) for c in backlog)

    def _flush_speaker_block(self) -> None:
        """Resample the buffered speaker block and write it to disk."""
        if not self._speaker_block or self._speaker_writer is None:
            self._speaker_block = []
            self._speaker_block_frames = 0
            return
        block = np.concatenate(self._speaker_block, axis=0).reshape(-1)
        self._speaker_block = []
        self._speaker_block_frames = 0
        native = self._speaker_native_rate or self.sample_rate
        if native != self.sample_rate:
            g = gcd(self.sample_rate, native)
            block = resample_poly(block, self.sample_rate // g, native // g)
        self._speaker_writer.write(
            block.astype(np.float32, copy=False).reshape(-1, 1)
        )

    def _close_pyaudio(self):
        """Clean up PyAudio loopback resources."""
        if self._speaker_pa_stream is not None:
            try:
                self._speaker_pa_stream.stop_stream()
                self._speaker_pa_stream.close()
            except Exception:
                pass
            self._speaker_pa_stream = None
        if self._pyaudio is not None:
            try:
                self._pyaudio.terminate()
            except Exception:
                pass
            self._pyaudio = None

    @property
    def speaker_loopback_active(self) -> bool:
        """Whether the speaker loopback stream is currently capturing."""
        return self._speaker_pa_stream is not None

    def stop(self) -> dict:
        with self._lock:
            self._recording = False
            if self._mic_stream:
                self._mic_stream.stop()
                self._mic_stream.close()
                self._mic_stream = None
            if self._speaker_stream:
                self._speaker_stream.stop()
                self._speaker_stream.close()
                self._speaker_stream = None
            self._close_pyaudio()

        result = {}
        if self._disk_only and self._mic_writer is not None:
            result["mic_path"] = self._mic_writer.path
        elif self._mic_data:
            result["mic"] = np.concatenate(self._mic_data, axis=0)

        if self._speaker_writer is not None:
            # Disk-streamed speaker channel: migrate any pre-writer backlog
            # (covers meetings so short that no callback ran after the
            # writer opened), flush the tail block, finalize the WAV
            # (already at target rate), hand back the path. The caller
            # reads it lazily; RAM stayed flat for the whole meeting.
            # Safe: _recording is already False and the PA stream closed,
            # so no callback can race these buffers.
            self._migrate_speaker_backlog()
            self._flush_speaker_block()
            self._speaker_writer.close()
            result["speaker_path"] = self._speaker_writer.path
            self._speaker_writer = None
        elif self._speaker_data:
            raw = np.concatenate(self._speaker_data, axis=0)
            # Resample from native rate to target sample rate if needed
            if self._speaker_native_rate and self._speaker_native_rate != self.sample_rate:
                up = self.sample_rate // gcd(self.sample_rate, self._speaker_native_rate)
                down = self._speaker_native_rate // gcd(self.sample_rate, self._speaker_native_rate)
                resampled = resample_poly(raw.flatten(), up, down)
                result["speaker"] = resampled.astype(np.float32).reshape(-1, 1)
            else:
                result["speaker"] = raw
        return result

    @property
    def is_recording(self) -> bool:
        return self._recording

    def start_streaming(self, mic_path, speaker_path=None, disk_only=False):
        """Open streaming WAV writers for crash safety during meeting recording.

        Args:
            mic_path: Path for mic WAV file.
            speaker_path: Optional explicit path for the speaker WAV file.
                Defaults to ``speaker-temp.wav`` next to ``mic_path`` when
                the loopback stream is active and ``disk_only`` is set.
            disk_only: If True, skip RAM accumulation -- audio lives only on disk.
                Use for long meetings to prevent MemoryError.

        Transactional: if ``StreamingWavWriter`` raises (e.g. unwritable
        disk), the ``_disk_only`` flag is NOT flipped, so the callback
        continues to accumulate into RAM and the meeting is still usable.
        """
        writer = StreamingWavWriter(mic_path, channels=1, rate=self.sample_rate)
        # Only commit state after the writer is successfully opened. The
        # old ordering flipped _disk_only first; a writer-open failure
        # then left the callback skipping BOTH RAM and disk and the
        # meeting silently captured nothing.
        self._mic_writer = writer
        self._disk_only = disk_only

        # Speaker channel: stream to disk too when in disk_only mode and
        # loopback is capturing. Historically the speaker channel was
        # ALWAYS RAM-accumulated at native rate (~691 MB/hour); see
        # _ingest_speaker_chunk. Failure here is non-fatal: the callback
        # falls back to RAM accumulation exactly as before.
        if disk_only and self.speaker_loopback_active:
            if speaker_path is None:
                speaker_path = Path(mic_path).parent / "speaker-temp.wav"
            try:
                self._speaker_block = []
                self._speaker_block_frames = 0
                self._speaker_writer = StreamingWavWriter(
                    speaker_path, channels=1, rate=self.sample_rate
                )
            except Exception as e:
                logger.warning(
                    "Speaker disk streaming unavailable (RAM fallback): %s", e
                )
                self._speaker_writer = None

    def stop_streaming(self):
        """Close and finalize streaming WAV writers."""
        if self._mic_writer is not None:
            self._mic_writer.close()
            self._mic_writer = None
        if self._speaker_writer is not None:
            self._speaker_writer.close()
            self._speaker_writer = None

    def discard_streaming(self):
        """Close writers and delete the temp files.

        Logs each discarded channel (path + size) so the forensic record
        shows exactly which recording was dropped when the user aborts
        the meeting-name dialog or the app decides to discard.
        """
        from .streaming_wav import cleanup_temp_files
        parent = None
        for label, w in (("mic", self._mic_writer), ("speaker", self._speaker_writer)):
            if w is None:
                continue
            parent = w.path.parent
            try:
                size = w.path.stat().st_size
            except OSError:
                size = 0
            logger.info(
                "discard_streaming: channel=%s path=%s size=%d",
                label, w.path, size,
            )
            try:
                w.close()
            except Exception:
                logger.debug("discard_streaming: close failed for %s", label, exc_info=True)
        self._mic_writer = None
        self._speaker_writer = None
        if parent is not None:
            cleanup_temp_files(parent)


def save_wav(filepath: str, data: np.ndarray, sample_rate: int = 16000):
    path = Path(filepath)
    path.parent.mkdir(parents=True, exist_ok=True)
    int_data = (data * 32767).astype(np.int16)
    with wave.open(str(path), "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(int_data.tobytes())


def _peak_normalize(arr: np.ndarray, target: float = 0.9) -> np.ndarray:
    """Normalize array so peak amplitude equals target. Prevents one channel from dominating the mix."""
    peak = np.max(np.abs(arr))
    if peak > 0:
        return arr / peak * target
    return arr


def save_stereo_wav(filepath: str, mic: np.ndarray, speaker: np.ndarray, sample_rate: int = 16000):
    path = Path(filepath)
    path.parent.mkdir(parents=True, exist_ok=True)
    max_len = max(len(mic.flatten()), len(speaker.flatten()))
    mic_padded = np.pad(mic.flatten(), (0, max_len - len(mic.flatten())))
    spk_padded = np.pad(speaker.flatten(), (0, max_len - len(speaker.flatten())))
    # Normalize each channel so neither dominates the mono mixdown
    mic_padded = _peak_normalize(mic_padded)
    spk_padded = _peak_normalize(spk_padded)
    stereo = np.column_stack([mic_padded, spk_padded])
    int_data = (stereo * 32767).astype(np.int16)
    with wave.open(str(path), "w") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(int_data.tobytes())

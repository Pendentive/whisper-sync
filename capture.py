"""Audio capture — mic and system audio (WASAPI loopback on Windows)."""

import threading
import wave
from pathlib import Path

import numpy as np
import sounddevice as sd

try:
    import pyaudiowpatch as pyaudio
except ImportError:
    pyaudio = None

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


class AudioRecorder:
    def __init__(self, sample_rate: int = 16000):
        self.sample_rate = sample_rate
        self._mic_data: list[np.ndarray] = []
        self._speaker_data: list[np.ndarray] = []
        self._mic_stream = None
        self._speaker_stream = None
        self._recording = False
        self._lock = threading.Lock()
        self._mic_writer: StreamingWavWriter | None = None
        self._speaker_writer: StreamingWavWriter | None = None
        # PyAudioWPatch loopback state
        self._pyaudio = None
        self._speaker_pa_stream = None
        self._speaker_native_rate: int | None = None

    def _mic_callback(self, indata, frames, time_info, status):
        try:
            if self._recording:
                self._mic_data.append(indata.copy())
                if self._mic_writer is not None:
                    self._mic_writer.write(indata)
        except Exception as e:
            # Never let exceptions escape into PortAudio's C thread
            if not getattr(self, "_mic_error_logged", False):
                self._mic_error_logged = True
                print(f"[WhisperSync] WARN: mic callback error (suppressed): {e}")

    def _speaker_callback(self, indata, frames, time_info, status):
        try:
            if self._recording:
                self._speaker_data.append(indata.copy())
                if self._speaker_writer is not None:
                    self._speaker_writer.write(indata)
        except Exception as e:
            if not getattr(self, "_speaker_error_logged", False):
                self._speaker_error_logged = True
                print(f"[WhisperSync] WARN: speaker callback error (suppressed): {e}")

    def start(self, mic_device: int | None = None, speaker_device: int | None = None):
        with self._lock:
            self._mic_data = []
            self._speaker_data = []
            self._mic_error_logged = False
            self._speaker_error_logged = False
            self._recording = True

            self._mic_stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype="float32",
                device=mic_device,
                callback=self._mic_callback,
            )
            self._mic_stream.start()

            if speaker_device is not None:
                self._start_speaker_loopback()

    def _start_speaker_loopback(self):
        """Start WASAPI loopback capture via PyAudioWPatch."""
        if pyaudio is None:
            print("[WhisperSync] WARN: pyaudiowpatch not installed — speaker loopback disabled")
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
                        self._speaker_data.append(mono.copy())
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
            print(f"[WhisperSync] Speaker loopback active: {loopback_device['name']} @ {native_rate} Hz")
        except Exception as e:
            print(f"[WhisperSync] WARN: speaker loopback failed: {e}")
            self._close_pyaudio()

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
        if self._mic_data:
            result["mic"] = np.concatenate(self._mic_data, axis=0)
        if self._speaker_data:
            raw = np.concatenate(self._speaker_data, axis=0)
            # Resample from native rate to target sample rate if needed
            if self._speaker_native_rate and self._speaker_native_rate != self.sample_rate:
                from math import gcd
                from scipy.signal import resample_poly
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

    def start_streaming(self, mic_path, speaker_path=None):
        """Open streaming WAV writers for crash safety during meeting recording.

        Only the mic stream gets crash-safe streaming writes. The speaker
        loopback data (captured at native rate via PyAudioWPatch) is held
        in memory and resampled on stop. On crash, the mic recording is
        preserved — that alone produces usable transcriptions.
        """
        self._mic_writer = StreamingWavWriter(mic_path, channels=1, rate=self.sample_rate)

    def stop_streaming(self):
        """Close and finalize streaming WAV writers."""
        if self._mic_writer is not None:
            self._mic_writer.close()
            self._mic_writer = None
        if self._speaker_writer is not None:
            self._speaker_writer.close()
            self._speaker_writer = None

    def discard_streaming(self):
        """Close writers and delete the temp files."""
        from .streaming_wav import cleanup_temp_files
        parent = None
        for w in (self._mic_writer, self._speaker_writer):
            if w is not None:
                parent = w.path.parent
                try:
                    w.close()
                except Exception:
                    pass
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

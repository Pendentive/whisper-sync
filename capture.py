"""Audio capture — mic and system audio (WASAPI loopback on Windows)."""

import threading
import wave
from pathlib import Path

import numpy as np
import sounddevice as sd
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

    def _mic_callback(self, indata, frames, time_info, status):
        if self._recording:
            self._mic_data.append(indata.copy())
            if self._mic_writer is not None:
                self._mic_writer.write(indata)

    def _speaker_callback(self, indata, frames, time_info, status):
        if self._recording:
            self._speaker_data.append(indata.copy())
            if self._speaker_writer is not None:
                self._speaker_writer.write(indata)

    def start(self, mic_device: int | None = None, speaker_device: int | None = None):
        with self._lock:
            self._mic_data = []
            self._speaker_data = []
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
                try:
                    self._speaker_stream = sd.InputStream(
                        samplerate=self.sample_rate,
                        channels=1,
                        dtype="float32",
                        device=speaker_device,
                        callback=self._speaker_callback,
                        extra_settings=sd.WasapiSettings(loopback=True),
                    )
                    self._speaker_stream.start()
                except Exception:
                    self._speaker_stream = None

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

        result = {}
        if self._mic_data:
            result["mic"] = np.concatenate(self._mic_data, axis=0)
        if self._speaker_data:
            result["speaker"] = np.concatenate(self._speaker_data, axis=0)
        return result

    @property
    def is_recording(self) -> bool:
        return self._recording

    def start_streaming(self, mic_path, speaker_path=None):
        """Open streaming WAV writers for crash safety during meeting recording."""
        self._mic_writer = StreamingWavWriter(mic_path, channels=1, rate=self.sample_rate)
        if speaker_path is not None:
            self._speaker_writer = StreamingWavWriter(speaker_path, channels=1, rate=self.sample_rate)

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


def save_stereo_wav(filepath: str, mic: np.ndarray, speaker: np.ndarray, sample_rate: int = 16000):
    path = Path(filepath)
    path.parent.mkdir(parents=True, exist_ok=True)
    max_len = max(len(mic.flatten()), len(speaker.flatten()))
    mic_padded = np.pad(mic.flatten(), (0, max_len - len(mic.flatten())))
    spk_padded = np.pad(speaker.flatten(), (0, max_len - len(speaker.flatten())))
    stereo = np.column_stack([mic_padded, spk_padded])
    int_data = (stereo * 32767).astype(np.int16)
    with wave.open(str(path), "w") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(int_data.tobytes())

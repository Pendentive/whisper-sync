"""Always-on wake-word listener - assistant build round, steps 2+3.

Tier 0+1 of the voice-assistant architecture (direction spec): a
shared, non-exclusive mic stream feeds 80ms frames into an openWakeWord
model on the CPU, with a PRETRAINED phrase as a placeholder (default
"hey jarvis") until the owner's custom wake/outro phrases and the
in-app trainer arrive (step 5).

Tier 2 splice (step 3): the listener keeps a rolling RAM ring buffer of
the last ~2.5s of frames. On detection it hands that buffer to a normal
disk-first dictation start (DictationFlow.begin_via_wake), so the
syllables spoken around the wake phrase are not lost while the
dictation mic opens. The spoken wake phrase rides along in the prefix
audio; the stop path strips it from the transcription text
(strip_leading_phrase). Known POC gap, recorded in the plan doc: audio
between detection and the dictation mic opening (~0.1-0.3s, usually
the natural pause after the phrase) is not captured.

Power and privacy posture (spec + owner decisions):
- OFF by default (``wake_listener``); tray toggle under Settings.
- All audio stays in RAM inside openWakeWord's internal buffer;
  nothing is written to disk before a wake.
- openWakeWord's built-in silero VAD gate (``vad_threshold``) keeps
  the melspectrogram/model work near-zero while the room is silent.
- The listener PAUSES while whisper/incognito mode is on (owner
  default until decided otherwise) and while a recording or overlay
  dictation is running (the mic is already being captured; a wake
  mid-recording has nothing to do yet).
- The stream is opened in shared mode (sounddevice/WASAPI default) and
  never blocks other apps or the recorder from the mic.

Dependency posture: openwakeword (+ onnxruntime) lives in the venv;
this module imports it lazily and is fully inert - one log line, no
thread - when the package or its models are missing, so the
dependency-light system python (CI) never sees it.

Feature pattern (gpu_guard precedent): one owning module, flat config
keys (wake_listener, wake_phrase_model, wake_threshold), inert when
disabled.
"""

from __future__ import annotations

import re
import threading
import time
from collections import deque

from .logger import logger
from .notifications import notify

# 80ms at 16kHz - openWakeWord's native frame size.
SAMPLE_RATE = 16000
FRAME_SAMPLES = 1280

# After a detection, ignore further hits for this long: scores stay
# elevated for several consecutive frames per utterance, and one
# spoken wake phrase must fire exactly one wake.
REFRACTORY_S = 3.0

# Rolling pre-wake buffer handed to the dictation splice. Long enough
# to cover the spoken wake phrase plus lead-in; short enough that the
# transcriber barely notices the prefix. RAM cost: ~80 KB of int16.
RING_SECONDS = 2.5
RING_FRAMES = int(RING_SECONDS * SAMPLE_RATE / FRAME_SAMPLES)

# strip_leading_phrase only strips when the wake phrase appears within
# this many characters of the start of the transcription: the ring is
# ~2.5s, so a genuine spoken phrase always lands early. Anything later
# is the user SAYING the phrase mid-sentence and must be preserved.
STRIP_SEARCH_CHARS = 48


def _phrase_tokens(phrase_name: str) -> list[str]:
    """Model name -> spoken words ("hey_jarvis_v0.1" -> ["hey", "jarvis"]).

    Drops path components, the file extension, and bare version tokens
    (v0, v1, ...) that appear in openWakeWord model file names.
    """
    stem = re.split(r"[\\/]", phrase_name)[-1]
    stem = stem.split(".", 1)[0]
    tokens = [t for t in re.split(r"[_\-\s]+", stem.lower()) if t]
    return [t for t in tokens if not re.fullmatch(r"v\d+", t)]


def strip_leading_phrase(text: str, phrase_name: str) -> str:
    """Remove the spoken wake phrase (and any lead-in before it) from
    the head of a wake-spliced transcription.

    The ring-buffer prefix contains the wake phrase and up to ~1.5s of
    room audio before it; both belong to the summons, not the
    dictation. Conservative: when the phrase is not found near the
    start, the text is returned unchanged.
    """
    if not text:
        return text
    tokens = _phrase_tokens(phrase_name)
    if not tokens:
        return text
    # Whisper renders "hey jarvis" as "Hey, Jarvis." and friends -
    # allow punctuation and whitespace between and after the tokens.
    # Word boundaries keep single-token phrases from matching inside a
    # longer word ("alexa" must not strip through "Alexander").
    sep = r"[\s,.!?;:-]+"
    pattern = (r"\b" + sep.join(re.escape(t) for t in tokens)
               + r"\b[\s,.!?;:-]*")
    match = re.search(pattern, text, re.IGNORECASE)
    if match is None or match.start() > STRIP_SEARCH_CHARS:
        return text
    return text[match.end():].lstrip()


class WakeListener:
    """Owns the always-on listening loop; services via ``app``."""

    def __init__(self, app, on_wake=None):
        self.app = app
        # The step-3 seam: the default action IS the tier-2 splice now;
        # tests (and future command routing) can still inject on_wake.
        self._on_wake = on_wake or self._default_wake_action
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._last_fire = 0.0
        self._lock = threading.Lock()
        # Rolling pre-wake audio (int16 mono frames). Only fed while
        # inference runs: paused periods (whisper mode, recordings)
        # leave nothing behind, and the buffer is cleared on pause
        # entry so pre-pause audio never crosses a privacy boundary.
        self._ring: deque = deque(maxlen=RING_FRAMES)
        self._was_paused = False

    # -- Config ----------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return bool(self.app.cfg.get("wake_listener", False))

    def _phrase(self) -> str:
        return str(self.app.cfg.get("wake_phrase_model", "hey_jarvis"))

    def _threshold(self) -> float:
        try:
            return float(self.app.cfg.get("wake_threshold", 0.5))
        except (TypeError, ValueError):
            return 0.5

    # -- Lifecycle ---------------------------------------------------------------

    def start(self) -> None:
        """Start the listening thread if enabled. Safe to call again
        after a config toggle; a healthy running thread is left alone.

        The stop event is bound PER THREAD (worker _WorkerGeneration
        precedent): a quick off -> on toggle starts a fresh generation
        immediately instead of short-circuiting on the old, stopping
        thread and leaving the listener enabled-but-inert (review
        catch). A briefly overlapping old thread exits on ITS OWN
        event; both streams are shared-mode, so the overlap is
        harmless.
        """
        with self._lock:
            if not self.enabled:
                return
            if (self._thread is not None and self._thread.is_alive()
                    and not self._stop_event.is_set()):
                return  # already running and not stopping
            self._stop_event = threading.Event()
            self._thread = threading.Thread(
                target=self._run, args=(self._stop_event,),
                daemon=True, name="wake-listener")
            self._thread.start()

    def stop(self) -> None:
        """Signal the listening thread to exit (config toggle, quit)."""
        self._stop_event.set()

    def restart_if_toggled(self) -> None:
        """Reconcile the thread with the config flag (tray setter)."""
        if self.enabled:
            self.start()
        else:
            self.stop()

    # -- The listening loop -------------------------------------------------------

    def _run(self, stop_event: threading.Event) -> None:
        try:
            model = self._load_model()
        except ImportError as exc:
            logger.warning(
                f"Wake listener unavailable: openwakeword is not "
                f"installed ({exc}) - pip install openwakeword in "
                "whisper-env to enable it")
            notify("Wake listener unavailable",
                   "openwakeword is not installed; see the log.")
            return
        except Exception:
            # Distinct from the missing dependency (review catch):
            # model download/load or onnxruntime failures need the
            # real traceback, not an install hint.
            logger.warning("Wake listener failed to load its model",
                           exc_info=True)
            notify("Wake listener failed",
                   "Wake model could not load; see the log.")
            return
        if model is None:
            return
        import numpy as np
        import sounddevice as sd

        logger.info(
            f"Wake listener active: phrase model '{self._phrase()}', "
            f"threshold {self._threshold():.2f}")
        self._was_paused = False
        self._ring.clear()
        try:
            with sd.InputStream(samplerate=SAMPLE_RATE, channels=1,
                                dtype="int16", blocksize=FRAME_SAMPLES) as stream:
                while not stop_event.is_set():
                    frame, _overflowed = stream.read(FRAME_SAMPLES)
                    self.handle_frame(np.squeeze(frame), model)
        except Exception:
            logger.warning("Wake listener stopped on stream error",
                           exc_info=True)
        finally:
            logger.info("Wake listener stopped")

    def _load_model(self):
        """Load the openWakeWord model (lazy heavy import)."""
        from openwakeword.model import Model
        try:
            return Model(wakeword_models=[self._phrase()],
                         inference_framework="onnx",
                         vad_threshold=0.5)
        except Exception:
            # Pretrained model files are a one-time download.
            logger.info("Wake listener: downloading openWakeWord models...")
            import openwakeword.utils
            openwakeword.utils.download_models()
            return Model(wakeword_models=[self._phrase()],
                         inference_framework="onnx",
                         vad_threshold=0.5)

    # -- Decision logic (unit-tested without audio) --------------------------------

    def handle_frame(self, mono, model) -> None:
        """One frame's worth of decisions (unit-tested without audio).

        ``mono`` is one 80ms int16 frame (already squeezed to 1-D by the
        loop); ``model`` is the loaded openWakeWord model. While paused,
        inference is skipped and the model is reset ONCE on entering the
        pause (review catch on the POC: not per 80ms frame) - it clears
        pre-pause audio so it cannot fire on resume, and the ring buffer
        is cleared for the same reason (paused audio is someone's
        recording or whisper-mode speech; the splice must never inherit
        it).
        """
        if self._paused():
            if not self._was_paused:
                model.reset()
                self._ring.clear()
                self._was_paused = True
            return
        self._was_paused = False
        self._ring.append(mono)
        self.process_scores(model.predict(mono))

    def _assemble_prefix(self):
        """Ring frames -> the mono float32 column the recorder expects.

        Consumes (and clears) the ring. Returns None when the ring is
        empty. numpy is imported lazily: this only runs on the wake
        path, where the audio stack is present by definition.
        """
        if not self._ring:
            return None
        import numpy as np
        frames = list(self._ring)
        self._ring.clear()
        audio = np.concatenate(frames).astype(np.float32) / 32768.0
        return audio.reshape(-1, 1)

    def _paused(self) -> bool:
        """True while inference should not run.

        Whisper mode: owner default - the listener does not listen.
        Recording/overlay: the mic is already captured; nothing for a
        wake to do until the tier-2 splice exists.
        """
        cfg = self.app.cfg
        if cfg.get("incognito", False):
            return True
        state = self.app.state
        current = state.current if state else None
        if current is None:
            return True
        return (current.mode not in (None, "done", "error")
                or self.app.recorder.is_recording)

    def process_scores(self, scores: dict) -> bool:
        """Evaluate one frame's model scores; fire at most one wake.

        Returns True when a wake fired (refractory window applies).
        """
        threshold = self._threshold()
        hit = any(score >= threshold for score in (scores or {}).values())
        if not hit:
            return False
        now = time.monotonic()
        if now - self._last_fire < REFRACTORY_S:
            return False
        self._last_fire = now
        try:
            self._on_wake()
        except Exception:
            logger.warning("Wake action failed", exc_info=True)
        return True

    def _default_wake_action(self) -> None:
        """Tier-2 splice: hand the ring buffer to a dictation start.

        begin_via_wake owns the busy checks and wakes a sleeping model
        itself (recording is disk-first, so it starts immediately and
        transcription waits for the load). The prefix is skipped when
        the recorder runs at a non-16k sample rate: the ring is 16k and
        must not be spliced into a stream at another rate.
        """
        logger.info("Wake word detected - starting dictation")
        prefix = None
        try:
            recorder_rate = int(self.app.cfg.get("sample_rate", SAMPLE_RATE))
        except (TypeError, ValueError):
            recorder_rate = SAMPLE_RATE
        if recorder_rate == SAMPLE_RATE:
            prefix = self._assemble_prefix()
        else:
            self._ring.clear()
            logger.debug("wake prefix skipped: recorder sample_rate is "
                         f"{recorder_rate}, ring is {SAMPLE_RATE}")
        started = self.app.dictation.begin_via_wake(prefix)
        if not started:
            logger.info("Wake word heard but dictation could not start "
                        "(another workflow is active)")
            self.app._yellow_flash()

"""Always-on wake-word listener - assistant build round, step 2 (POC).

Tier 0+1 of the voice-assistant architecture (direction spec): a
shared, non-exclusive mic stream feeds 80ms frames into an openWakeWord
model on the CPU. This POC ships with a PRETRAINED phrase as a
placeholder (default "hey jarvis") - the owner's custom wake/outro
phrases and the in-app trainer come later. On detection the POC wakes
the transcription model (auto_sleep.wake) and toasts; the tier-2
splice into a ring-buffer-prefixed dictation is the next step.

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

import threading
import time

from .logger import logger
from .notifications import notify

# 80ms at 16kHz - openWakeWord's native frame size.
SAMPLE_RATE = 16000
FRAME_SAMPLES = 1280

# After a detection, ignore further hits for this long: scores stay
# elevated for several consecutive frames per utterance, and one
# spoken wake phrase must fire exactly one wake.
REFRACTORY_S = 3.0


class WakeListener:
    """Owns the always-on listening loop; services via ``app``."""

    def __init__(self, app, on_wake=None):
        self.app = app
        # Seam for step 3: the splice replaces this default action.
        self._on_wake = on_wake or self._default_wake_action
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._last_fire = 0.0
        self._lock = threading.Lock()

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
        was_paused = False
        try:
            with sd.InputStream(samplerate=SAMPLE_RATE, channels=1,
                                dtype="int16", blocksize=FRAME_SAMPLES) as stream:
                while not stop_event.is_set():
                    frame, _overflowed = stream.read(FRAME_SAMPLES)
                    if self._paused():
                        # Keep draining the stream (cheap) but skip
                        # inference. Reset ONCE on entering the pause
                        # (review catch: not per 80ms frame) - clears
                        # pre-pause audio so it cannot fire on resume;
                        # nothing accumulates during the pause because
                        # paused frames are never fed to the model.
                        if not was_paused:
                            model.reset()
                            was_paused = True
                        continue
                    was_paused = False
                    scores = model.predict(np.squeeze(frame))
                    self.process_scores(scores)
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
        """POC action: wake the model + announce. The tier-2 splice
        (ring-buffer-prefixed dictation + outro phrase) replaces this."""
        logger.info("Wake word detected")
        state = self.app.state
        if state is not None and state.current.sleeping:
            self.app.auto_sleep.wake(reason="wake_word")
        else:
            self.app._yellow_flash()
        notify("Wake word heard",
               "POC: dictation splice arrives in the next step.")

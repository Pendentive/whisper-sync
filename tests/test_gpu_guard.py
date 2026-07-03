"""Tests for whisper_sync.gpu_guard and vram_probe.

Everything runs against fake probes and a temp event log; no GPU, no
subprocess, no scheduler thread (check_once is driven directly).
"""

import json
import tempfile
import unittest
from pathlib import Path

from whisper_sync.gpu_guard import GpuGuard
from whisper_sync.vram_probe import ProbeResult, get_probe


def _cfg(**overrides):
    base = {
        "gpu_guard": True,
        "gpu_guard_low_vram_mb": 750,
        "gpu_guard_ladder": ["large-v3", "medium", "small", "base"],
        "model": "large-v3",
    }
    base.update(overrides)
    return base


class _Harness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.event_path = Path(self._tmp.name) / "gpu-guard.jsonl"
        self.toasts = []

    def _guard(self, free_sequence=None, **cfg_overrides):
        seq = list(free_sequence or [])

        def _probe():
            if not seq:
                return None
            free = seq.pop(0)
            if free is None:
                return None
            return ProbeResult(total_mb=8192, free_mb=free)

        return GpuGuard(
            _cfg(**cfg_overrides),
            notify=lambda t, m: self.toasts.append((t, m)),
            probe=_probe, probe_name="fake",
            event_path=self.event_path,
        )

    def _events(self):
        if not self.event_path.exists():
            return []
        return [json.loads(l) for l in self.event_path.read_text().splitlines()]


class WatermarkTests(_Harness):
    def test_low_vram_arms_one_downgrade(self):
        g = self._guard(free_sequence=[500])
        g.check_once()
        self.assertEqual(g.effective_model("large-v3"), "medium")
        events = self._events()
        self.assertEqual(events[-1]["event"], "downgrade_armed")
        self.assertEqual(events[-1]["trigger"], "low_vram")
        self.assertEqual(events[-1]["free_mb"], 500)
        self.assertEqual(events[-1]["model_to"], "medium")
        self.assertEqual(len(self.toasts), 1)

    def test_persistent_low_reading_does_not_walk_the_ladder(self):
        # Hysteresis: staying below the watermark must not escalate on
        # every poll - only a recovery + new breach re-arms.
        g = self._guard(free_sequence=[500, 400, 300])
        for _ in range(3):
            g.check_once()
        self.assertEqual(g.effective_model("large-v3"), "medium",
                         "three consecutive low reads must equal ONE downgrade")

    def test_recovery_then_new_breach_escalates_again(self):
        g = self._guard(free_sequence=[500, 2000, 600])
        g.check_once()  # breach -> level 1
        g.check_once()  # recovery
        g.check_once()  # new breach -> level 2
        self.assertEqual(g.effective_model("large-v3"), "small")
        kinds = [e["event"] for e in self._events()]
        self.assertIn("vram_recovered", kinds)

    def test_healthy_vram_never_downgrades(self):
        g = self._guard(free_sequence=[4000, 3000])
        g.check_once()
        g.check_once()
        self.assertEqual(g.effective_model("large-v3"), "large-v3")
        self.assertEqual(self.toasts, [])

    def test_probe_failure_is_transient_noop(self):
        g = self._guard(free_sequence=[None, 4000])
        g.check_once()  # probe failed; no state change
        g.check_once()
        self.assertEqual(g.effective_model("large-v3"), "large-v3")


class LadderTests(_Harness):
    def test_crash_trigger_escalates_immediately(self):
        g = self._guard()
        g.note_pressure_trigger("worker_crash_meeting")
        self.assertEqual(g.effective_model("large-v3"), "medium")
        g.note_pressure_trigger("worker_crash_meeting")
        self.assertEqual(g.effective_model("large-v3"), "small")

    def test_ladder_floor_is_sticky_and_logged(self):
        g = self._guard()
        for _ in range(10):
            g.note_pressure_trigger("worker_crash_dictation")
        self.assertEqual(g.effective_model("large-v3"), "base")
        self.assertIn("ladder_floor", [e["event"] for e in self._events()])

    def test_downgrade_never_upgrades_a_smaller_request(self):
        # Caller already using 'base' for dictation must not be bumped
        # up to the guard's rung.
        g = self._guard()
        g.note_pressure_trigger("x")  # rung = medium
        self.assertEqual(g.effective_model("base"), "base")
        self.assertEqual(g.effective_model("small"), "small")

    def test_model_not_on_ladder_maps_to_rung(self):
        g = self._guard()
        g.note_pressure_trigger("x")
        self.assertEqual(g.effective_model("distil-large-v3"), "medium")

    def test_level_zero_passes_through_unknown_models(self):
        g = self._guard()
        self.assertEqual(g.effective_model("distil-large-v3"), "distil-large-v3")


class DisabledTests(_Harness):
    def test_disabled_guard_is_inert(self):
        g = self._guard(gpu_guard=False)
        g.note_pressure_trigger("worker_crash_meeting")
        self.assertEqual(g.effective_model("large-v3"), "large-v3")
        self.assertEqual(self.toasts, [])

    def test_start_without_probe_logs_event_and_stays_inactive(self):
        g = GpuGuard(_cfg(), probe=None, probe_name=None,
                     event_path=self.event_path)

        class _NoProviderScheduler:
            def call_every(self, *a, **k):
                raise AssertionError("must not poll without a probe")

        import unittest.mock as mock
        with mock.patch("whisper_sync.vram_probe.get_probe",
                        return_value=(None, None)):
            g.start(_NoProviderScheduler(), io_executor=None)
        self.assertIn("probe_unavailable", [e["event"] for e in self._events()])


class ProbeRegistryTests(unittest.TestCase):
    def test_get_probe_returns_none_when_no_provider(self):
        import unittest.mock as mock
        import whisper_sync.vram_probe as vp
        with mock.patch.object(vp, "PROVIDERS", [("x", lambda: None)]):
            self.assertEqual(get_probe(), (None, None))

    def test_preferred_provider_pins_selection(self):
        import unittest.mock as mock
        import whisper_sync.vram_probe as vp
        fake = lambda: ProbeResult(8192, 4096)
        with mock.patch.object(vp, "PROVIDERS", [
            ("first", lambda: (lambda: None)),
            ("second", lambda: fake),
        ]):
            name, probe = get_probe(preferred="second")
        self.assertEqual(name, "second")
        self.assertEqual(probe(), ProbeResult(8192, 4096))


if __name__ == "__main__":
    unittest.main()

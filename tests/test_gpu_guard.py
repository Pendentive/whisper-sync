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
    # note_pressure_trigger probes device reachability before walking
    # the ladder; these tests model a healthy (reachable) GPU, so each
    # trigger consumes one successful probe from the sequence.

    def test_crash_trigger_escalates_immediately(self):
        g = self._guard(free_sequence=[4000, 4000])
        g.note_pressure_trigger("worker_crash_meeting")
        self.assertEqual(g.effective_model("large-v3"), "medium")
        g.note_pressure_trigger("worker_crash_meeting")
        self.assertEqual(g.effective_model("large-v3"), "small")

    def test_ladder_floor_is_sticky_and_logged(self):
        g = self._guard(free_sequence=[4000] * 10)
        for _ in range(10):
            g.note_pressure_trigger("worker_crash_dictation")
        self.assertEqual(g.effective_model("large-v3"), "base")
        self.assertIn("ladder_floor", [e["event"] for e in self._events()])

    def test_downgrade_never_upgrades_a_smaller_request(self):
        # Caller already using 'base' for dictation must not be bumped
        # up to the guard's rung.
        g = self._guard(free_sequence=[4000])
        g.note_pressure_trigger("x")  # rung = medium
        self.assertEqual(g.effective_model("base"), "base")
        self.assertEqual(g.effective_model("small"), "small")

    def test_model_not_on_ladder_maps_to_rung(self):
        g = self._guard(free_sequence=[4000])
        g.note_pressure_trigger("x")
        self.assertEqual(g.effective_model("distil-large-v3"), "medium")

    def test_level_zero_passes_through_unknown_models(self):
        g = self._guard()
        self.assertEqual(g.effective_model("distil-large-v3"), "distil-large-v3")


class DisabledTests(_Harness):
    def test_providerless_guard_is_inert_even_for_crash_triggers(self):
        # Regression for PR #158 review: without a probe provider the
        # guard must never alter model selection, including on crashes.
        g = GpuGuard(_cfg(), notify=lambda t, m: None,
                     probe=None, probe_name=None, event_path=self.event_path)
        g.note_pressure_trigger("worker_crash_meeting")
        self.assertEqual(g.effective_model("large-v3"), "large-v3")

    def test_invalid_ladder_config_falls_back_to_default(self):
        # Regression for PR #158 review: an empty or malformed ladder
        # must not crash escalation with an IndexError.
        g = self._guard(free_sequence=[4000], gpu_guard_ladder=[])
        g.note_pressure_trigger("x")
        self.assertEqual(g.effective_model("large-v3"), "medium")
        # string, not list
        g2 = self._guard(free_sequence=[4000], gpu_guard_ladder="large-v3")
        g2.note_pressure_trigger("x")
        self.assertEqual(g2.effective_model("large-v3"), "medium")

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


class DeviceFailoverTests(_Harness):
    """GPU power-state failover (2026-07-04 voice-assistant spec)."""

    def test_three_failed_polls_declare_device_lost(self):
        g = self._guard(free_sequence=[None, None, None])
        for _ in range(3):
            g.check_once()
        self.assertTrue(g.device_lost)
        self.assertEqual(g.effective_model("large-v3"), "base")
        events = [e for e in self._events() if e["event"] == "gpu_device_lost"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["fail_streak"], 3)
        self.assertEqual(len(self.toasts), 1)

    def test_two_failures_then_success_stays_healthy(self):
        g = self._guard(free_sequence=[None, None, 4000])
        for _ in range(3):
            g.check_once()
        self.assertFalse(g.device_lost)
        self.assertEqual(g.effective_model("large-v3"), "large-v3")

    def test_crash_with_failed_probe_fails_over_immediately(self):
        g = self._guard(free_sequence=[None])
        g.note_pressure_trigger("worker_crash_dictation")
        self.assertTrue(g.device_lost)
        self.assertEqual(g.effective_model("large-v3"), "base")
        # While lost, the fallback clamp governs - no ladder escalation.
        self.assertNotIn("downgrade_armed",
                         [e["event"] for e in self._events()])

    def test_crash_with_healthy_probe_keeps_ladder_semantics(self):
        g = self._guard(free_sequence=[4000])
        g.note_pressure_trigger("worker_crash_dictation")
        self.assertFalse(g.device_lost)
        self.assertEqual(g.effective_model("large-v3"), "medium")

    def test_successful_poll_clears_loss(self):
        g = self._guard(free_sequence=[None, None, None, 4000])
        for _ in range(4):
            g.check_once()
        self.assertFalse(g.device_lost)
        self.assertEqual(g.effective_model("large-v3"), "large-v3")
        self.assertIn("gpu_device_recovered",
                      [e["event"] for e in self._events()])

    def test_fallback_never_upgrades_a_smaller_request(self):
        g = self._guard(free_sequence=[None], cpu_fallback_model="small")
        g.note_pressure_trigger("x")
        self.assertEqual(g.effective_model("large-v3"), "small")
        self.assertEqual(g.effective_model("base"), "base")

    def test_explicit_cpu_device_never_declares_loss(self):
        g = self._guard(free_sequence=[None, None, None, None], device="cpu")
        for _ in range(3):
            g.check_once()
        g.note_pressure_trigger("worker_crash_dictation")
        self.assertFalse(g.device_lost)
        # The ladder still applies on explicit-cpu machines (existing
        # semantics); only the failover path is gated off.
        self.assertEqual(g.effective_model("large-v3"), "medium")

    def test_respawn_overlay_pins_cpu_while_lost(self):
        g = self._guard(free_sequence=[None])
        self.assertIsNone(g.respawn_overlay())
        g.note_pressure_trigger("worker_crash_meeting")
        overlay = g.respawn_overlay()
        self.assertEqual(overlay, {
            "device": "cpu",
            "model": "base",
            "dictation_model": "base",
            "compute_type": "int8",
        })

    def test_invalid_fallback_model_uses_base(self):
        g = self._guard(free_sequence=[None], cpu_fallback_model=123)
        g.note_pressure_trigger("x")
        self.assertEqual(g.effective_model("large-v3"), "base")

    def test_repeated_crashes_while_lost_toast_once(self):
        g = self._guard(free_sequence=[None, None])
        g.note_pressure_trigger("x")
        g.note_pressure_trigger("x")
        self.assertEqual(len(self.toasts), 1)
        events = [e for e in self._events() if e["event"] == "gpu_device_lost"]
        self.assertEqual(len(events), 1)

    def test_providerless_guard_never_fails_over(self):
        g = GpuGuard(_cfg(), notify=lambda t, m: None,
                     probe=None, probe_name=None, event_path=self.event_path)
        self.assertFalse(g.device_lost)
        self.assertIsNone(g.respawn_overlay())

    def test_cpu_period_does_not_bank_failures(self):
        # Review catch: failures during an explicit-cpu period must not
        # accumulate, or the first failure after switching back to auto
        # would declare loss instantly, bypassing the 3-failure rule.
        g = self._guard(free_sequence=[None] * 8, device="cpu")
        for _ in range(5):
            g.check_once()
        g._cfg["device"] = "auto"
        g.check_once()  # first failure after the switch: streak = 1
        self.assertFalse(g.device_lost)
        g.check_once()
        g.check_once()  # third consecutive failure: now lost
        self.assertTrue(g.device_lost)

    def test_crash_event_logs_the_fresh_probe_reading(self):
        # Review catch: the downgrade event must carry the reading from
        # the probe that just ran, not the last poll's stale value.
        g = self._guard(free_sequence=[3000])
        g.note_pressure_trigger("worker_crash_dictation")
        armed = [e for e in self._events() if e["event"] == "downgrade_armed"]
        self.assertEqual(armed[0]["free_mb"], 3000)
        self.assertEqual(armed[0]["total_mb"], 8192)


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

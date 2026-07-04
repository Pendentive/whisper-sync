"""Tests for whisper_sync.meeting_watch - per-app meeting auto-record.

Everything runs against a fake consent-store probe and a fake app; no
registry, no scheduler thread (check_once is driven directly). One
integration test touches the real consent store read path on Windows
to pin the does-not-crash contract.
"""

import threading
import types
import unittest
from unittest import mock

from whisper_sync import meeting_watch as mw
from whisper_sync.meeting_watch import MeetingWatch, apps_map, match_token
from whisper_sync.state_manager import StateManager, MEETING_STARTED, IDLE

ZOOM = "c:#program files#zoom#bin#zoom.exe"
STALE_SLACK = "c:#users#x#appdata#local#slack#app-4.50.140#slack.exe"


class _FakeMeetings:
    """Toggle stand-in that mirrors the real mode contract."""

    def __init__(self, app):
        self.app = app
        self.toggles = 0
        self.aborts = 0

    def toggle(self):
        self.toggles += 1
        current = self.app.state.current
        if current.mode == "meeting":
            self.app.state.emit(IDLE, mode=None)
        else:
            self.app.state.emit(MEETING_STARTED, mode="meeting")

    def abort_recording(self, reason="user"):
        self.aborts += 1
        if self.app.state.current.mode == "meeting":
            self.app.state.emit(IDLE, mode=None)


class _FakeApp:
    def __init__(self):
        self.cfg = {
            "meeting_auto_record": True,
            "meeting_watch_apps": {"zoom.exe": "record",
                                   "slack.exe": "record",
                                   "msteams": "record"},
            "meeting_watch_poll_seconds": 5,
            "meeting_watch_stop_after_s": 30,
        }
        self._lock = threading.RLock()
        self.state = StateManager(None, {})
        self.recorder = types.SimpleNamespace(is_recording=False)
        self.meetings = _FakeMeetings(self)

    def _can_record(self):
        mode = self.state.current.mode
        return mode is None or mode in ("transcribing", "done", "error")


class _WatchHarness(unittest.TestCase):
    def setUp(self):
        self.app = _FakeApp()
        self.polls = []  # list of {entry: active} dicts, or None
        self.watch = MeetingWatch(self.app, read_entries=self._next_poll)
        p = mock.patch.object(mw, "notify")
        self.notify = p.start()
        self.addCleanup(p.stop)

    def _next_poll(self):
        return self.polls.pop(0) if self.polls else {}

    def _run_polls(self, snapshots):
        # Count-driven, never consumption-driven: a DISABLED watcher
        # returns before reading the store, so `while self.polls` would
        # spin forever (this exact bug hung the first suite run).
        self.polls = list(snapshots)
        for _ in snapshots:
            self.watch.check_once()

    @property
    def mode(self):
        return self.app.state.current.mode


class TriggerTests(_WatchHarness):
    def test_observed_transition_plus_debounce_starts_recording(self):
        self._run_polls([
            {ZOOM: False},  # baseline
            {ZOOM: True},   # transition observed (streak 1)
            {ZOOM: True},   # debounce satisfied -> start
        ])
        self.assertEqual(self.app.meetings.toggles, 1)
        self.assertEqual(self.mode, "meeting")
        self.assertIn("Auto-recording", self.notify.call_args[0][0])

    def test_entry_already_active_at_baseline_never_triggers(self):
        # The stale-entry hazard: a dead app version's entry can be
        # stuck active forever - only observed transitions count.
        self._run_polls([{STALE_SLACK: True}] * 5)
        self.assertEqual(self.app.meetings.toggles, 0)

    def test_one_poll_blip_does_not_trigger(self):
        self._run_polls([
            {ZOOM: False},
            {ZOOM: True},   # mic permission check blip
            {ZOOM: False},
            {ZOOM: False},
        ])
        self.assertEqual(self.app.meetings.toggles, 0)

    def test_no_retrigger_while_continuously_active(self):
        self._run_polls([{ZOOM: False}] + [{ZOOM: True}] * 6)
        self.assertEqual(self.app.meetings.toggles, 1)

    def test_stale_sibling_does_not_mask_the_live_entry(self):
        live = "c:#users#x#appdata#local#slack#app-4.50.143#slack.exe"
        self._run_polls([
            {STALE_SLACK: True, live: False},
            {STALE_SLACK: True, live: True},
            {STALE_SLACK: True, live: True},
        ])
        self.assertEqual(self.app.meetings.toggles, 1)

    def test_unwatched_app_is_ignored(self):
        game = "d:#games#overwatch#overwatch.exe"
        self._run_polls([{game: False}, {game: True}, {game: True}])
        self.assertEqual(self.app.meetings.toggles, 0)

    def test_newly_watched_stale_entry_does_not_trigger(self):
        # Review catch: the baseline covers ALL entries, so editing
        # meeting_watch_apps mid-session cannot promote a stale
        # always-active entry into a "transition".
        self.app.cfg["meeting_watch_apps"] = ["zoom.exe"]
        self._run_polls([{STALE_SLACK: True}, {STALE_SLACK: True}])
        self.app.cfg["meeting_watch_apps"] = ["zoom.exe", "slack.exe"]
        self._run_polls([{STALE_SLACK: True}] * 3)
        self.assertEqual(self.app.meetings.toggles, 0)

    def test_entry_appearing_active_is_a_real_acquire(self):
        # The consent store only creates entries on actual mic use, so
        # an entry APPEARING active (first-ever use, or a fresh app
        # version directory) is a genuine transition.
        self._run_polls([{}, {ZOOM: True}, {ZOOM: True}])
        self.assertEqual(self.app.meetings.toggles, 1)

    def test_busy_at_debounce_retries_until_recordable(self):
        # Review catch: a dictation in flight at the debounce moment
        # must not lose the whole meeting - the start retries while the
        # mic stays held.
        self.app.state.emit("dictation_started", mode="dictation")
        self._run_polls([{ZOOM: False}] + [{ZOOM: True}] * 3)
        self.assertEqual(self.app.meetings.toggles, 0)
        self.app.state.emit("idle", mode=None)
        self._run_polls([{ZOOM: True}])
        self.assertEqual(self.app.meetings.toggles, 1)
        self.assertEqual(self.mode, "meeting")

    def test_manual_recording_already_running_is_left_alone(self):
        self.app.state.emit(MEETING_STARTED, mode="meeting")
        self._run_polls([{ZOOM: False}, {ZOOM: True}, {ZOOM: True}])
        self.assertEqual(self.app.meetings.toggles, 0)
        # And the manual recording is never auto-stopped afterwards.
        self._run_polls([{ZOOM: False}] * 8)
        self.assertEqual(self.mode, "meeting")

    def test_second_meeting_after_release_retriggers(self):
        self._run_polls([
            {ZOOM: False},
            {ZOOM: True}, {ZOOM: True},              # meeting 1 starts
        ])
        # meeting 1 ends: sustained release auto-stops (6 polls at 5s/30s)
        self._run_polls([{ZOOM: False}] * 6)
        self.assertEqual(self.mode, None)
        # meeting 2: a fresh transition triggers again
        self._run_polls([{ZOOM: True}, {ZOOM: True}])
        self.assertEqual(self.app.meetings.toggles, 3)  # start, stop, start


class AutoStopTests(_WatchHarness):
    def _start_auto_meeting(self):
        self._run_polls([{ZOOM: False}, {ZOOM: True}, {ZOOM: True}])
        self.assertEqual(self.mode, "meeting")

    def test_sustained_release_stops_the_auto_started_recording(self):
        self._start_auto_meeting()
        # 30s at 5s polls = 6 release polls
        self._run_polls([{ZOOM: False}] * 5)
        self.assertEqual(self.mode, "meeting", "not yet - needs 6 polls")
        self._run_polls([{ZOOM: False}])
        self.assertEqual(self.mode, None)
        self.assertIn("Meeting recording stopped",
                      self.notify.call_args[0][0])

    def test_stop_timing_never_undershoots_the_configured_release(self):
        # Review catch: round() stopped at 28s with poll=7/stop=30;
        # the ceiling makes it 5 polls = 35s, never less than 30.
        self.app.cfg["meeting_watch_poll_seconds"] = 7
        self._start_auto_meeting()
        self._run_polls([{ZOOM: False}] * 4)
        self.assertEqual(self.mode, "meeting", "4 polls = 28s < 30s")
        self._run_polls([{ZOOM: False}])
        self.assertEqual(self.mode, None)

    def test_brief_release_then_reacquire_keeps_recording(self):
        self._start_auto_meeting()
        self._run_polls([{ZOOM: False}] * 3 + [{ZOOM: True}] * 2
                        + [{ZOOM: False}] * 5)
        self.assertEqual(self.mode, "meeting",
                         "release counter must reset on reacquire")

    def test_manual_stop_relinquishes_ownership(self):
        self._start_auto_meeting()
        self.app.meetings.toggle()  # user stops via hotkey
        self.assertEqual(self.mode, None)
        toggles = self.app.meetings.toggles
        self._run_polls([{ZOOM: False}] * 8)
        self.assertEqual(self.app.meetings.toggles, toggles,
                         "watcher must not stop or restart anything")


class LifecycleTests(_WatchHarness):
    def test_disabled_is_inert_and_reenabling_reseeds_baseline(self):
        self.app.cfg["meeting_auto_record"] = False
        self._run_polls([{ZOOM: False}, {ZOOM: True}, {ZOOM: True}])
        self.assertEqual(self.app.meetings.toggles, 0)
        # Re-enable while zoom is mid-call: the first poll is a fresh
        # baseline, so the already-active entry must not trigger.
        self.app.cfg["meeting_auto_record"] = True
        self._run_polls([{ZOOM: True}] * 3)
        self.assertEqual(self.app.meetings.toggles, 0)

    def test_unreadable_store_changes_nothing(self):
        self._run_polls([{ZOOM: False}, {ZOOM: True}, None, {ZOOM: True}])
        # None polls carry no information; the streak continues on the
        # next real read.
        self.assertEqual(self.app.meetings.toggles, 1)

    def test_start_registers_one_poll_job(self):
        calls = []
        sched = types.SimpleNamespace(
            call_every=lambda s, fn, label=None: calls.append((s, label)))
        self.watch.start(sched, io_executor=types.SimpleNamespace(
            submit_native=lambda label, fn: fn()))
        self.assertEqual(calls, [(5.0, "meeting-watch")])


class ThreeStateTests(_WatchHarness):
    """Record / Ask / Ignore per app (owner design, third intake)."""

    def _toast_buttons(self):
        kwargs = self.notify.call_args[1]
        return {b["label"]: b["action"] for b in kwargs.get("buttons", [])}

    def test_ask_state_offers_but_never_records(self):
        self.app.cfg["meeting_watch_apps"] = {"zoom.exe": "ask"}
        self._run_polls([{ZOOM: False}, {ZOOM: True}, {ZOOM: True}])
        self.assertEqual(self.app.meetings.toggles, 0)
        self.assertIn("Not recording", self.notify.call_args[0][0])
        # One offer per call, not one per poll.
        self._run_polls([{ZOOM: True}] * 3)
        self.assertEqual(self.notify.call_count, 1)

    def test_ask_toast_record_button_starts_recording(self):
        self.app.cfg["meeting_watch_apps"] = {"zoom.exe": "ask"}
        self._run_polls([{ZOOM: False}, {ZOOM: True}, {ZOOM: True}])
        self._toast_buttons()["Record"]()
        self.assertEqual(self.mode, "meeting")
        # The click-started recording is watcher-owned: it auto-stops
        # after sustained release like any auto-started one.
        self._run_polls([{ZOOM: False}] * 6)
        self.assertEqual(self.mode, None)

    def test_ignore_state_is_fully_silent(self):
        self.app.cfg["meeting_watch_apps"] = {"zoom.exe": "ignore"}
        self._run_polls([{ZOOM: False}] + [{ZOOM: True}] * 4)
        self.assertEqual(self.app.meetings.toggles, 0)
        self.notify.assert_not_called()

    def test_optin_toast_dont_record_discards_silently(self):
        self._run_polls([{ZOOM: False}, {ZOOM: True}, {ZOOM: True}])
        self.assertEqual(self.mode, "meeting")
        self._toast_buttons()["Don't record"]()
        self.assertEqual(self.app.meetings.aborts, 1)
        self.assertEqual(self.mode, None)
        # Ownership released: no auto-stop fires later.
        self._run_polls([{ZOOM: False}] * 8)
        self.assertEqual(self.app.meetings.aborts, 1)

    def test_toast_master_toggle_silences_but_still_records(self):
        self.app.cfg["meeting_watch_toasts"] = False
        self._run_polls([{ZOOM: False}, {ZOOM: True}, {ZOOM: True}])
        self.assertEqual(self.mode, "meeting")
        self.notify.assert_not_called()

    def test_optout_toggle_silences_ask_apps(self):
        self.app.cfg["meeting_watch_apps"] = {"zoom.exe": "ask"}
        self.app.cfg["meeting_watch_toast_optout"] = False
        self._run_polls([{ZOOM: False}, {ZOOM: True}, {ZOOM: True}])
        self.assertEqual(self.app.meetings.toggles, 0)
        self.notify.assert_not_called()

    def test_legacy_list_config_reads_as_all_record(self):
        self.app.cfg["meeting_watch_apps"] = ["zoom.exe"]
        self._run_polls([{ZOOM: False}, {ZOOM: True}, {ZOOM: True}])
        self.assertEqual(self.mode, "meeting")

    def test_unknown_state_string_reads_as_ignore(self):
        self.app.cfg["meeting_watch_apps"] = {"zoom.exe": "recrd"}  # typo
        self._run_polls([{ZOOM: False}] + [{ZOOM: True}] * 3)
        self.assertEqual(self.app.meetings.toggles, 0,
                         "a config typo must never enable recording")


class MatchingTests(unittest.TestCase):
    def test_exe_token_matches_leaf_only(self):
        self.assertEqual(match_token(ZOOM, ["zoom.exe"]), "zoom.exe")
        self.assertIsNone(
            match_token("c:#tools#zoom.exe#helper.exe", ["zoom.exe"]),
            "an exe token must match the path leaf, never a segment")

    def test_packaged_token_matches_family_prefix(self):
        self.assertEqual(match_token(
            "msteams_8wekyb3d8bbwe", ["msteams"]), "msteams")
        self.assertIsNone(match_token(
            "microsoft.windowscamera_8wekyb3d8bbwe", ["msteams"]))

    def test_matching_is_case_insensitive_and_skips_blanks(self):
        self.assertEqual(match_token(ZOOM, ["ZOOM.EXE"]), "zoom.exe")
        self.assertIsNone(match_token(ZOOM, ["", "  "]))

    def test_apps_map_normalizes_forms(self):
        self.assertEqual(apps_map({"meeting_watch_apps": ["Zoom.EXE", ""]}),
                         {"zoom.exe": "record"})
        self.assertEqual(
            apps_map({"meeting_watch_apps": {"zoom.exe": "ASK",
                                             "x.exe": "bogus"}}),
            {"zoom.exe": "ask", "x.exe": "ignore"})
        self.assertEqual(apps_map({"meeting_watch_apps": None}), {})


class RealStoreTests(unittest.TestCase):
    def test_read_mic_entries_does_not_crash_on_this_machine(self):
        result = mw.read_mic_entries()
        # Windows: a dict (possibly empty) of lowercase ids -> bool.
        # Elsewhere: None. Either way, never an exception.
        if result is not None:
            for key, value in result.items():
                self.assertEqual(key, key.lower())
                self.assertIsInstance(value, bool)


if __name__ == "__main__":
    unittest.main()

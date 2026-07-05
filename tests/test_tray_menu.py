"""Tests for whisper_sync.tray_menu, github_tray, and icons.FlashController.

The full right-click menu build runs against the real default config
with pystray/capture stubbed - menu-build crashes were a real crash
class (2026-05-07 heap corruption came from a menu rebuild), so "build()
completes and contains the load-bearing sections" is the smoke test
that guards every future settings change. Settings setters are pinned
through the config snapshot they save.
"""

import sys
import threading
import types
import unittest
from pathlib import Path
from unittest import mock


class _MenuItem:
    def __init__(self, text, action=None, **kw):
        self.text = str(text)
        self.action = action
        self.kw = kw


class _Menu:
    SEPARATOR = "--sep--"

    def __init__(self, *items):
        self.items = items


_fake_pystray = types.ModuleType("pystray")
_fake_pystray.MenuItem = _MenuItem
_fake_pystray.Menu = _Menu

_fake_capture = types.ModuleType("whisper_sync.capture")
_fake_capture.list_devices = lambda api_filter=None: {"inputs": [], "outputs": []}
_fake_capture.get_default_devices = lambda api_filter=None: {"input": None, "output": None}
_fake_capture.get_host_apis = lambda: [{"name": "Windows WASAPI"}, {"name": "MME"}]

_fake_transcribe = types.ModuleType("whisper_sync.transcribe")
_fake_transcribe.DIARIZE_METHODS = {"balanced_mix": "Balanced Mix",
                                    "per_channel": "Per Channel"}

_MODULE_STUBS = {"pystray": _fake_pystray,
                 "whisper_sync.capture": _fake_capture,
                 "whisper_sync.transcribe": _fake_transcribe}

# tray_menu/github_tray import pystray and capture lazily, so these
# imports are safe on the dependency-light system python. (Importing
# them INSIDE a patch.dict block would evict them from sys.modules on
# exit and later patches would target a duplicate module.)
from whisper_sync import config
from whisper_sync.config_store import ConfigStore
from whisper_sync.tray_menu import TrayMenu, menu_callback
from whisper_sync.github_tray import GitHubTray


def _iter_texts(menu):
    """Flatten all MenuItem texts in a stubbed menu tree."""
    out = []
    stack = list(getattr(menu, "items", menu))
    while stack:
        item = stack.pop()
        if isinstance(item, _MenuItem):
            out.append(item.text)
            if isinstance(item.action, _Menu):
                stack.extend(item.action.items)
        elif isinstance(item, _Menu):
            stack.extend(item.items)
    return out


class _FakeWorker:
    gpu_name = "FakeGPU"

    def is_ready(self):
        return True

    def reload_model(self, *a, **k):
        pass




class _FakeDictation:
    def recent_history(self):
        return [{"text": "hello world", "timestamp": "10:00", "chars": 11}]

    def clear_history(self):
        pass


class _FakeGitHub:
    def menu_items(self):
        return []


class _FakeApp:
    def __init__(self, tmp: Path):
        # Real defaults through the real ConfigStore: every key the
        # menu reads must exist, and setters need .snapshot().
        self.cfg = ConfigStore(config.load())
        self.cfg["output_dir"] = str(tmp)
        self.state = None
        self.tray = None
        self.worker = _FakeWorker()
        self._backup = types.SimpleNamespace(is_loading=False)
        from whisper_sync.session_stats import SessionStats
        self._stats = SessionStats()  # real: the stats menu reads its full schema
        self.dictation = _FakeDictation()
        self.meetings = types.SimpleNamespace(
            recover_meeting_speakers=lambda d: None)
        self.github = _FakeGitHub()
        self.auto_sleep = types.SimpleNamespace(toggle=lambda: None)
        self._cpu_name = "FakeCPU"
        self._dialog_dispatcher = None
        self.saved = 0
        self.refreshes = 0

    def _output_dir(self):
        return Path(self.cfg["output_dir"])

    def _refresh_menu(self):
        self.refreshes += 1

    def _on_left_click(self):
        pass

    def _update(self, branch="dev"):
        pass

    def _restart(self):
        pass

    def quit(self):
        pass

    def _yellow_flash(self):
        pass


class MenuBuildTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        p = mock.patch.dict(sys.modules, _MODULE_STUBS)
        p.start()
        self.addCleanup(p.stop)
        self.app = _FakeApp(Path(self._tmp.name))
        self.menu = TrayMenu(self.app)

    def test_full_menu_builds_with_default_config(self):
        # The load-bearing smoke test: every cfg key the menu reads must
        # exist in the shipped defaults, and no submenu build may raise.
        menu = self.menu.build()
        texts = " | ".join(_iter_texts(menu))
        for expected in ("Dictation", "Meeting", "Settings", "Quit"):
            self.assertIn(expected, texts, f"menu missing {expected!r}")

    def test_menu_includes_recent_dictation_preview(self):
        menu = self.menu._build_recent_dictations_menu()
        texts = " | ".join(_iter_texts(menu))
        self.assertIn("hello world", texts)

    def test_set_paste_method_saves_config(self):
        with mock.patch.object(config, "save") as save:
            self.menu._set_paste_method("keystrokes")
            self.assertEqual(self.app.cfg["paste_method"], "keystrokes")
            save.assert_called_once()

    def test_toggle_incognito_flips_flag(self):
        with mock.patch.object(config, "save"):
            before = self.app.cfg.get("incognito", False)
            self.menu._toggle_incognito()
            self.assertNotEqual(self.app.cfg["incognito"], before)

    def test_toggle_meeting_auto_record_flips_and_saves(self):
        with mock.patch.object(config, "save") as save:
            self.menu._toggle_meeting_auto_record()
            self.assertTrue(self.app.cfg["meeting_auto_record"])
            save.assert_called_once()

    def test_toggle_wake_listener_flips_saves_and_reconciles(self):
        self.app.wake_listener = types.SimpleNamespace(
            restart_if_toggled=mock.Mock())
        with mock.patch.object(config, "save") as save:
            self.menu._toggle_wake_listener()
        self.assertTrue(self.app.cfg["wake_listener"])
        self.app.wake_listener.restart_if_toggled.assert_called_once()
        save.assert_called_once()

    def test_saved_phrases_empty_registry_shows_info_line(self):
        items = self.menu._build_saved_phrase_items()
        self.assertEqual(len(items), 1)
        self.assertIn("No saved phrases", items[0].text)
        self.assertFalse(items[0].kw.get("enabled", True))

    def test_saved_phrases_entries_render_with_roles(self):
        self.app.cfg["wake_phrases"] = {
            "hey_hal": {"path": "C:/p/hey_hal.onnx", "role": "wake",
                        "active": True},
            "thats_all": {"path": "C:/p/t.onnx", "role": "outro",
                          "active": False}}
        items = self.menu._build_saved_phrase_items()
        texts = [i.text for i in items]
        self.assertEqual(texts, ["hey_hal (wake)", "thats_all (outro)"])
        checked = [i.kw["checked"](i) for i in items]
        self.assertEqual(checked, [True, False])

    def test_toggle_saved_phrase_flips_saves_and_bounces_listener(self):
        self.app.cfg["wake_phrases"] = {
            "hey_hal": {"path": "C:/p/hey_hal.onnx", "role": "wake",
                        "active": False}}
        self.app.wake_listener = types.SimpleNamespace(
            stop=mock.Mock(), start=mock.Mock())
        with mock.patch.object(config, "save") as save:
            self.menu._toggle_saved_phrase("hey_hal")
        self.assertTrue(self.app.cfg["wake_phrases"]["hey_hal"]["active"])
        self.app.wake_listener.stop.assert_called_once()
        self.app.wake_listener.start.assert_called_once()
        save.assert_called_once()

    def test_menu_shows_meeting_auto_record_section(self):
        menu = self.menu.build()
        texts = " | ".join(_iter_texts(menu))
        self.assertIn("Meeting Auto-Record", texts)
        self.assertIn("Detect apps...", texts)
        self.assertIn("zoom\trecord", texts)
        self.assertIn("discord\task", texts)
        self.assertIn("Show toasts", texts)

    def test_set_app_record_state_saves_the_map(self):
        from whisper_sync.meeting_watch import apps_map
        with mock.patch.object(config, "save") as save:
            self.menu._set_app_record_state("discord.exe", "ignore")
        self.assertEqual(apps_map(self.app.cfg)["discord.exe"], "ignore")
        save.assert_called_once()

    def test_detect_adds_new_apps_as_ignore_only(self):
        from whisper_sync import tray_menu as tray_menu_mod
        from whisper_sync.meeting_watch import apps_map
        entries = {
            "c:#program files#zoom#bin#zoom.exe": False,   # already covered
            "c:#program files#obs#obs64.exe": True,        # new
            "microsoft.windowssoundrecorder_8wek": False,  # new (packaged)
        }
        with mock.patch("whisper_sync.meeting_watch.read_mic_entries",
                        return_value=entries), \
                mock.patch.object(config, "save"), \
                mock.patch.object(tray_menu_mod, "notify"):
            self.menu._detect_auto_record_apps()
        apps = apps_map(self.app.cfg)
        self.assertEqual(apps["obs64.exe"], "ignore")
        self.assertEqual(apps["microsoft.windowssoundrecorder_8wek"],
                         "ignore")
        # The existing zoom token still covers its entry: no duplicate.
        self.assertEqual(apps["zoom.exe"], "record")
        self.assertNotIn("c:#program files#zoom#bin#zoom.exe", apps)

    def test_menu_callback_swallows_pystray_args(self):
        calls = []
        cb = menu_callback(lambda a, b: calls.append((a, b)), 1, 2)
        cb(icon=object(), item=object())
        self.assertEqual(calls, [(1, 2)])


class GitHubTrayTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.dict(sys.modules, _MODULE_STUBS)
        p.start()
        self.addCleanup(p.stop)
        self.app = _FakeApp(Path("."))
        self.gh = GitHubTray(self.app)

    def test_menu_items_empty_without_repo(self):
        self.app.cfg["github_repo"] = ""
        self.assertEqual(self.gh.menu_items(), [])

    def test_menu_items_empty_without_poller(self):
        self.app.cfg["github_repo"] = "owner/repo"
        self.assertEqual(self.gh.menu_items(), [])

    def test_stop_without_poller_is_safe(self):
        self.gh.stop()
        self.gh.poll_now()

    def test_merge_pr_notifies_on_success(self):
        ok = types.SimpleNamespace(returncode=0, stdout="", stderr="")
        with mock.patch("subprocess.run", return_value=ok), \
                mock.patch("whisper_sync.github_tray.notify") as notify:
            self.gh._merge_pr("owner/repo", 42)
        self.assertIn("merged", notify.call_args[0][0].lower())


class FlashControllerTests(unittest.TestCase):
    def test_yellow_flash_gate_blocks_reentry(self):
        from whisper_sync import icons
        started = []
        fake_animator = types.SimpleNamespace(
            flash=lambda **kw: started.append("flash"),
            flash_between=lambda *a, **kw: started.append("between"))
        with mock.patch.object(icons, "IconAnimator", return_value=fake_animator), \
                mock.patch.object(icons.scheduler, "call_later") as later:
            fc = icons.FlashController(lambda: None, threading.Lock())
            fc.yellow()
            fc.yellow()  # gated: second call must be a no-op
            self.assertEqual(started, ["flash"])
            later.assert_called_once()
            # After the scheduled clear runs, the gate re-arms.
            later.call_args[0][1]()
            fc.yellow()
            self.assertEqual(started, ["flash", "flash"])

    def test_queued_flash_is_ungated(self):
        from whisper_sync import icons
        started = []
        fake_animator = types.SimpleNamespace(
            flash_between=lambda *a, **kw: started.append("between"))
        with mock.patch.object(icons, "IconAnimator", return_value=fake_animator):
            fc = icons.FlashController(lambda: None, threading.Lock())
            fc.queued()
            fc.queued()
        self.assertEqual(started, ["between", "between"])


if __name__ == "__main__":
    unittest.main()

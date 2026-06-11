"""Tests for whisper_sync.config_store - lock-guarded config Mapping.

The bug class this closes: the bare cfg dict was read by the tray menu,
hotkey handlers, and pipeline threads while settings handlers wrote to
it. ``config.save()`` could hit "dictionary changed size during
iteration" and persist a torn view; nested hotkey writes were visible
half-applied.
"""

import pickle
import threading
import unittest

from whisper_sync.config_store import ConfigStore


def _store():
    return ConfigStore({
        "model": "large-v3",
        "paste_method": "keystrokes",
        "hotkeys": {"dictation_toggle": "ctrl+shift+space",
                    "meeting_toggle": "ctrl+shift+m"},
    })


class ConfigStoreMappingTests(unittest.TestCase):
    def test_existing_read_idioms_work_unchanged(self):
        cfg = _store()
        self.assertEqual(cfg["model"], "large-v3")
        self.assertEqual(cfg.get("missing", "fallback"), "fallback")
        self.assertIn("paste_method", cfg)
        self.assertEqual(cfg["hotkeys"]["meeting_toggle"], "ctrl+shift+m")
        # Unpacking idioms used by backup_worker and worker_manager.
        self.assertEqual({**cfg}["model"], "large-v3")
        self.assertEqual(dict(cfg)["paste_method"], "keystrokes")
        self.assertEqual(len(cfg), 3)
        self.assertEqual(set(cfg.keys()), {"model", "paste_method", "hotkeys"})

    def test_setitem_and_readback(self):
        cfg = _store()
        cfg["model"] = "base"
        self.assertEqual(cfg["model"], "base")

    def test_read_values_are_copies_not_live_references(self):
        # Regression for Copilot review on PR #149: handing out the live
        # inner dict/list would let callers mutate shared state without
        # the lock. Reads of mutable containers must be isolated copies.
        cfg = _store()
        cfg["toast_events"] = ["a", "b"]
        cfg["toast_events"].append("c")          # mutating a read copy...
        self.assertEqual(cfg["toast_events"], ["a", "b"])  # ...never writes back
        hk = cfg["hotkeys"]
        hk["dictation_toggle"] = "tampered"
        self.assertEqual(cfg["hotkeys"]["dictation_toggle"], "ctrl+shift+space")

    def test_store_does_not_alias_initial_dict(self):
        initial = {"hotkeys": {"dictation_toggle": "a"}}
        cfg = ConfigStore(initial)
        initial["hotkeys"]["dictation_toggle"] = "mutated"
        self.assertEqual(cfg["hotkeys"]["dictation_toggle"], "a")


class SetNestedTests(unittest.TestCase):
    def test_set_nested_updates_value(self):
        cfg = _store()
        cfg.set_nested("hotkeys", "dictation_toggle", "f13")
        self.assertEqual(cfg["hotkeys"]["dictation_toggle"], "f13")
        self.assertEqual(cfg["hotkeys"]["meeting_toggle"], "ctrl+shift+m")

    def test_set_nested_is_copy_on_write(self):
        # A reader holding the old inner dict must keep a complete stale
        # value, never see a half-applied write.
        cfg = _store()
        before = cfg["hotkeys"]
        cfg.set_nested("hotkeys", "dictation_toggle", "f13")
        self.assertEqual(before["dictation_toggle"], "ctrl+shift+space")
        self.assertIsNot(cfg["hotkeys"], before)

    def test_set_nested_rejects_non_dict_target(self):
        cfg = _store()
        with self.assertRaises(TypeError):
            cfg.set_nested("model", "x", 1)


class SnapshotTests(unittest.TestCase):
    def test_snapshot_is_deep_and_isolated(self):
        cfg = _store()
        snap = cfg.snapshot()
        snap["model"] = "tampered"
        snap["hotkeys"]["dictation_toggle"] = "tampered"
        self.assertEqual(cfg["model"], "large-v3")
        self.assertEqual(cfg["hotkeys"]["dictation_toggle"], "ctrl+shift+space")

    def test_store_refuses_pickle_snapshot_allows_it(self):
        # The store holds a lock; passing it to multiprocessing must fail
        # loudly, and the documented alternative must work.
        cfg = _store()
        with self.assertRaises(TypeError):
            pickle.dumps(cfg)
        restored = pickle.loads(pickle.dumps(cfg.snapshot()))
        self.assertEqual(restored["model"], "large-v3")


class ConcurrencyTests(unittest.TestCase):
    def test_snapshot_never_tears_under_concurrent_writes(self):
        # Writers flip two keys together; every snapshot must see them in
        # agreement (the torn-save bug this store exists to prevent).
        cfg = ConfigStore({"a": 0, "b": 0, "hotkeys": {}})
        stop = threading.Event()
        torn = []

        def _writer():
            i = 0
            while not stop.is_set():
                i += 1
                with cfg.transaction():  # paired write, as the diarize swap does
                    cfg["a"] = i
                    cfg["b"] = i

        def _reader():
            while not stop.is_set():
                snap = cfg.snapshot()
                if snap["a"] != snap["b"]:
                    torn.append(snap)
                    return

        w = threading.Thread(target=_writer, daemon=True)
        readers = [threading.Thread(target=_reader, daemon=True) for _ in range(4)]
        w.start()
        for r in readers:
            r.start()
        threading.Event().wait(0.5)
        stop.set()
        w.join(timeout=2.0)
        for r in readers:
            r.join(timeout=2.0)
        self.assertEqual(torn, [], "snapshot must be atomic across keys")

    def test_concurrent_writers_and_iterators_do_not_crash(self):
        # The classic failure: json.dump iterating the dict while another
        # thread adds keys raises RuntimeError. The store's iteration is
        # taken under the lock, so this must be exception-free.
        cfg = ConfigStore({"seed": 0, "hotkeys": {}})
        stop = threading.Event()
        errors = []

        def _writer(tag):
            i = 0
            while not stop.is_set():
                i += 1
                cfg[f"key_{tag}_{i % 50}"] = i
                cfg.set_nested("hotkeys", f"hk_{tag}", i)

        def _iterator():
            while not stop.is_set():
                try:
                    dict(cfg)
                    {**cfg}
                    list(cfg.items())
                except RuntimeError as e:
                    errors.append(e)
                    return

        threads = [threading.Thread(target=_writer, args=(t,), daemon=True)
                   for t in range(3)]
        threads += [threading.Thread(target=_iterator, daemon=True)
                    for _ in range(3)]
        for t in threads:
            t.start()
        threading.Event().wait(0.5)
        stop.set()
        for t in threads:
            t.join(timeout=2.0)
        self.assertEqual(errors, [], "iteration must never race writers")


if __name__ == "__main__":
    unittest.main()

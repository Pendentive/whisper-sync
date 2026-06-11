"""Tests for the DialogDispatcher persistent Tk root (stability Phase 3).

Uses a fake tk factory so no real tkinter is needed. The contract under
test: ONE root, created lazily on the dispatcher thread, passed to every
wants_root dialog, destroyed exactly once on the dispatcher thread at
shutdown — never left for interpreter-exit GC (the access-violation crash
family in the May-June 2026 production logs).
"""

import threading
import unittest

from whisper_sync.dialog_dispatcher import DialogDispatcher


class _FakeRoot:
    def __init__(self):
        self.created_on = threading.current_thread().name
        self.destroyed = 0
        self.destroyed_on = None

    def destroy(self):
        self.destroyed += 1
        self.destroyed_on = threading.current_thread().name


class DialogDispatcherRootTests(unittest.TestCase):
    def setUp(self):
        self.roots: list[_FakeRoot] = []

        def _factory():
            r = _FakeRoot()
            self.roots.append(r)
            return r

        self.d = DialogDispatcher(name="test-dispatcher", tk_factory=_factory)
        self.d.start()

    def tearDown(self):
        self.d.shutdown(timeout=2.0)

    def test_root_created_lazily_and_reused_across_dialogs(self):
        self.assertEqual(self.roots, [], "root must not exist before first dialog")

        seen = []
        for i in range(3):
            self.d.run(lambda root: seen.append(root), label=f"dlg{i}", wants_root=True)

        self.assertEqual(len(self.roots), 1, "exactly ONE root for many dialogs")
        self.assertTrue(all(r is self.roots[0] for r in seen),
                        "every dialog must receive the same persistent root")

    def test_root_created_and_destroyed_on_dispatcher_thread(self):
        self.d.run(lambda root: None, label="dlg", wants_root=True)
        root = self.roots[0]
        self.assertEqual(root.created_on, "test-dispatcher")

        self.d.shutdown(timeout=2.0)
        self.assertEqual(root.destroyed, 1, "root destroyed exactly once at shutdown")
        self.assertEqual(
            root.destroyed_on, "test-dispatcher",
            "root must die on ITS OWN thread, never via GC elsewhere",
        )

    def test_shutdown_without_any_dialog_destroys_nothing(self):
        self.d.shutdown(timeout=2.0)
        self.assertEqual(self.roots, [], "no root was created, none to destroy")

    def test_legacy_no_root_callable_still_works(self):
        out = self.d.run(lambda: "legacy-result", label="legacy")
        self.assertEqual(out, "legacy-result")
        self.assertEqual(self.roots, [], "legacy dialogs must not force a root")

    def test_dialog_exception_propagates_and_root_survives(self):
        self.d.run(lambda root: None, label="ok", wants_root=True)

        def _boom(root):
            raise RuntimeError("dialog blew up")

        with self.assertRaises(RuntimeError):
            self.d.run(_boom, label="boom", wants_root=True)

        root = self.roots[0]
        self.assertEqual(root.destroyed, 0, "a crashing dialog must not kill the root")
        # And the dispatcher keeps serving dialogs with the same root.
        seen = []
        self.d.run(lambda r: seen.append(r), label="after-boom", wants_root=True)
        self.assertIs(seen[0], root)

    def test_reentrant_wants_root_call_runs_inline_with_same_root(self):
        outer_inner = []

        def _outer(root):
            # Nested dialog request from within a dialog (same thread) must
            # run inline with the SAME root, not deadlock.
            inner = self.d.run(lambda r: r, label="inner", wants_root=True)
            outer_inner.append((root, inner))

        self.d.run(_outer, label="outer", wants_root=True)
        outer_root, inner_root = outer_inner[0]
        self.assertIs(outer_root, inner_root)

    def test_timed_out_shutdown_keeps_dispatcher_state(self):
        # Regression for Copilot review on PR #142: if shutdown's join
        # times out (dialog still open), state must stay intact so a later
        # start() cannot spawn a SECOND thread that would reuse the Tk
        # root across threads.
        gate = threading.Event()
        entered = threading.Event()

        def _blocking_dialog(root):
            entered.set()
            gate.wait(timeout=10.0)

        # run() blocks until the dialog completes, so submit from a helper.
        t = threading.Thread(
            target=lambda: self.d.run(
                _blocking_dialog, label="stuck", wants_root=True
            ),
            daemon=True,
        )
        t.start()
        self.assertTrue(entered.wait(timeout=2.0))

        # Shutdown with a tiny timeout: join times out, state must hold.
        self.d.shutdown(timeout=0.05)
        self.assertTrue(self.d._started, "timed-out shutdown must keep state")
        first_thread = self.d._thread
        self.assertIsNotNone(first_thread)

        # start() while the original thread lives must NOT spawn another.
        self.d.start()
        self.assertIs(self.d._thread, first_thread)

        # Release the dialog; the sentinel already queued by the first
        # shutdown lets the loop exit; a second shutdown clears state.
        gate.set()
        t.join(timeout=2.0)
        self.d.shutdown(timeout=2.0)
        self.assertFalse(self.d._started)

    def test_root_factory_failure_propagates_but_dispatcher_survives(self):
        calls = []

        def _flaky_factory():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("tk init failed")
            r = _FakeRoot()
            self.roots.append(r)
            return r

        d = DialogDispatcher(name="flaky-dispatcher", tk_factory=_flaky_factory)
        d.start()
        try:
            with self.assertRaises(RuntimeError):
                d.run(lambda root: None, label="first", wants_root=True)
            # Second attempt: factory works, dispatcher recovered.
            seen = []
            d.run(lambda root: seen.append(root), label="second", wants_root=True)
            self.assertEqual(len(seen), 1)
        finally:
            d.shutdown(timeout=2.0)


if __name__ == "__main__":
    unittest.main()

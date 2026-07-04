"""Update, restart, and quit lifecycle - extracted from __main__.py.

Hardening round item 6 (architecture spec A2), extraction 4 (final).
AppControl owns the self-update flow (git fetch/checkout/pull against
the repo checkout the tray app runs from), the shared shutdown
sequence, and the deferred restart/quit paths (pystray menu callbacks
run inside the Win32 message pump; tray.stop() posts WM_QUIT but the
pump cannot process it until the callback returns, hence the
background thread + defer).

The old ``_updating`` class attribute is folded into
``AppState.updating`` with UPDATE_STARTED / UPDATE_COMPLETED events -
the last stray mode flag from the architecture audit.
"""

import os
import sys
import threading
from pathlib import Path

from . import lifecycle
from . import weekly_stats
from .logger import logger
from .notifications import notify
from .paths import get_install_root
from .state_manager import UPDATE_STARTED, UPDATE_COMPLETED


class AppControl:
    """Owns update/restart/quit; reads services through ``app``."""

    def __init__(self, app):
        self.app = app

    def update(self, branch="dev"):
        """Pull latest code from a branch and restart if updated.

        The old _updating guard flag is folded into AppState.updating
        (hardening item 6): same check-then-set semantics, but the whole
        app mode is now inspectable in one snapshot.
        """
        state = self.app.state
        if state is None or state.current.updating:
            logger.debug("Update already in progress (or app not running), ignoring")
            return
        state.emit(UPDATE_STARTED, updating=True)

        def _do_update():
            import subprocess as _sp
            from .executors import native_call
            repo_root = str(get_install_root())

            # One native-call span for the whole git sequence; over-marking
            # is safe (idle GC just skips a tick), under-marking is not.
            try:
                with native_call("git-update"):
                    self._run_update_steps(_sp, repo_root, branch)
            except FileNotFoundError:
                logger.error("git not found on PATH")
                notify("Update failed", "git not found, check installation")
            except _sp.TimeoutExpired:
                logger.error("git command timed out during update")
                notify("Update failed", "git timed out, check network")
            except Exception as e:
                logger.error(f"Update failed: {e}")
                notify("Update failed", "Unexpected error, check console")
            finally:
                state.emit(UPDATE_COMPLETED, updating=False)

        threading.Thread(target=_do_update, daemon=True).start()

    def _run_update_steps(self, _sp, repo_root: str, branch: str):
        """Run the git fetch/checkout/pull sequence for self-update."""
        notify("Updating WhisperSync...", f"Pulling latest from {branch}")

        # Check for uncommitted changes
        status = _sp.run(
            ["git", "status", "--porcelain"],
            cwd=repo_root, capture_output=True, text=True, timeout=10
        )
        if status.stdout.strip():
            logger.warning(f"Uncommitted changes detected:\n{status.stdout.strip()}")

        # Fetch
        fetch = _sp.run(
            ["git", "fetch", "origin", branch],
            cwd=repo_root, capture_output=True, text=True, timeout=30
        )
        if fetch.returncode != 0:
            logger.error(f"git fetch failed: {fetch.stderr}")
            notify("Update failed", "git fetch failed, check console")
            return

        # Check if there are updates
        diff = _sp.run(
            ["git", "rev-list", f"HEAD..origin/{branch}", "--count"],
            cwd=repo_root, capture_output=True, text=True, timeout=10
        )
        count_str = (diff.stdout or "").strip()
        commit_count = int(count_str) if count_str.isdigit() else 0

        if commit_count == 0:
            notify("Already up to date", f"No new changes on {branch}")
            return

        # Checkout the target branch if not already on it
        current = _sp.run(
            ["git", "branch", "--show-current"],
            cwd=repo_root, capture_output=True, text=True, timeout=10
        )
        if current.stdout.strip() != branch:
            checkout = _sp.run(
                ["git", "checkout", branch],
                cwd=repo_root, capture_output=True, text=True, timeout=15
            )
            if checkout.returncode != 0:
                logger.error(f"git checkout {branch} failed: {checkout.stderr}")
                notify("Update failed", f"Could not switch to {branch}")
                return

        pull = _sp.run(
            ["git", "pull", "origin", branch],
            cwd=repo_root, capture_output=True, text=True, timeout=60
        )
        if pull.returncode != 0:
            logger.error(f"git pull failed: {pull.stderr}")
            notify("Update failed", "git pull failed, check console")
            return

        logger.info(f"Updated from {branch}: {commit_count} new commit(s)")
        notify("Updated!", f"{commit_count} commit(s) from {branch}. Restarting...")

        import time
        time.sleep(2)  # Let the notification display
        self.restart()

    # Empirically chosen delay (seconds) to let pystray menu callbacks
    # return before we call tray.stop(). Without this, WM_QUIT is posted
    # but can't be processed while the callback is still on the stack.
    _CALLBACK_DEFER_SECS = 0.3

    def _cleanup(self):
        """Shared shutdown sequence for restart and quit."""
        try:
            weekly_stats.flush()
        except Exception:
            logger.debug("Stats flush failed during shutdown", exc_info=True)
        # Signal post-processing worker to shut down
        try:
            self.app.meetings.shutdown_post_worker()
        except Exception:
            logger.debug("Post-queue shutdown signal failed", exc_info=True)
        try:
            if self.app.recorder.is_recording:
                self.app.recorder.stop()
            self.app.worker.stop()
            self.app._backup.stop()
            # Lazy import last: keyboard is absent on the CI system
            # python, and an ImportError here must never skip the
            # recorder/worker/backup stops above.
            import keyboard
            keyboard.unhook_all()
        except Exception:
            logger.debug("Cleanup error during shutdown", exc_info=True)

    def restart(self):
        """Restart WhisperSync by cleaning up, stopping tray, then spawning.

        Deferred to a background thread because pystray menu callbacks
        run inside the Win32 message pump. tray.stop() posts WM_QUIT
        but can't process it until the callback returns.
        """
        def _do_restart():
            import subprocess
            import time
            lifecycle.record_exit_reason(lifecycle.REASON_USER_RESTART)
            time.sleep(self._CALLBACK_DEFER_SECS)
            self._cleanup()
            # Spawn new process first so it starts loading immediately
            subprocess.Popen(
                [sys.executable, "-m", "whisper_sync"],
                cwd=str(Path(__file__).parent.parent),
            )
            # Stop tray icon, then force-exit. os._exit() is needed because
            # daemon threads (worker subprocesses, keyboard listener, etc.)
            # can keep the process alive after tray.stop() returns.
            if self.app.tray:
                self.app.tray.stop()
            time.sleep(0.2)
            lifecycle.log_exit_banner(logger)
            os._exit(0)

        threading.Thread(target=_do_restart, daemon=True).start()

    def quit(self):
        """Quit WhisperSync. Deferred like _restart for the same reason."""
        def _do_quit():
            import time
            lifecycle.record_exit_reason(lifecycle.REASON_USER_QUIT)
            time.sleep(self._CALLBACK_DEFER_SECS)
            self._cleanup()
            if self.app.tray:
                self.app.tray.stop()
            # Explicit banner for symmetry with _restart. In theory the
            # atexit hook emits one when the interpreter shuts down, but
            # daemon threads keeping the interpreter alive can swallow
            # atexit; better to log here and let atexit be a no-op via
            # the first-wins rule in record_exit_reason.
            lifecycle.log_exit_banner(logger)
        threading.Thread(target=_do_quit, daemon=True).start()

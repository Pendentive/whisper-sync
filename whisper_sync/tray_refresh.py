"""Debounced, single-owner tray menu refresh.

pystray is not thread-safe, and menu rebuilds are expensive: _build_menu
allocates hundreds of MenuItem objects with closures. Historically,
_refresh_menu() assigned ``tray.menu`` inline from whatever thread wanted
a refresh (dictation workers, overlay threads, the GitHub poller, recovery
threads). That raced the Win32 message pump reading the menu — the
2026-05-07 15:10 crash trace is exactly ``_build_menu ← _refresh_menu ←
_process_overlay`` corrupting the heap mid-MenuItem-construction.

MenuRefresher fixes both problems:

- **Single owner**: the rebuild runs on the shared scheduler thread, and
  the swap is applied through a caller-supplied ``apply_menu`` callback
  that takes the tray lock. Arbitrary threads only ever call
  ``request()``, which is lock-cheap and never touches pystray.
- **Debounce**: N requests within the window coalesce into ONE rebuild.
  Menu refreshes happen after every dictation, every github poll, every
  config change; bursts (e.g. dictation completion triggering history +
  stats + state updates) previously rebuilt the menu several times
  back-to-back.
"""

from __future__ import annotations

import threading
from typing import Callable

from .logger import logger
from .scheduler import scheduler as _default_scheduler


class MenuRefresher:
    """Coalesces menu refresh requests into debounced single rebuilds."""

    def __init__(
        self,
        build_menu: Callable[[], object],
        apply_menu: Callable[[object], None],
        debounce_s: float = 0.3,
        sched=None,
    ):
        """
        Args:
            build_menu: builds and returns a new menu object. Runs on the
                scheduler thread.
            apply_menu: applies the built menu to the tray (must take the
                tray lock internally). Runs on the scheduler thread.
            debounce_s: coalescing window.
            sched: scheduler override for tests; defaults to the shared
                process scheduler.
        """
        self._build_menu = build_menu
        self._apply_menu = apply_menu
        self._debounce_s = debounce_s
        self._sched = sched if sched is not None else _default_scheduler
        self._lock = threading.Lock()
        self._pending = False
        # Telemetry for tests/forensics
        self.rebuild_count = 0

    def request(self) -> None:
        """Request a menu refresh. Safe from any thread; coalesces bursts."""
        with self._lock:
            if self._pending:
                return
            self._pending = True
        self._sched.call_later(self._debounce_s, self._run, label="menu-refresh")

    def _run(self) -> None:
        # Clear the pending flag FIRST so a request arriving during the
        # rebuild schedules a fresh pass (it may carry newer state).
        with self._lock:
            self._pending = False
        try:
            menu = self._build_menu()
        except Exception:
            logger.exception("menu rebuild failed")
            return
        try:
            self._apply_menu(menu)
            self.rebuild_count += 1
        except Exception:
            logger.exception("menu apply failed")

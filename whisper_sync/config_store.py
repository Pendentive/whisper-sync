"""Thread-safe configuration store.

The app previously shared one bare cfg dict across the tray menu thread,
hotkey handlers, the scheduler, executor workers, and the meeting
pipeline (stability rebuild plan, Phase 5). Writes raced with reads, and
``config.save()`` iterated the dict while other threads mutated it, which
can raise "dictionary changed size during iteration" and persist a
half-updated view.

``ConfigStore`` is a ``Mapping`` over the config data, so every existing
read site (``cfg["model"]``, ``cfg.get(...)``, ``{**cfg}``, ``dict(cfg)``)
keeps working unchanged. Writes go through ``__setitem__``/``set_nested``
under a lock, and ``snapshot()`` returns an atomic deep copy for
persistence and for crossing process boundaries (a ConfigStore holds a
lock and must not be pickled; pass ``snapshot()`` to subprocesses).

Nested updates are copy-on-write: ``set_nested("hotkeys", k, v)``
replaces the inner dict rather than mutating it, so a concurrent reader
holding the old inner dict sees a complete (if stale) value, never a
half-written one.
"""

from __future__ import annotations

import copy
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager


class ConfigStore(Mapping):
    """Lock-guarded Mapping over the app configuration."""

    def __init__(self, initial: Mapping):
        self._lock = threading.RLock()
        self._data: dict = copy.deepcopy(dict(initial))

    # -- reads (Mapping interface: get/keys/items/values/contains derive) ----

    def __getitem__(self, key):
        with self._lock:
            return self._data[key]

    def __iter__(self) -> Iterator:
        with self._lock:
            return iter(tuple(self._data))

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def __repr__(self) -> str:
        with self._lock:
            return f"ConfigStore({self._data!r})"

    # -- writes ---------------------------------------------------------------

    def __setitem__(self, key, value) -> None:
        with self._lock:
            self._data[key] = value

    def set_nested(self, key, subkey, value) -> None:
        """Set ``cfg[key][subkey] = value`` without mutating the inner dict.

        Copy-on-write: readers that already fetched ``cfg[key]`` keep a
        complete stale dict; the store swaps in a fresh one atomically.
        """
        with self._lock:
            inner = self._data.get(key)
            if not isinstance(inner, dict):
                raise TypeError(
                    f"set_nested requires cfg[{key!r}] to be a dict, "
                    f"got {type(inner).__name__}"
                )
            replacement = dict(inner)
            replacement[subkey] = value
            self._data[key] = replacement

    @contextmanager
    def transaction(self):
        """Hold the lock across a multi-key read-modify-write.

        Use for compound updates that must be atomic as a group, e.g.
        swapping two diarization slots. The lock is reentrant, so normal
        reads and writes on the store work inside the block.
        """
        with self._lock:
            yield self

    # -- snapshots --------------------------------------------------------------

    def snapshot(self) -> dict:
        """Atomic deep copy, safe to persist, iterate, or pickle."""
        with self._lock:
            return copy.deepcopy(self._data)

    def __reduce__(self):
        raise TypeError(
            "ConfigStore holds a lock and must not be pickled; "
            "pass snapshot() across process boundaries instead"
        )

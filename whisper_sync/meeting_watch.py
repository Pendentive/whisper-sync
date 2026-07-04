"""Per-app meeting auto-record - assistant build round, step 4.

When a configured communications app (Zoom, Slack, Teams, ...) starts
using the microphone, a meeting recording auto-starts; when that app
releases the mic and stays quiet, the auto-started recording stops
(through the normal stop path, save dialog included). Manual hotkey
control is untouched, and a manually started recording is never
auto-stopped.

Detection source: the Windows CapabilityAccessManager consent store
(HKCU ConsentStore/microphone) - the registry tree behind the OS
mic-in-use indicator. Desktop apps live under ``NonPackaged`` keyed by
exe path ('#' separators); MSIX apps sit directly under ``microphone``
keyed by package family name. Each entry carries LastUsedTimeStart /
LastUsedTimeStop FILETIMEs, and ``LastUsedTimeStop == 0`` means the app
holds the mic RIGHT NOW. Reading it is stdlib winreg - no COM, no new
dependencies - and it is capture-specific: an app merely playing audio
(a notification sound) can never look like a call. This deliberately
replaces the WASAPI session enumeration sketched in the direction spec
(which would have needed pycaw/comtypes and sees render sessions).

Stale-entry hazard (observed on the dev machine): an app version that
died mid-call leaves its entry active forever (old Slack app-x.y.z
directories). The watcher therefore acts ONLY on transitions it
observes between polls: an entry already active when watching begins
never triggers, and per-ENTRY tracking means a stale sibling entry
cannot mask the live app version's entry.

Feature pattern (gpu_guard precedent): one owning module, flat config
keys (meeting_auto_record, meeting_watch_apps,
meeting_watch_poll_seconds, meeting_watch_stop_after_s), inert when
disabled or off-Windows.
"""

from __future__ import annotations

import math
import os

from .logger import logger
from .notifications import notify

_CONSENT_ROOT = (
    r"SOFTWARE\Microsoft\Windows\CurrentVersion"
    r"\CapabilityAccessManager\ConsentStore\microphone"
)

# Consecutive active polls before an observed transition triggers a
# recording - filters sub-poll blips like a mic permission check.
TRIGGER_AFTER_POLLS = 2


def read_mic_entries() -> dict[str, bool] | None:
    """The consent store as {entry_id: holds_mic_now}.

    entry_id is the NonPackaged exe path (with '#' separators, as
    stored) or the package family name, lowercased. Entries that never
    used the mic (no timestamps) are omitted. Returns None when the
    store is unreadable (non-Windows or registry error) - callers must
    treat that as "no information", never as "everything released".
    """
    if os.name != "nt":
        return None
    import winreg

    entries: dict[str, bool] = {}
    try:
        _scan_tree(winreg, _CONSENT_ROOT, entries, skip={"nonpackaged"})
        _scan_tree(winreg, _CONSENT_ROOT + r"\NonPackaged", entries, skip=set())
    except OSError as exc:
        logger.debug(f"meeting watch: consent store unreadable: {exc}")
        return None
    return entries


def _scan_tree(winreg, path: str, out: dict, skip: set) -> None:
    """Collect {entry_id: active} from one consent-store tree."""
    try:
        root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, path)
    except FileNotFoundError:
        return
    with root:
        index = 0
        while True:
            try:
                sub = winreg.EnumKey(root, index)
            except OSError:
                break  # end of enumeration
            index += 1
            if sub.lower() in skip:
                continue
            try:
                with winreg.OpenKey(root, sub) as key:
                    start, _ = winreg.QueryValueEx(key, "LastUsedTimeStart")
                    stop, _ = winreg.QueryValueEx(key, "LastUsedTimeStop")
            except OSError:
                continue  # no usage values recorded for this app
            if start:
                out[sub.lower()] = stop == 0


def entry_matches(entry_id: str, app_names) -> bool:
    """True when a consent-store entry belongs to a watched app.

    Desktop entries are full '#'-separated paths: a configured
    ``*.exe`` token must match the path LEAF exactly (never a
    substring - '#zoom.exe#helper.exe' belongs to helper.exe).
    Packaged entries are package family names (MSTeams_8wekyb...): a
    token without '.exe' matches as a case-insensitive prefix.
    """
    entry = entry_id.lower()
    for name in app_names or ():
        token = str(name).strip().lower()
        if not token:
            continue
        if token.endswith(".exe"):
            if entry.rsplit("#", 1)[-1] == token:
                return True
        elif entry.startswith(token):
            return True
    return False


def _label(entry_id: str) -> str:
    """Human-readable app name for toasts/logs (exe leaf or family)."""
    return entry_id.rsplit("#", 1)[-1]


class MeetingWatch:
    """Owns per-app auto-record; reads services through ``app``."""

    def __init__(self, app, read_entries=read_mic_entries):
        self.app = app
        self._read_entries = read_entries
        self._prev: dict[str, bool] = {}
        self._streak: dict[str, int] = {}  # entry -> active polls observed
        self._handled: set[str] = set()    # entries whose call is dealt with
        self._auto_started_by: str | None = None
        self._release_polls = 0
        self._primed = False  # first enabled poll only seeds the baseline

    # -- Wiring (called from run()) -------------------------------------------

    def start(self, scheduler, io_executor) -> None:
        """Register the poll tick. Runs even while disabled (the check
        is a dict lookup); enabling from the tray needs no restart."""
        poll = float(self.app.cfg.get("meeting_watch_poll_seconds", 5))
        # Registry scans are milliseconds, but keep the scheduler tick
        # to a pure enqueue (short-jobs contract, gpu-guard precedent).
        scheduler.call_every(
            poll,
            lambda: io_executor.submit_native("meeting-watch", self.check_once),
            label="meeting-watch",
        )
        enabled = bool(self.app.cfg.get("meeting_auto_record", False))
        logger.info(
            f"Meeting watch registered (poll={poll:.0f}s, "
            f"auto-record {'ON' if enabled else 'off'})"
        )

    # -- Poll ------------------------------------------------------------------

    def check_once(self) -> None:
        """One consent-store poll. Runs on the IO executor."""
        try:
            self._check()
        except Exception:
            logger.debug("meeting watch check failed", exc_info=True)

    def _check(self) -> None:
        cfg = self.app.cfg
        if not cfg.get("meeting_auto_record", False):
            if self._primed:
                # Reset so re-enabling re-seeds the baseline instead of
                # treating everything that changed meanwhile as fresh.
                self._prev.clear()
                self._streak.clear()
                self._handled.clear()
                self._auto_started_by = None
                self._release_polls = 0
                self._primed = False
            return
        entries = self._read_entries()
        if entries is None:
            return
        apps = cfg.get("meeting_watch_apps") or []
        if not self._primed:
            # Baseline over ALL entries, not just watched ones: an entry
            # already active now may be a stale leftover from a dead app
            # version, and it must stay baseline even if the watch list
            # is edited later to include it (review catch). An entry that
            # APPEARS later is a real acquire - the consent store only
            # creates entries on actual mic use.
            self._prev = entries
            self._primed = True
            return
        for eid, active in entries.items():
            if not active:
                self._streak.pop(eid, None)
                self._handled.discard(eid)
                continue
            if not self._prev.get(eid, False) and eid not in self._streak:
                self._streak[eid] = 1  # observed transition (or appearance)
            elif eid in self._streak and self._streak[eid] < TRIGGER_AFTER_POLLS:
                self._streak[eid] += 1
            if (self._streak.get(eid, 0) >= TRIGGER_AFTER_POLLS
                    and eid not in self._handled
                    and entry_matches(eid, apps)):
                # Retried every poll while the mic stays held (review
                # catch): an app that is busy dictating at the debounce
                # moment must not lose the whole meeting.
                if self._maybe_start(eid):
                    self._handled.add(eid)
        self._prev = entries
        self._maybe_stop(entries)

    # -- Start / stop decisions -------------------------------------------------

    def _maybe_start(self, entry_id: str) -> bool:
        """Try to auto-start. True = this call is dealt with (started,
        or someone is already recording it); False = busy, retry next
        poll while the mic stays held."""
        label = _label(entry_id)
        with self.app._lock:
            current = self.app.state.current if self.app.state else None
            mode = current.mode if current else None
            if mode == "meeting" or self.app.recorder.is_recording:
                return True  # already recording; that meeting is handled
            if not self.app._can_record():
                logger.info(
                    f"meeting watch: {label} is in a call but the app is "
                    "busy; retrying while the mic stays held")
                return False
            logger.info(
                f"meeting watch: {label} started using the mic; "
                "auto-starting the meeting recording")
            # toggle() re-enters the same RLock, wakes a sleeping model,
            # and claims the mode atomically (try_transition).
            self.app.meetings.toggle()
            self._auto_started_by = entry_id
            self._release_polls = 0
        notify("Meeting recording started",
               f"{label} is in a call. Stop with the meeting hotkey "
               "or the tray if unwanted.")
        return True

    def _maybe_stop(self, watched: dict) -> None:
        if self._auto_started_by is None:
            return
        with self.app._lock:
            current = self.app.state.current if self.app.state else None
            if (current.mode if current else None) != "meeting":
                # Stopped manually (or errored) - no longer ours to manage.
                self._auto_started_by = None
                self._release_polls = 0
                return
        if watched.get(self._auto_started_by, False):
            self._release_polls = 0
            return
        poll = max(float(self.app.cfg.get("meeting_watch_poll_seconds", 5)), 1.0)
        stop_after = float(self.app.cfg.get("meeting_watch_stop_after_s", 30))
        # Ceiling, never round: the release duration must be AT LEAST
        # stop_after (review catch: poll=7/stop=30 rounded to 28s).
        if self._release_polls + 1 < max(1, math.ceil(stop_after / poll)):
            self._release_polls += 1
            return
        entry = self._auto_started_by
        self._auto_started_by = None
        self._release_polls = 0
        label = _label(entry)
        with self.app._lock:
            current = self.app.state.current if self.app.state else None
            if (current.mode if current else None) != "meeting":
                return  # raced with a manual stop
            logger.info(
                f"meeting watch: {label} released the mic; stopping the "
                "auto-started recording")
            self.app.meetings.toggle()
        notify("Meeting recording stopped", f"{label} left the call.")

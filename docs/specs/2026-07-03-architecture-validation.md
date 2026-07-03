# Architecture Validation - 2026-07-03 findings and remediation spec

Status: FINDINGS RECORDED; remediation items PROPOSED
Source: repo-wide audit (2026-07-03) against the owner directive's
questions: is the state machine stable, is the app modular and
composable, is timeout-based coordination gone, is it all testable.

## Verdict summary

- **Concurrency primitives: healthy.** The 2026-05/06 stability rebuild
  landed single-owner primitives (scheduler, executors, config store,
  dialog dispatcher, worker protocol) and the hot dictation path uses
  them. Long-lived loops use cancellable Event.wait, not bare sleeps.
- **State machine: an event bus, not a machine.** StateManager.emit
  setattrs any field; there is no transition table; try_transition guards
  are caller-supplied and scattered. The real app mode is split across
  StateManager.mode plus at least four stray flags in __main__.py
  (_feature_suggest_active at 15+ sites, _updating, _flash_active, the
  recovery sets). Stable in practice after Phase 5a, but not validatable
  by inspection.
- **Modularity: one god-class.** __main__.py is 3,851 lines and owns
  hotkeys, menu, every workflow, recovery, updates, and wiring. All other
  modules are reasonable (installer_gui.py is 1,683 but a separate
  process).
- **Timeout residue: small and known.** Two real polling loops remain
  (GitHub first-poll sleep loop; installer), retry backoffs (file delete,
  watchdog cooldown), about five UI-timing sleeps around restart/quit/
  paste, and magic-number shutdown joins (2/3/5s) in the primitives.
- **Raw-thread bypasses: 6-7 remain** outside the documented by-design
  list: feature-format spawn, deep speaker-ID, meeting save+enqueue,
  first-poll waiter, a redundant thread wrapping the dialog dispatcher,
  dictation model reload, paste clipboard-restore.

## Remediation items (each one PR, in priority order)

### A1. Centralize the state machine
Define the mode set and legal transition table inside StateManager;
emit validates against it (warn-then-allow first, enforce after a soak
period). Fold _feature_suggest_active, _updating, and _flash_active into
AppState fields so the whole mode is inspectable in one place. Tests:
table-driven legal/illegal transition matrix.

### A2. Decompose __main__.py by workflow
Extract, in order of size and churn: dictation flow (incl. overlay),
meeting flow (start/stop/save/recovery), settings+menu construction,
update/restart/quit lifecycle. Target: __main__.py becomes wiring plus
hotkey routing under ~800 lines; each extracted module gets the unit
tests the god-class made impossible.

### A3. Finish the executor migration
Move the 6-7 listed bypass spawns onto IO/DICTATION or scheduler jobs
(the dialog-dispatcher double-spawn is a deletion). Convert the GitHub
first-poll sleep loop to a scheduler chain. Small PR, closes Phase 1b.

### A4. Timeout hygiene
Name the magic shutdown joins as module constants with rationale
comments; convert paste's sleep-thread restore to a scheduler job. The
UI-timing defers around pystray restart/quit stay (they exist for
pystray's benefit and are documented).

## Feature-flag note
There is no toggle mechanism beyond config keys read ad hoc. The GPU
Guard spec establishes the pattern (one owning module, flat config keys,
inert when disabled); future features should follow it rather than
adding scattered cfg.get branches.

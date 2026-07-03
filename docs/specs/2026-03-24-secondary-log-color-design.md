# Secondary Log Color Design

> Status: SHIPPED (2026-03; logger.py secondary color). Committed 2026-07-03 (previously an untracked draft).

> **Date**: 2026-03-24
> **Status**: Approved
> **Scope**: Color-code backup/secondary log lines in light purple
> **Issue**: #62

## Design

Add a `secondary` flag to log records. When set, the text portion renders in light purple (ANSI `\033[95m`) instead of white. This distinguishes backup/secondary subsystem operations from primary operations at a glance.

## Changes

### logger.py
- Add `COLOR_SECONDARY = "\033[95m"` constant
- In the formatter, check `getattr(record, "secondary", False)`. If True, use `COLOR_SECONDARY` for the text portion instead of white/default.

### backup_worker.py
- All `logger.*()` calls use `extra={"secondary": True}`

### __main__.py
- All overlay dictation log lines (in `_start_overlay_dictation`, `_stop_overlay_dictation`) use `extra={"secondary": True}`

## What stays the same
- Timestamps, [WhisperSync] prefix, and color coding for those elements are unchanged
- Primary log lines stay white
- All tiers affected equally (normal, detailed, verbose)
- File log handler is unaffected (no ANSI in file logs)

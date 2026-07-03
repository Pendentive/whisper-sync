# Spec: Hardware Resilience - device, power, and stall handling

Status: PROPOSED (derived from docs/specs/2026-07-03-owner-directive-verbatim.md)
Companion to the GPU Guard spec. This app talks directly to microphones,
speakers, and the GPU; the 2026-07-03 hardware audit found the hardware
failure modes are thinly handled compared to the software ones. Each item
below is independently shippable and feature-toggled where behavior
changes are user-visible.

## H1. Power/suspend-resume handling (highest crash relevance)
Today there is no WM_POWERBROADCAST handling anywhere: open PortAudio
streams, a live CUDA context, and in-flight transcription all run blindly
across suspend/resume. That transition is the most plausible moment for a
driver/TDR fault. Add a small message-window listener (pattern exists in
clipboard_thread.py): on suspend, checkpoint recordings (flush streaming
WAVs), pause streams, and quiesce the worker (no new requests); on
resume, reopen streams and verify the worker answers a ping, respawning
it if not. Log both events to the gpu-guard event log for crash
correlation.

## H2. Live audio device-change handling
A device that disappears mid-recording (USB unplug, Bluetooth drop) is
currently a silent drop: callbacks ignore the PortAudio status flags,
errors log once and suppress, and the user finds out only when stop()
returns nothing. Add: (a) inspect callback status and count aborted/
overflow flags; (b) a scheduler-based stall detector (no mic buffers for
N seconds while recording = device gone); on stall, notify the user and
attempt one stream reopen against the current default device; (c) the
same for the loopback stream, whose callback today is a bare
except-pass with no logging.

## H3. Overlay dictation disk-first
Normal dictation and meetings are disk-first (streaming WAV, orphan
recovery); overlay dictation (dictation during a meeting) is RAM-only
with no recovery path. Route it through the same start_streaming path so
a crash mid-overlay-dictation loses nothing. Closes the last audio-loss
gap and is a precondition for the GPU Guard downgrade handoff to be
fully safe.

## H4. Wedged-worker stall detection for meetings
Meeting transcription intentionally has no hard timeout (long meetings
are legitimate), but a worker that wedges while STAYING alive (classic
hung-driver symptom) hangs the post-process thread forever; only actual
process death is detected. Add a progress heartbeat to the worker
protocol: the worker emits a liveness ping every N seconds during long
jobs; the manager treats M missed pings as a wedge (kill, log,
downgrade-or-retry per GPU Guard policy). Preserves the
no-hard-timeout design decision (commit 4f3b307) while bounding hangs.

## H5. OOM handling by exception type + CPU floor
The worker's OOM retry matches "out of memory" strings on RuntimeError;
catch torch.cuda.OutOfMemoryError by type as well, and after retry
exhaustion hand the failure to the GPU Guard ladder (smaller model, then
CPU int8 if allowed) instead of re-raising to the caller.

## Test plan
Each item ships with unit tests against fake seams (fake power events,
fake callback status objects, fake worker pings) plus a manual-checklist
entry (unplug the mic mid-dictation; sleep the laptop mid-meeting). H3
extends the existing capture streaming tests; H4 extends
test_worker_protocol.py.

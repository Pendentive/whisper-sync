# Voice assistant direction - 2026-07-04 intake

Status: DIRECTION RECORDED; nothing beyond step 1 is committed.
Source: owner voice notes, 2026-07-04. This captures the intent and a
proposed staged architecture so future sessions design against it
instead of rediscovering it. The owner explicitly wants the always-on
ideas DEFINED before any build.

## The owner's intent (paraphrased)

WhisperSync is slowly evolving into a simple, mostly-offline voice
assistant:

1. Capture must never be blocked - "no matter what, when I dictate, it
   always works" (shipped as step 1, see below).
2. A wake word ("hey Hal" - name TBD) on an always-on, power-efficient
   listening loop: speak to start a dictation, an outro phrase ("that's
   it" / "send request") to stop. Windows mic sharing already allows a
   non-exclusive always-on stream.
3. Meetings detected (or at least startable by voice) so recording
   never depends on remembering a hotkey.
4. After a meeting: auto-suggested artifacts (markdown minutes, action
   items) with harness-style actionable suggestions - "want me to send
   that Slack message? Here's a draft."
5. Long-term: the same feature set ported to another always-on device;
   voice commands that open applications; recognition and dictation
   stay offline/local, only the ACTIONS use an LLM + OAuth.

## Staged architecture proposal

### Step 1 - capture never waits (SHIPPED, this PR)
Dictation and meetings record disk-first regardless of model state;
transcription waits for the model. This is the foundation: every later
stage assumes capture is always safe and cheap.

### Step 2 - wake word, done the power-efficient way
Do NOT loop Whisper over 5-second windows - that keeps the GPU hot and
defeats auto-sleep. The standard tiered pattern, all local and CPU:

- Tier 0: WASAPI shared always-on stream into a small ring buffer
  (seconds), plus cheap RMS/VAD gating (webrtcvad or silero-vad ONNX,
  ~1% CPU) so tiers above run only on speech.
- Tier 1: a dedicated keyword-spotting model on the VAD-gated audio -
  openWakeWord or a Porcupine-style KWS (millisecond CPU inference,
  custom "hey Hal" trainable). These run 24/7 on laptops by design.
- Tier 2: on wake-word hit, splice the ring buffer into a normal
  disk-first dictation (no lost syllables), wake the big model
  (auto_sleep.wake already does this), and end on the outro phrase -
  detectable by the same KWS or by a VAD long-silence rule.
- The whole listener is a feature-pattern module (one owner, flat
  config keys, inert when disabled) and NEVER blocks the mic
  exclusively.

### Step 3 - meeting awareness
Cheapest reliable signal is not audio: a meeting almost always means a
communications app has an active audio session. WASAPI session
enumeration (which app holds an active render/capture stream - Zoom,
Teams, Meet tab) is a poll-cheap, offline heuristic; toast "Meeting
detected - record?" with a one-click start (or auto-start policy per
app). Voice ("hey Hal, record the meeting") comes free with step 2
command routing.

### Step 4 - actions layer (the only online part)
Keep the boundary hard: capture/transcribe/diarize stay offline; the
actions layer consumes FINISHED artifacts (minutes.md, action items)
and drafts outbound actions for one-click approval. Token-efficient
because it runs once per meeting on a compact artifact, not on a
live audio stream. Two viable shapes:

- **Claude CLI headless** (`claude -p`), which the pipeline already
  uses for minutes/speaker ID: extend the minutes prompt to also emit
  a structured action-items block with suggested drafts. MCP servers
  (Slack etc.) give the CLI real, OAuth'd tools; the human stays the
  approve/send button - which also respects harness rules: WhisperSync
  never sends anything itself, it presents drafts.
- **The Aloop/PM harness**: WhisperSync just drops artifacts in a
  watched location; the existing harness picks them up and owns
  suggestions/OAuth. Cleaner separation; WhisperSync stays a light
  local app (the owner's standing constraint) and the "voice assistant
  brain" lives in the successor API-first application.

Recommendation: step 4 via artifact handoff (second shape), steps 2-3
in this app behind config flags. That keeps WhisperSync loadable/
unloadable and makes the future device port a matter of reimplementing
tiers 0-2, which are deliberately tiny.

## Open questions to define before step 2

- Wake word name and outro phrase(s); false-positive tolerance.
- Should wake-word capture be visible (icon state) and separately
  toggleable from the tray? (Almost certainly yes: a "listening" ring.)
- Privacy defaults: always-on listener OFF by default; whisper-mode
  interaction; what, if anything, is ever written to disk before the
  wake word fires (proposal: nothing - the ring buffer is RAM-only).
- Command grammar beyond start/stop ("open X" needs an allowlist).

# Voice assistant direction - 2026-07-04 intake

Status: DIRECTION RECORDED. Build-order step 1 (GPU power-state
failover) SHIPPED 2026-07-04 (#180-#182); the NPU backend (step 6) is
committed scope; steps 2-3 await the owner's phrase/whisper-mode
decisions (open questions below). Round state:
docs/plans/2026-07-04-assistant-build-round.md.
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

## Owner decisions - 2026-07-04 (second intake)

- **Always-on listener: GO.** Build steps 2-3 behind config flags.
- **Meeting auto-record: per-app now, discovery later.** A configurable
  app list (Zoom, Slack huddles, Teams, Meet, ...): when that app opens
  an active audio session, recording auto-starts. Manual trigger stays.
- **In-app wake-word training wanted**: a Train button walks through
  recording the open phrase and the exit phrase; support saving 2-3
  phrases. (Feasibility notes below.)
- **Power efficiency is a primary constraint** (laptop, library use).

## Owner decisions - 2026-07-04 (third intake)

- **Auto-record settings redesign**: Settings > Meeting Auto-Record
  with Enabled, an Apps submenu ("Detect apps..." populates from the
  mic consent store; each app is Record / Ask / Ignore - the 3-state
  resolves the owner's Discord case, where opt-out toasts on every
  call would be exhausting), and a Toasts submenu (master + opt-in +
  opt-out). Opt-in toast: "Auto-recording this meeting" with a single
  "Don't record" action that discards silently. Opt-out toast (ask
  apps): "Not recording this call" with a single "Record" action.
- **Listener POC: GO this session** with a pretrained openWakeWord
  phrase as the placeholder; the owner's custom wake/outro phrases and
  the in-app trainer come later. Whisper-mode default until decided:
  the listener does not run while whisper mode is on.
- **Streaming dictation ("fill as it goes")**: owner floated live
  word-by-word insertion with rolling self-correction for
  paste-unfriendly apps; explicitly deferred ("we can delay this").
  Recorded in BACKLOG.md - it is a different ASR architecture
  (streaming zipformer / whisper-streaming partial hypotheses), not a
  config flip on the current batch pipeline.

## GPU power-state resilience (owner: "actually really important")

The laptop (Core Ultra 9 285H + Arc 140T iGPU + RTX 5070 Ti, hybrid
graphics) can power the discrete GPU on and off. Requirements:

- Detect whether the dGPU is present/active; if yes, use the current
  CUDA model selection unchanged.
- If the dGPU turns off or disconnects MID-SESSION: the app must not
  crash the machine and workers must not hang. Detection signals:
  the VRAM probe (vram_probe.py) starts failing with pynvml/nvidia-smi errors, CUDA errors out
  of the worker, or WM_DEVICECHANGE removal events.
- On loss: kill the worker (wedge detection already bounds a hang),
  respawn on device=cpu with a CPU-appropriate model (new config key,
  e.g. cpu_fallback_model, default small/base - CPU inference was
  validated as acceptable earlier), toast the failover, log it to
  gpu-guard.jsonl.
- CRITICAL: the respawn-after-crash path must NOT retry CUDA in a loop
  when the GPU is gone (today it would). The failover decision belongs
  to GPU Guard - it already owns VRAM health and the downgrade ladder;
  device failover is the ladder's missing floor (the "CPU floor"
  deferral in BACKLOG.md becomes this requirement).
- When the dGPU returns: notify + offer switch-back (or auto, config).

## Hardware portability (owner question, verified 2026-07-04)

| Target | Verdict |
|--------|---------|
| Raspberry Pi 3/4/5, Orange Pi | Comfortable for tiers 0-2: silero-vad + openWakeWord run in real time on a Pi 3. This is the reference platform for Home Assistant voice work. |
| ESP32-S3 | NOT too much to ask - it is the shipped standard for Home Assistant voice satellites, via microWakeWord (TFLite-Micro models designed for the S3). openWakeWord itself is too heavy for it; the S3 runs wake word + VAD locally and streams audio to a host for transcription. |
| Plain ESP32 (non-S3) | Marginal; the S3 vector instructions matter. Prefer S3. |

Portability consequence: keep tier 0-2 model formats to ONNX/TFLite
and the interface tiny (audio in, wake events out); the same design
ports from the laptop to a Pi (same code) or an S3 satellite
(microWakeWord reimplementation, protocol unchanged).

## Wake-word model landscape (owner question)

- **openWakeWord** (Apache-2.0, dscripka; models on GitHub/HF Hub) -
  the laptop/Pi choice. ONNX/TFLite, pretrained models, custom phrases
  trained SYNTHETICALLY (Piper TTS generates the samples; no human
  recordings needed), plus a per-user custom verifier layer trained on
  a few real recordings in seconds.
- **microWakeWord** (kahrendt/OHF-Voice) - same synthetic-training
  approach, models small enough for ESP32-S3/TFLite-Micro. The
  openWakeWord docs point efficiency-critical users here.
- **Porcupine (Picovoice)** - commercial, free tier; type a phrase in
  their console and get a model instantly; broadest MCU support.
  Tradeoff: licensing + accounts, not fully local training.
- **sherpa-onnx KWS (k2-fsa)** - open-source spotter where keywords are
  defined AS TEXT at runtime (phoneme matching), no training run at
  all. Weaker than a trained model but instant phrase changes.
- LiveKit published a one-command wake-word training pipeline built on
  the same synthetic approach - useful reference for the in-app
  trainer.

**In-app training design implication**: "click Train" cannot literally
retrain openWakeWord on-device in seconds (synthetic pipeline + GPU,
tens of minutes). The honest UX: (a) phrase presets from pretrained
models; (b) per-user CUSTOM VERIFIER training in-app (record 3-5
samples of open/exit phrases - seconds, fully local; a real
openWakeWord feature); (c) fully custom phrases as a background
training job (Piper + openWakeWord trainer on the dGPU while idle) or
via sherpa-onnx text keywords for instant-but-weaker phrases. Support
2-3 saved phrases as parallel models - cheap, same loop.

## NPU (owner question)

This laptop has an Intel AI Boost NPU (Core Ultra 9 285H, verified via
device enumeration). Assessment:

- VAD + wake word are ~1% CPU; moving them to the NPU is possible
  (ONNX Runtime OpenVINO EP) but saves little - not the priority.
- The REAL NPU win: the CPU-fallback/backup Whisper model. Running
  whisper base/small through OpenVINO on the NPU instead of CPU cuts
  the power draw of the library scenario and of always-available
  dictation. This slots into the GPU-failover work: fallback targets
  become cpu OR npu-via-openvino.
- Caveat: the worker stack is ctranslate2/whisperX (CUDA/CPU only). An
  OpenVINO whisper path is a NEW backend for the backup transcriber,
  not a config flip.
- **Owner decision 2026-07-04: COMMITTED scope, not an experiment.**
  Ship it as a built-in feature that auto-detects an NPU and prefers it
  for the backup/fallback transcriber when present (feature pattern:
  own module/backend, config key, inert without an NPU). Sequence it
  after GPU failover; measure quality/latency vs the CPU path as the
  acceptance gate.

## Build order (next sessions)

1. GPU power-state failover (guard-owned; includes cpu_fallback_model)
   - the safety item, and auto-sleep's natural sibling.
2. Tier 0+1 listener: always-on shared stream + ring buffer +
   silero-vad + openWakeWord with a pretrained phrase, OFF by default,
   listening icon state, tray toggle.
3. Tier 2 splice: wake -> ring-buffer-prefixed dictation; outro phrase.
4. Per-app meeting auto-record (WASAPI session watch + app list in
   settings).
5. In-app trainer (verifier first, background full training later).
6. NPU/OpenVINO backup-transcriber backend (committed; auto-detect,
   prefer NPU when present, CPU otherwise).

## Open questions to define before step 2

- Wake word name and outro phrase(s); false-positive tolerance.
- Privacy defaults confirmed: listener OFF by default, ring buffer
  RAM-only until wake; how the listener interacts with whisper mode.
- Command grammar beyond start/stop ("open X" needs an allowlist).

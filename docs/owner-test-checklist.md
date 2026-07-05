# Owner test checklist - running list

Everything shipped but not yet validated by the owner on the real
machine, with concrete steps and expected results. Check items off as
they pass; anything that fails becomes a bug issue (`/ws-bug`) and the
entry stays open with a note.

**Maintenance rule (same-PR docs discipline):** any PR that changes
owner-facing behavior appends or updates an entry here in the same PR.
Automated coverage lives in the suites (docs/testing.md); this list is
only for what needs the owner's hands: real mic, real calls, real GPU,
real days of uptime.

## 0. Restart the tray app (gates everything below)

- [ ] Quit the tray app and start it again from the checkout.
  Everything from #167 onward (auto-sleep, failover, auto-record,
  wake listener, all the fixes) only activates after this restart.

## 1. Post-restart smoke (5 min)

- [ ] Normal dictation: hotkey, speak a sentence, hotkey - text pastes.
- [ ] Normal meeting: record ~1 min with system audio, stop, name it -
  minutes + transcript appear, no error toast.
- [ ] The #167 fixes ride along: the meeting completes (no
  post-transcription abort) and saving with a name that already
  exists does not clobber anything.

## 2. Model auto-sleep (#174) + dictation-always-records (#176)

- [ ] Leave the app idle 30+ min - tray icon turns deep-gray (asleep),
  VRAM drops (check Task Manager or `gpu-guard.jsonl` model_sleep).
- [ ] Double-click the tray icon - sleeps on demand (for gaming).
- [ ] While asleep, hit the dictation hotkey and speak IMMEDIATELY -
  recording starts at once (yellow loading flash), text arrives
  after the model loads. Nothing you said is lost.

## 3. GPU power-state failover (#180-#182) - opportunistic

Hard to trigger on demand; validate whenever the hybrid laptop powers
the dGPU off mid-session (or via a driver toggle).

- [ ] On dGPU loss: failover toast, dictation still works (cpu +
  `cpu_fallback_model`), no crash, no CUDA-retry loop in the log.
- [ ] When the dGPU returns and the app is idle: switch-back toast,
  next dictation is fast (GPU) again.
- [ ] `gpu-guard.jsonl` shows gpu_device_lost / gpu_device_recovered.

## 4. Per-app meeting auto-record (#183, #184, #186)

Enable Settings > Meeting Auto-Record first (off by default).

- [ ] Join a Zoom or Slack call - start toast within ~10s (two 5s
  polls), recording begins on its own.
- [ ] Leave the call - recording stops ~30s later, save dialog appears.
- [ ] Opt-in toast's "Don't record" - recording discards silently, no
  save dialog, nothing on disk.
- [ ] Discord (default "ask"): joining a call shows the opt-out toast
  with a one-click "Record" button; ignoring it records nothing.
- [ ] Settings > Meeting Auto-Record > Apps > "Detect apps..." - apps
  that have used the mic appear; new ones arrive as Ignore.
- [ ] Manual hotkey recording still works exactly as before with the
  watcher enabled.

## 5. Wake listener + tier-2 splice (#187, #189, #190)

Enable Settings > Wake Word Listener (off by default; first enable
downloads the openWakeWord models - watch the log).

- [ ] Say "hey jarvis, take a note about X" in one breath - dictation
  starts, and the pasted text is "take a note about X" WITHOUT the
  wake phrase and without clipped syllables at the start.
- [ ] Stop by hotkey mid-dictation - works, listener resumes normal
  listening.
- [ ] After waking it, stay silent ~8s - the dictation stops itself
  and transcribes what was said (silence auto-stop).
- [ ] Say "hey jarvis" during a meeting recording - nothing happens
  except a yellow flash (busy refusal).
- [ ] Whisper mode on - the listener does not react to the phrase at
  all; whisper mode off - it reacts again.
- [ ] A NORMAL hotkey dictation that happens to contain the words "hey
  jarvis" mid-sentence keeps them in the text (strip is
  wake-sessions-only).
- [ ] Optional (needs a second pretrained model set as
  `wake_outro_model`, e.g. "alexa"): saying the outro ends the
  dictation and the outro word is stripped from the tail.

## 6. Multi-day soak (docs/testing.md five signals)

- [ ] After a few days of normal use: no "Unexpected mode transition"
  warnings in the log (gates the MODE_TRANSITIONS warn-to-reject
  tightening).
- [ ] Clean exits, flat memory across meetings, `gpu-guard.jsonl`
  shows only expected events.

## 7. Phrase training first run (step 5 PR A) - needs your go twice

Both gates exist on purpose: the download is 12-18 GB of disk, and
training occupies the dGPU for tens of minutes (do not start it while
gaming).

- [ ] `python training/setup_trainer.py --yes` completes (idempotent -
  rerun after any interruption and it continues).
- [ ] `python training/train_phrase.py "hey hal" --go` produces
  `training/workspace/phrases/hey_hal.onnx` (this run also validates
  the PROVISIONAL config values; a bad key fails fast and gets fixed).
- [ ] Register it in `wake_phrases` (name `hey_hal`, the .onnx path,
  role `wake`, active `true`), then check it under Settings > Wake
  Word Listener > Saved Phrases - saying "hey hal" starts a
  dictation; "hey jarvis" no longer does (custom phrases replace the
  pretrained fallback while any is active).

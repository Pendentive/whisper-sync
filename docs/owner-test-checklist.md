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

**Automated evidence (owner directive 2026-07-05: verify outcomes with
software tests, not hand-testing):** `tests/test_live_validation.py`
(WS_LIVE=1, app venv) exercises the real mic, the real consent store,
the real openWakeWord model on synthesized speech, the real GPU probe,
a real CPU-pinned worker, and the full production pipeline on the
pinned real-meeting fixture (tests/fixtures/). Items below marked
`[x] automated` passed there on 2026-07-05 (9/9 green); unchecked
items still need a human or multi-day soak.

## 0. Restart the tray app (gates everything below)

- [x] DONE 2026-07-05: the app was not running; the session started
  it fresh from the checkout via start.ps1 (post-#194 code, worker
  spawned, guard_started logged). Everything from #167 onward is now
  active.

## 1. Post-restart smoke (5 min)

- [ ] Normal dictation: hotkey, speak a sentence, hotkey - text pastes.
- [x] automated (LiveRealMeetingTests): the full production pipeline
  (GPU model, align, diarize) on the pinned 5.9-min 4-speaker meeting
  fixture produced 60%+ of the reference word volume, 2+ speakers,
  and full-duration coverage. 2026-07-05.
- [ ] Normal meeting via the tray: record ~1 min with system audio,
  stop, name it - minutes + transcript appear, no error toast.
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

## 3. GPU power-state failover (#180-#182)

- [x] automated (LiveFailoverTests): with the probe reporting the
  device gone, the guard declares loss, pins the spawn to cpu +
  `cpu_fallback_model`, and a REAL worker transcribes speech
  correctly on CPU; with the real probe, the GPU is seen and no
  pinning happens. 2026-07-05.
- [ ] Opportunistic (real dGPU power-off only): failover toast +
  switch-back toast in the running tray app, gpu_device_lost /
  gpu_device_recovered in `gpu-guard.jsonl`.

## 4. Per-app meeting auto-record (#183, #184, #186)

Enable Settings > Meeting Auto-Record first (off by default).

- [ ] Join a Zoom or Slack call - start toast within ~10s (two 5s
  polls), recording begins on its own.
- [ ] Leave the call - recording stops ~30s later, save dialog appears.
- [ ] Opt-in toast's "Don't record" - recording discards silently, no
  save dialog, nothing on disk.
- [ ] Discord (default "ask"): joining a call shows the opt-out toast
  with a one-click "Record" button; ignoring it records nothing.
- [x] automated (LiveConsentStoreTests): the consent-store probe that
  powers detection and "Detect apps..." reads real entries on this
  machine. 2026-07-05.
- [ ] Settings > Meeting Auto-Record > Apps > "Detect apps..." - apps
  that have used the mic appear; new ones arrive as Ignore.
- [ ] Manual hotkey recording still works exactly as before with the
  watcher enabled.

## 5. Wake listener + tier-2 splice (#187, #189, #190)

Enable Settings > Wake Word Listener (off by default; first enable
downloads the openWakeWord models - watch the log).

> Owner-reported failure 2026-07-05 ("nothing happens"): root-caused
> to a DLL-load-order conflict (windows_toasts' WinRT bindings break
> onnxruntime when loaded first) - the listener silently reported
> itself unavailable with a misleading "not installed" message. Fixed
> by an onnxruntime preload at app startup + an honest error split;
> reproduced and verified by bisect. Retest after the next app start
> picks up the fix.

- [x] automated (LiveWakePipelineTests): synthesized "hey jarvis"
  through the real model + real decision loop fires EXACTLY one wake
  and hands a well-formed >1s ring-buffer prefix to the dictation
  seam; unrelated speech never fires; the silero VAD separates speech
  from silence (the silence auto-stop signal). 2026-07-05.
- [ ] Say "hey jarvis, take a note about X" into the real mic with
  the tray app running - text pastes WITHOUT the wake phrase and
  without clipped syllables (the last human-only step: your voice,
  your mic, the running app).
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

## 8. In-app phrase manager (step 5 PR C) - the no-CLI path

- [ ] Settings > Wake Word Listener > "Set New Wake Phrase...", type
  a phrase, Start Training - toast confirms, the menu shows a live
  status line (stage + minutes), and after 20-40 min a "Phrase
  ready" toast fires with the phrase ACTIVE in Saved Phrases.
- [ ] Saying the new phrase starts a dictation without any manual
  registry edit or listener toggle.
- [ ] While a job is running, starting a second one refuses politely;
  starting one while the model is asleep (gaming) refuses with a
  clear message and touches nothing.

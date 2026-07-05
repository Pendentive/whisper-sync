# Test fixtures - machine-local, never committed

Everything in this directory except this README and the .gitignore is
ignored by git: the repo is public and REAL MEETING AUDIO STAYS OUT.
The fixtures are pinned locally so live tests run against a stable,
known recording instead of re-finding one every session (owner
directive, 2026-07-05).

## real-meeting/

A real 5-10 minute multi-speaker meeting used by
`tests/test_live_validation.py::LiveRealMeetingTests` (WS_LIVE=1).

- `recording.wav` - the meeting audio (16 kHz stereo mic+speaker mix).
- `reference-transcript.json` - the transcript the production pipeline
  produced for this recording when it was originally processed. The
  live test uses it as the accuracy baseline (word volume, speaker
  count), NOT as an exact-match target - models change.

Current fixture (dev machine): 2026-04-20 credit meeting, 5.9 min,
4 speakers, 767 reference words, real conversation throughout.

To (re)provision on a new machine: copy a recording.wav +
transcript.json pair from any WhisperSync meeting folder that is
5-10 minutes long with at least 2 real speakers. The live test skips
with instructions when the fixture is absent.

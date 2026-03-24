# Testing Rules

No automated test suite exists. All verification is manual.

## Setup

1. Switch to the dev branch: `git checkout dev && git pull`
2. Activate the virtual environment: `whisper-env\Scripts\activate`
3. Run: `python -m whisper_sync`
4. Verify the tray icon appears and the log window shows model loading

## Dictation Test

1. Open a text editor or input field
2. Press the dictation hotkey (default: `Ctrl+Shift+Space`)
3. Speak clearly for 3-5 seconds
4. Press the hotkey again to stop
5. Verify: text appears in the focused window within a few seconds
6. Verify: tray icon cycles through dictation (blue) -> transcribing (yellow) -> done (green) -> idle

## Meeting Test

1. Press the meeting hotkey (default: `Ctrl+Shift+M`)
2. Speak or play audio for 10+ seconds
3. Press the hotkey again to stop
4. In the save dialog: enter a name and click "Save & Summarize" or "Save"
5. Verify: transcript.json and transcript-readable.txt appear in the output folder
6. Verify: if summarized, minutes.md is generated

## Device Switch Test

1. Open Settings -> Device in the tray menu
2. Switch from GPU to CPU (or vice versa)
3. Run a dictation immediately after switching
4. Verify: transcription completes without errors
5. Verify: log shows model reload on the new device
6. Switch back and verify again

## GPU Memory Stability

1. Run 5+ dictations in sequence
2. After each, check `nvidia-smi` in a terminal
3. Verify: GPU memory usage is stable (not growing between dictations)
4. Run a meeting transcription, then 3 more dictations
5. Verify: memory returns to baseline after meeting completes

## Log Window Tier Test

1. Open Settings -> Log Window in the tray menu
2. Switch between Off, Normal, Detailed, and Verbose
3. Verify: console output changes immediately (no restart needed)
4. Run a dictation on each tier and confirm expected output:
   - Off: no console output
   - Normal: shows dictation result summary
   - Detailed: shows transcription content preview
   - Verbose: shows full debug output with `[WhisperSync]` prefix

## Incognito Mode Test

1. Enable incognito via the tray menu
2. Run a dictation
3. Verify: no WAV file saved in the dictation logs folder
4. Verify: no entry added to the daily dictation log markdown
5. Disable incognito and verify normal behavior resumes

## Crash Recovery Test

1. Start a dictation or meeting
2. Kill the worker process: `taskkill /F /PID <worker_pid>` (find PID in log output)
3. Verify: main process detects the crash and shows an error state
4. Verify: worker respawns automatically
5. Start another dictation and verify it works

## Backup Dictation Test

1. Start a meeting recording (Ctrl+Shift+M)
2. Press Ctrl+Shift+Space to trigger dictation
3. Verify: dictation works on CPU (backup model)
4. Verify: meeting recording continues uninterrupted during and after dictation
5. Verify: yellow double-flash appears on rapid hotkey presses during model load
6. Verify: Settings > Device label shows correct device (CPU when CPU selected)
7. Verify: tray icon shows three rings with blue inner dot during meeting + dictation
8. Verify: log output contains "Backup model loading [cpu]" confirming CPU device

"""Watchdog — auto-restart WhisperSync on crash.

Run this instead of `python -m whisper_sync` to get automatic restart
when the process dies from a native segfault or other non-zero exit.

Usage:
    python -m whisper_sync.watchdog

Clean exit (user quit, exit code 0) stops the watchdog.
Crash loops (too many crashes in a short time) also stop it.
"""

import subprocess
import sys
import time
from pathlib import Path

MAX_RESTARTS = 5
COOLDOWN_SECONDS = 5
RESET_AFTER_SECONDS = 300  # Reset crash counter after 5 min of stability


def main():
    python = sys.executable
    cwd = str(Path(__file__).parent.parent)
    crashes = 0

    while crashes < MAX_RESTARTS:
        start_time = time.time()

        print(f"[Watchdog] Starting WhisperSync...")
        proc = subprocess.run(
            [python, "-m", "whisper_sync"],
            cwd=cwd,
        )

        runtime = time.time() - start_time

        if proc.returncode == 0:
            print("[Watchdog] WhisperSync exited cleanly.")
            break

        crashes += 1

        # If it ran for a long time before crashing, it was stable — reset counter
        if runtime > RESET_AFTER_SECONDS:
            crashes = 1

        print(
            f"[Watchdog] WhisperSync exited with code {proc.returncode} "
            f"(crash {crashes}/{MAX_RESTARTS}), "
            f"restarting in {COOLDOWN_SECONDS}s..."
        )
        time.sleep(COOLDOWN_SECONDS)

    if crashes >= MAX_RESTARTS:
        print(f"[Watchdog] Too many crashes ({MAX_RESTARTS}), giving up.")
        sys.exit(1)


if __name__ == "__main__":
    main()

"""GitHub PR status polling — surfaces PR state in the tray menu.

Polls `gh pr list` on a background thread and parses results.
Degrades gracefully if `gh` CLI is not installed or not authenticated.
No LLM cost — pure CLI + JSON parsing.
"""

import json
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .logger import logger


def _find_gh() -> str:
    """Find the gh CLI executable path."""
    import shutil
    gh = shutil.which("gh")
    if gh:
        return gh
    # Common install locations on Windows
    for path in [
        r"C:\Program Files\GitHub CLI\gh.exe",
        r"C:\Program Files (x86)\GitHub CLI\gh.exe",
    ]:
        if Path(path).exists():
            return path
    return "gh"  # Fallback to bare name


_GH = _find_gh()


@dataclass
class PRStatus:
    """Parsed status of a single pull request."""
    number: int
    title: str
    state: str  # OPEN, CLOSED, MERGED
    complexity: str  # low, medium, high, unknown
    review_state: str  # pending, clean, suggestions, human-review
    suggestion_count: int = 0
    url: str = ""

    @property
    def display(self) -> str:
        """One-line summary for the tray menu."""
        status_icon = {
            "pending": "...",
            "clean": "ok",
            "suggestions": f"{self.suggestion_count} suggestion{'s' if self.suggestion_count != 1 else ''}",
            "human-review": "needs review",
        }.get(self.review_state, "?")
        return f"#{self.number}: {self.title[:40]} — {status_icon}"


@dataclass
class GitHubState:
    """Current state of all open PRs."""
    prs: list = field(default_factory=list)
    last_poll: float = 0
    error: str | None = None
    available: bool = False  # True if gh CLI is installed and authenticated


def check_gh_available() -> bool:
    """Check if gh CLI is installed and authenticated."""
    try:
        result = subprocess.run(
            [_GH, "auth", "status"],
            capture_output=True, text=True, timeout=10,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def poll_prs(repo: str) -> list[PRStatus]:
    """Fetch open PRs and their review status from GitHub."""
    try:
        result = subprocess.run(
            [_GH, "pr", "list", "--repo", repo,
             "--json", "number,title,state,url,labels,reviews,reviewRequests",
             "--limit", "10"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            logger.debug(f"gh pr list failed: {result.stderr.strip()}")
            return []

        prs_raw = json.loads(result.stdout)
        parsed = []

        for pr in prs_raw:
            # Determine complexity from labels
            labels = [l.get("name", "") for l in pr.get("labels", [])]
            complexity = "unknown"
            for l in labels:
                if l.startswith("complexity:"):
                    complexity = l.split(":")[1]
                    break

            # Determine review state from Copilot reviews
            reviews = pr.get("reviews", [])
            copilot_reviews = [
                r for r in reviews
                if r.get("author", {}).get("login", "") == "copilot-pull-request-reviewer[bot]"
            ]

            review_state = "pending"
            suggestion_count = 0

            if copilot_reviews:
                # Only fetch suggestion count if Copilot actually reviewed
                # (avoids extra API calls for PRs without reviews)
                suggestion_count = _count_copilot_suggestions(repo, pr["number"])
                if suggestion_count > 0:
                    review_state = "suggestions"
                elif complexity == "high":
                    review_state = "human-review"
                else:
                    review_state = "clean"
            elif complexity == "high":
                review_state = "human-review"

            parsed.append(PRStatus(
                number=pr["number"],
                title=pr["title"],
                state=pr["state"],
                complexity=complexity,
                review_state=review_state,
                suggestion_count=suggestion_count,
                url=pr.get("url", ""),
            ))

        return parsed

    except (json.JSONDecodeError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        logger.debug(f"GitHub poll error: {e}")
        return []


def _count_copilot_suggestions(repo: str, pr_number: int) -> int:
    """Count inline review comments from Copilot on a PR."""
    try:
        result = subprocess.run(
            [_GH, "api", f"repos/{repo}/pulls/{pr_number}/comments",
             "--jq", '[.[] | select(.user.login == "copilot-pull-request-reviewer[bot]")] | length'],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return int(result.stdout.strip())
    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
        pass
    return 0


class GitHubPoller:
    """Background thread that polls GitHub PR status."""

    def __init__(self, repo: str, interval: int = 300, on_change=None):
        self.repo = repo
        self.interval = interval
        self.on_change = on_change  # callback(old_state, new_state)
        self.state = GitHubState()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._polling = threading.Lock()

    def start(self):
        """Start polling in background. No-op if already running."""
        if self._thread and self._thread.is_alive():
            return

        # Check gh availability once at startup
        self.state.available = check_gh_available()
        if not self.state.available:
            logger.info("GitHub status: gh CLI not available, feature disabled")
            return

        logger.info(f"GitHub status: polling {self.repo} every {self.interval}s")
        self._stop.clear()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def stop(self):
        """Stop the polling thread."""
        self._stop.set()

    def poll_now(self):
        """Trigger an immediate poll (non-blocking, deduplicated)."""
        if self._polling.locked():
            return  # Already polling
        threading.Thread(target=self._do_poll, daemon=True).start()

    def _poll_loop(self):
        """Main polling loop."""
        # Initial poll immediately
        self._do_poll()

        while not self._stop.wait(timeout=self.interval):
            self._do_poll()

    def _do_poll(self):
        """Execute a single poll and process results."""
        if not self._polling.acquire(blocking=False):
            return  # Another poll is running
        try:
            self._do_poll_inner()
        finally:
            self._polling.release()

    def _do_poll_inner(self):
        new_prs = poll_prs(self.repo)
        old_prs = self.state.prs

        self.state.prs = new_prs
        self.state.last_poll = time.time()
        self.state.error = None

        # Detect changes
        if self.on_change and _state_changed(old_prs, new_prs):
            try:
                self.on_change(old_prs, new_prs)
            except Exception as e:
                logger.debug(f"GitHub status change callback error: {e}")


def _state_changed(old: list[PRStatus], new: list[PRStatus]) -> bool:
    """Check if PR state changed between polls."""
    if len(old) != len(new):
        return True
    old_map = {pr.number: pr.review_state for pr in old}
    for pr in new:
        if pr.number not in old_map or old_map[pr.number] != pr.review_state:
            return True
    return False

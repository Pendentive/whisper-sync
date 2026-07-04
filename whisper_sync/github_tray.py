"""GitHub PR status tray glue - extracted from __main__.py.

Hardening round item 6 (architecture spec A2), extraction 3. GitHubTray
wires github_status.GitHubPoller into the tray: poller lifecycle, PR
change notifications with action buttons, the GitHub menu section, and
the gh-CLI merge action. Behavior is a direct move; the only
simplification is that toasts call notifications.notify directly
(the one-line _notify wrapper is gone).
"""

from .logger import logger
from .notifications import notify
from .tray_menu import menu_callback


class GitHubTray:
    """Owns the PR poller and its tray surface; services via ``app``."""

    def __init__(self, app):
        self.app = app
        self._poller = None
        self._prs = []

    def stop(self):
        """Stop the poller if it was started (from run() teardown)."""
        if self._poller:
            self._poller.stop()

    def poll_now(self):
        if self._poller:
            self._poller.poll_now()

    def start(self):
        """Start the GitHub PR status poller if configured (from run())."""
        repo = self.app.cfg.get("github_repo")
        if not repo:
            return

        from .github_status import GitHubPoller
        interval = self.app.cfg.get("github_poll_interval", 300)

        def _on_change(old_prs, new_prs):
            self._prs = new_prs
            self.app._refresh_menu()
            if not self.app.cfg.get("github_notifications", True):
                return
            # Notify on actionable changes
            repo = self.app.cfg.get("github_repo", "")
            old_map = {pr.number: pr.review_state for pr in old_prs}
            for pr in new_prs:
                old_state = old_map.get(pr.number)
                if old_state == pr.review_state:
                    continue
                if pr.review_state == "clean":
                    notify(
                        f"PR #{pr.number} ready to merge",
                        pr.title,
                        buttons=[
                            {"label": "Merge", "action": lambda _pr=pr: self._merge_pr(repo, _pr.number)},
                            {"label": "View on GitHub", "action": lambda _pr=pr: self._open_pr_url(_pr.url)},
                        ],
                    )
                elif pr.review_state == "suggestions":
                    notify(
                        f"PR #{pr.number}: {pr.suggestion_count} suggestion(s)",
                        pr.title,
                        buttons=[
                            {"label": "View on GitHub", "action": lambda _pr=pr: self._open_pr_url(_pr.url)},
                        ],
                    )
                elif pr.review_state == "human-review":
                    notify(
                        f"PR #{pr.number} flagged for human review",
                        pr.title,
                        buttons=[
                            {"label": "View on GitHub", "action": lambda _pr=pr: self._open_pr_url(_pr.url)},
                        ],
                    )

        def _on_initial_poll(old_prs, new_prs):
            """First poll - update menu regardless of change detection."""
            _on_change(old_prs, new_prs)

        def _on_feature_scan(open_prs, merged_prs):
            from . import feature_lifecycle
            feature_lifecycle.scan_open_prs(open_prs)
            feature_lifecycle.scan_merged_prs(merged_prs)

        self._poller = GitHubPoller(
            repo=repo, interval=interval, on_change=_on_change,
            on_feature_scan=_on_feature_scan,
        )
        self._poller.start()
        if self._poller.state.available:
            # Refresh menu after the first poll completes: a scheduler
            # step chain (1s cadence, 30 tries) instead of a sleeping
            # thread. Each step is a cheap flag check.
            from .scheduler import scheduler as _sched

            def _first_poll_step(remaining: int):
                if self._poller.state.last_poll > 0:
                    self._prs = self._poller.state.prs
                    self.app._refresh_menu()
                    return
                if remaining > 0:
                    _sched.call_later(1.0, lambda: _first_poll_step(remaining - 1),
                                      label="github-first-poll")

            _first_poll_step(30)

    def menu_items(self) -> list:
        import pystray  # lazy: not installed on the CI system python
        """Build menu items for GitHub PR status."""
        repo = self.app.cfg.get("github_repo")
        if not repo or not self._poller or not self._poller.state.available:
            return []

        prs = self._prs
        if not prs:
            # No PRs — clicking opens GitHub pulls page
            return [pystray.MenuItem(
                "GitHub\tno open PRs",
                menu_callback(self._open_pr_url, f"https://github.com/{repo}/pulls"),
            )]

        label = f"GitHub\t{len(prs)} open PR{'s' if len(prs) != 1 else ''}"
        pr_items = []
        for pr in prs:
            status_label = {
                "pending": "awaiting review",
                "clean": "ready to merge",
                "suggestions": f"{pr.suggestion_count} suggestion{'s' if pr.suggestion_count != 1 else ''}",
                "human-review": "needs review",
            }.get(pr.review_state, "unknown")

            pr_display = f"#{pr.number}: {pr.title[:35]} - {status_label}"

            # Build submenu based on state
            sub = [pystray.MenuItem("View on GitHub", menu_callback(self._open_pr_url, pr.url))]

            if pr.review_state == "clean":
                sub.append(pystray.MenuItem("Merge", menu_callback(self._merge_pr, repo, pr.number)))
            elif pr.review_state == "suggestions":
                sub.append(pystray.MenuItem("View Suggestions", menu_callback(self._open_pr_url, pr.url)))

            pr_items.append(pystray.MenuItem(pr_display, pystray.Menu(*sub)))

        pr_items.append(pystray.Menu.SEPARATOR)
        pr_items.append(pystray.MenuItem("Check now", lambda: self._poller.poll_now()))

        return [pystray.MenuItem(label, pystray.Menu(*pr_items))]

    def _open_pr_url(self, url: str):
        """Open a GitHub URL in the default browser."""
        import webbrowser
        if url:
            webbrowser.open(url)

    def _merge_pr(self, repo: str, pr_number: int):
        """Merge a PR via gh CLI.

        NOTE: This method may be called from a toast notification thread
        (via notifications.py button callbacks). The threading is handled
        in notifications.py -- this method itself is blocking.
        """
        import subprocess as _sp
        from .executors import native_call
        try:
            with native_call("gh-merge"):
                result = _sp.run(
                    ["gh", "pr", "merge", str(pr_number), "--repo", repo,
                     "--squash", "--delete-branch"],
                    capture_output=True, text=True, timeout=30,
                )
            if result.returncode == 0:
                logger.info(f"PR #{pr_number} merged successfully")
                notify("PR merged", f"PR #{pr_number} merged to main")
                # Refresh after merge
                if self._poller:
                    self._poller.poll_now()
            else:
                logger.warning(f"PR #{pr_number} merge failed: {result.stderr.strip()}")
                notify("Merge failed", f"PR #{pr_number} merge failed -- check logs")
        except Exception as e:
            logger.warning(f"PR merge error: {e}")

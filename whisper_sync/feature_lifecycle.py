"""Feature lifecycle automation - updates feature status from PR events.

Bridges GitHub polling and the feature log. Scans open PRs for
``feature:<slug>`` labels to mark features as in-progress, and scans
recently merged PRs to mark features as completed.
"""

import logging
from typing import Dict, List, Optional

from . import feature_log
from .github_status import PRStatus

logger = logging.getLogger("whisper_sync")

# Cache of known PR->feature mappings to avoid repeated label parsing
_pr_feature_map: dict[int, str] = {}  # pr_number -> feature_entry_id


def _extract_feature_id(labels: list[str]) -> Optional[str]:
    """Extract feature entry ID from PR labels.

    Labels use convention ``feature:<slug>`` where slug is the first 19
    chars of the entry ID (e.g., ``feature:2026-03-25T16:31:39``).
    """
    for label in labels:
        if label.startswith("feature:"):
            slug = label[8:]  # After "feature:"
            # Match against feature log entries by prefix
            for entry in feature_log.load_all():
                if entry["id"].startswith(slug):
                    return entry["id"]
    return None


def scan_open_prs(prs: list[PRStatus]) -> None:
    """Scan open PRs for feature labels and update status to in-progress."""
    for pr in prs:
        feature_id = _extract_feature_id(pr.labels)
        if feature_id:
            _pr_feature_map[pr.number] = feature_id
            entry = next(
                (e for e in feature_log.load_all() if e["id"] == feature_id),
                None,
            )
            if entry and entry["status"] == "pending":
                feature_log.update_status(feature_id, "in-progress", pr.url)
                logger.info(
                    "Feature %s linked to PR #%d, status -> in-progress",
                    feature_id[:19], pr.number,
                )


def scan_merged_prs(merged_prs: List[Dict]) -> None:
    """Scan recently merged PRs for feature labels and update status to completed."""
    for pr in merged_prs:
        labels = [l.get("name", "") for l in pr.get("labels", [])]
        feature_id = _extract_feature_id(labels)
        if feature_id:
            entry = next(
                (e for e in feature_log.load_all() if e["id"] == feature_id),
                None,
            )
            if entry and entry["status"] != "completed":
                feature_log.update_status(
                    feature_id, "completed", pr.get("url", ""),
                )
                logger.info(
                    "Feature %s completed via merged PR #%s",
                    feature_id[:19], pr.get("number"),
                )

#!/usr/bin/env python3
"""Run the resumable three-phase APS report repair protocol.

Issue pages are cached first.  Only after every requested issue is cached does
one shared APS recent-page stream fill exact DOI matches.  Rendering then uses
only those two local caches, so a browser failure can never replace a report
with partial metadata.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from papertrack.aps_repair import (
    ABSTRACT_COMPLETE,
    ApsRepairPlan,
    ISSUE_CACHED,
    ISSUE_PENDING,
    ISSUE_RETRYABLE,
    RENDERED,
    cache_issues,
    index_recent_abstracts,
    render_complete_issues,
)
from papertrack.codex import journal_configs
from papertrack.provider_runtime import ProviderRuntime


LOGGER = logging.getLogger(__name__)


def _candidates(
    data_dir: Path, journal_slug: str, requested: list[str] | None = None,
) -> list[tuple[str, str]]:
    root = data_dir / "aps" / journal_slug
    available = [
        (report.parent.name, report.stem)
        for report in root.glob("*/*.md")
        if report.parent.name.isdigit() and report.stem.isdigit()
    ]
    if not requested:
        return available
    requested_pairs = set()
    for candidate in requested:
        try:
            volume, issue = candidate.split("/", 1)
        except ValueError as exc:
            raise ValueError(f"invalid --candidate {candidate!r}; use VOLUME/ISSUE") from exc
        requested_pairs.add((volume, issue))
    return sorted(requested_pairs)


def _plan_path(diagnostics_dir: Path, journal_slug: str) -> Path:
    return diagnostics_dir / f"aps_{journal_slug}_repair_plan.json"


def _migrate_legacy_state(plan: ApsRepairPlan, diagnostics_dir: Path) -> None:
    """Carry old state forward without treating a circuit skip as terminal."""
    legacy_path = diagnostics_dir / "aps_full_repair_state.json"
    if not legacy_path.exists():
        return
    try:
        legacy = json.loads(legacy_path.read_text(encoding="utf-8"))
        records = legacy.get("issues", {})
    except (OSError, ValueError, AttributeError):
        LOGGER.warning("Ignoring unreadable legacy APS repair state: %s", legacy_path)
        return
    for key, state in plan.issues.items():
        old = records.get(key, {})
        outcome = old.get("issue")
        if outcome == "ready":
            state.issue_status = ISSUE_CACHED
        elif outcome in {"blocked", "fetch_failed", "parse_failed", "rate_limited"}:
            state.issue_status = ISSUE_RETRYABLE
            state.message = old.get("issue_message", "legacy retryable failure")
        elif outcome == "circuit_open":
            state.issue_status = ISSUE_PENDING
            state.message = "legacy circuit skip; request not attempted"


def _summary(plan: ApsRepairPlan) -> dict[str, int]:
    records = list(plan.issues.values())
    return {
        "issues": len(records),
        "issue_cached": sum(item.issue_status == ISSUE_CACHED for item in records),
        "issue_retryable": sum(item.issue_status == ISSUE_RETRYABLE for item in records),
        "abstract_complete": sum(item.abstract_status == ABSTRACT_COMPLETE for item in records),
        "rendered": sum(item.render_status == RENDERED for item in records),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--diagnostics-dir", required=True, type=Path)
    parser.add_argument("--profile-root", required=True, type=Path)
    parser.add_argument(
        "--browser-user-data-dir", type=Path,
        help="existing Chrome user-data directory to reuse; the PaperTrack lock remains under --profile-root",
    )
    parser.add_argument(
        "--browser-cdp-url", default="",
        help="attach to an existing Chrome DevTools endpoint instead of launching Chrome",
    )
    parser.add_argument("--snapshot-root", required=True, type=Path)
    parser.add_argument("--journal", default="prl")
    parser.add_argument("--candidate", action="append", default=[], metavar="VOLUME/ISSUE")
    parser.add_argument("--phase", choices=("all", "issue", "recent", "render"), default="all")
    parser.add_argument("--issue-interval", type=float, default=120.0)
    args = parser.parse_args(argv)
    if args.issue_interval < 30:
        parser.error("--issue-interval must be at least 30 seconds")
    config = journal_configs[args.journal]
    try:
        candidates = _candidates(args.data_dir, args.journal, args.candidate)
    except ValueError as exc:
        parser.error(str(exc))
    state_path = _plan_path(args.diagnostics_dir, args.journal)
    created = not state_path.exists()
    plan = ApsRepairPlan.load_or_create(state_path, args.journal, candidates)
    if created:
        _migrate_legacy_state(plan, args.diagnostics_dir)
    plan.save(state_path)

    with ProviderRuntime("aps", args.profile_root, args.snapshot_root,
                         issue_interval=args.issue_interval,
                         browser_user_data_dir=args.browser_user_data_dir,
                         browser_cdp_url=args.browser_cdp_url) as runtime:
        if args.phase in {"all", "issue"}:
            cache_issues(plan, config["name"], runtime, state_path)
        if args.phase in {"all", "recent"}:
            if all(item.issue_status == ISSUE_CACHED for item in plan.issues.values()):
                index_recent_abstracts(plan, config["name"], runtime, state_path)
            else:
                LOGGER.warning("APS recent phase deferred: issue snapshot phase incomplete")
        if args.phase in {"all", "render"}:
            render_complete_issues(
                plan, config["name"], config["issn"], str(args.data_dir / "aps"),
                runtime, state_path,
            )
    print(json.dumps(_summary(plan), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

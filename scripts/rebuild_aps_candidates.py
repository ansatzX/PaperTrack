#!/usr/bin/env python3
"""Sequentially rebuild the audited APS journal candidates."""

from __future__ import annotations

import argparse
from pathlib import Path

from papertrack.journal_rebuild import RebuildCandidate, run_provider_rebuild


_MUST_REBUILD_ISSUES = (("127", "27"), ("129", "27"))
_REVIEW_ISSUES = (
    ("122", "10"),
    ("123", "25"),
    ("124", "22"),
    ("125", "12"),
    ("131", "6"),
    ("132", "13"),
    ("133", "20"),
)


def _candidate(
    volume: str,
    issue: str,
    classification: str,
    priority: int,
    reasons: tuple[str, ...],
) -> RebuildCandidate:
    return RebuildCandidate(
        "aps",
        "prl",
        volume,
        issue,
        f"aps/prl/{volume}/{issue}.md",
        classification,
        priority,
        reasons,
    )


def default_candidates() -> tuple[RebuildCandidate, ...]:
    """Return only the APS issues selected by the approved offline audit."""
    definite_reasons = (
        "whole_issue_missing_abstracts",
        "lowercase_crossref_doi",
        "raw_mathml",
    )
    return tuple(
        [
            *(
                _candidate(volume, issue, "must_rebuild", 10, definite_reasons)
                for volume, issue in _MUST_REBUILD_ISSUES
            )
        ]
        + [
            *(
                _candidate(
                    volume,
                    issue,
                    "ambiguous_review",
                    20,
                    ("ordinary_article_missing_abstract",),
                )
                for volume, issue in _REVIEW_ISSUES
            )
        ]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--diagnostics-dir", required=True, type=Path)
    parser.add_argument("--profile-root", required=True, type=Path)
    parser.add_argument("--snapshot-root", required=True, type=Path)
    parser.add_argument("--retry-unresolved", action="store_true")
    parser.add_argument("--reset-progress", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    summary = run_provider_rebuild(
        "aps",
        default_candidates(),
        data_dir=args.data_dir,
        diagnostics_dir=args.diagnostics_dir,
        profile_root=args.profile_root,
        snapshot_root=args.snapshot_root,
        retry_unresolved=args.retry_unresolved,
        reset_progress=args.reset_progress,
        dry_run=args.dry_run,
    )
    print(
        f"aps: candidates={len(summary.ordered_candidate_keys)} "
        f"attempted={len(summary.attempted_keys)} "
        f"skipped={len(summary.skipped_keys)} "
        f"resolved={len(summary.resolved_keys)} "
        f"unresolved={len(summary.unresolved_keys)} "
        f"fetch_failed_issues={len(summary.fetch_failed_keys)} "
        f"official_fetch_rate={summary.official_fetch_rate:.4f} "
        f"complete_report_rate={summary.complete_report_rate:.4f} "
        f"snapshot_hit={summary.runtime_stats.snapshot_hit} "
        f"live_success={summary.runtime_stats.live_success} "
        f"blocked={summary.runtime_stats.blocked} "
        f"fetch_failed={summary.runtime_stats.fetch_failed} "
        f"circuit_skipped={summary.runtime_stats.circuit_skipped}"
    )
    return 0 if summary.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

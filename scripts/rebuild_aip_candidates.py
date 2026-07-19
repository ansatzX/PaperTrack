#!/usr/bin/env python3
"""Sequentially rebuild the audited AIP journal candidates."""

from __future__ import annotations

import argparse
from pathlib import Path

from papertrack.journal_rebuild import RebuildCandidate, run_provider_rebuild


_PRIORITY_ISSUES = (("160", "4"), ("160", "5"), ("160", "6"), ("160", "9"))
_REVIEW_ISSUES = (
    ("148", "10"),
    ("148", "11"),
    ("148", "13"),
    ("148", "14"),
    ("148", "15"),
    ("148", "24"),
    ("149", "7"),
    ("149", "24"),
    ("150", "4"),
    ("150", "20"),
    ("153", "12"),
    ("153", "21"),
    ("154", "9"),
    ("154", "22"),
    ("155", "5"),
    ("155", "9"),
    ("157", "16"),
    ("158", "5"),
    ("158", "23"),
    ("159", "14"),
    ("161", "4"),
    ("161", "7"),
    ("161", "19"),
    ("162", "19"),
    ("162", "22"),
    ("163", "12"),
    ("163", "21"),
    ("164", "22"),
)


def _candidate(volume: str, issue: str, priority: int) -> RebuildCandidate:
    return RebuildCandidate(
        "aip",
        "jcp",
        volume,
        issue,
        f"aip/jcp/{volume}/{issue}.md",
        "ambiguous_review",
        priority,
        ("ordinary_article_missing_abstract",),
    )


def default_candidates() -> tuple[RebuildCandidate, ...]:
    """Return only the AIP issues selected by the approved offline audit."""
    return tuple(
        [*(_candidate(*issue, 10) for issue in _PRIORITY_ISSUES)]
        + [*(_candidate(*issue, 20) for issue in _REVIEW_ISSUES)]
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
        "aip",
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
        f"aip: candidates={len(summary.ordered_candidate_keys)} "
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
        f"rate_limited={summary.runtime_stats.rate_limited} "
        f"fetch_failed={summary.runtime_stats.fetch_failed} "
        f"circuit_skipped={summary.runtime_stats.circuit_skipped}"
        f" cache_rejected={summary.runtime_stats.cache_rejected}"
        f" terminal_accounted={summary.runtime_stats.terminal_accounted} "
        f"navigation_failure={summary.runtime_stats.navigation_failure} "
        f"parse_validation_failure={summary.runtime_stats.parse_validation_failure} "
        f"cache_failure={summary.runtime_stats.cache_failure} "
        f"endpoint_success={summary.runtime_stats.endpoint_success} "
        f"endpoint_failure={summary.runtime_stats.endpoint_failure} "
        f"unresolved_metadata={summary.runtime_stats.unresolved_metadata}"
    )
    return 0 if summary.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

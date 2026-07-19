#!/usr/bin/env python3
"""Sequentially rebuild the audited ACS journal candidates."""

from __future__ import annotations

import argparse
from pathlib import Path

from papertrack.journal_rebuild import RebuildCandidate, run_provider_rebuild


def default_candidates() -> tuple[RebuildCandidate, ...]:
    """Return only the ACS issues selected by the approved offline audit."""
    return (
        RebuildCandidate(
            "acs",
            "jcim",
            "66",
            "12",
            "acs/jcim/66/12.md",
            "ambiguous_review",
            10,
            ("jcim_priority", "ordinary_article_missing_abstract"),
        ),
        RebuildCandidate(
            "acs",
            "jctc",
            "22",
            "12",
            "acs/jctc/22/12.md",
            "must_rebuild",
            20,
            ("whole_issue_missing_abstracts", "raw_html"),
        ),
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
        "acs",
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
        f"acs: candidates={len(summary.ordered_candidate_keys)} "
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

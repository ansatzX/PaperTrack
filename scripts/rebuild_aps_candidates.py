#!/usr/bin/env python3
"""Compatibility entry point for the APS candidates audited before migration."""

from __future__ import annotations

import sys

try:  # runnable both as ``python scripts/...`` and package import in tests
    from .repair_all_aps_reports import main as repair_main
except ImportError:
    from repair_all_aps_reports import main as repair_main

from papertrack.journal_rebuild import RebuildCandidate


_CANDIDATES = (
    "127/27", "129/27", "122/10", "123/25", "124/22", "125/12",
    "131/6", "132/13", "133/20",
)


def default_candidates() -> tuple[RebuildCandidate, ...]:
    """Legacy audit manifest, retained for diagnostics and test visibility."""
    must = {"127/27", "129/27"}
    return tuple(
        RebuildCandidate(
            "aps", "prl", volume, issue, f"aps/prl/{volume}/{issue}.md",
            "must_rebuild" if f"{volume}/{issue}" in must else "ambiguous_review",
            10 if f"{volume}/{issue}" in must else 20,
            ("whole_issue_missing_abstracts", "lowercase_crossref_doi", "raw_mathml")
            if f"{volume}/{issue}" in must
            else ("ordinary_article_missing_abstract",),
        )
        for volume, issue in (item.split("/", 1) for item in _CANDIDATES)
    )


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    # Old worker-only flags did not carry useful APS semantics.  Keep the
    # wrapper invocation compatible while routing all real work to one owner.
    arguments = [arg for arg in arguments if arg not in {"--retry-unresolved", "--reset-progress", "--dry-run"}]
    for candidate in _CANDIDATES:
        arguments.extend(("--candidate", candidate))
    return repair_main(arguments)


if __name__ == "__main__":
    raise SystemExit(main())

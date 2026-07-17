#!/usr/bin/env python3
"""Run one bounded, read-only official issue check per selected provider."""

from __future__ import annotations

import argparse
import json
from typing import Callable

from papertrack.adapter_result import AdapterResult
from papertrack.source_policy import SourcePolicy

SOURCE_POLICY = SourcePolicy.official_only()
PROVIDERS = ("acs", "aip", "aps")


def _official_fetchers() -> dict[str, Callable[[], AdapterResult]]:
    from papertrack.acs_fetch import query_acs_issue_result
    from papertrack.aip_fetch import query_aip_issue_result
    from papertrack.aps_fetch import query_aps_issue_result

    return {
        "acs": lambda: query_acs_issue_result(
            "jcisd8",
            "66",
            "12",
            "Journal of Chemical Information and Modeling",
        ),
        "aip": lambda: query_aip_issue_result(
            "jcp",
            "164",
            "16",
            "The Journal of Chemical Physics",
        ),
        "aps": lambda: query_aps_issue_result(
            "prl",
            "127",
            "27",
            "Physical Review Letters",
        ),
    }


def verify(
    providers: list[str],
    *,
    fetchers: dict[str, Callable[[], AdapterResult]] | None = None,
) -> int:
    selected_fetchers = fetchers or _official_fetchers()
    failed = False
    for provider in providers:
        try:
            result = selected_fetchers[provider]()
            article_count = len(result.entries)
            passed = result.ok and article_count > 0
            observation = {
                "provider": provider,
                "source_mode": SOURCE_POLICY.crossref_mode.value,
                "outcome": result.outcome,
                "article_count": article_count,
                "ok": passed,
                "message": result.message,
                "metadata": result.metadata,
            }
        except Exception as exc:
            passed = False
            observation = {
                "provider": provider,
                "source_mode": SOURCE_POLICY.crossref_mode.value,
                "outcome": "exception",
                "article_count": 0,
                "ok": False,
                "message": repr(exc),
            }
        print(json.dumps(observation, ensure_ascii=False, sort_keys=True))
        failed = failed or not passed
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--provider",
        choices=("all",) + PROVIDERS,
        default="all",
        help="official provider to check",
    )
    args = parser.parse_args(argv)
    providers = list(PROVIDERS) if args.provider == "all" else [args.provider]
    return verify(providers)


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Verify exactly one canonical publisher issue without writing Markdown by default."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Callable

from papertrack.adapter_result import AdapterResult, FETCH_FAILED
from papertrack.codex import journal_configs
from papertrack.diagnostics import ATTEMPTS_FILE, configure_diagnostics
from papertrack.provider_runtime import ProviderRuntime
from papertrack.source_policy import SourcePolicy


PROVIDERS = ("acs", "aip", "aps")
SOURCE_POLICY = SourcePolicy.official_only()
_SAFE_SCOPE_VALUE = re.compile(r"[A-Za-z0-9_.-]+")


@dataclass(frozen=True)
class IssueScope:
    provider: str
    journal: str
    volume: str
    issue: str

    def __post_init__(self) -> None:
        if self.provider not in PROVIDERS:
            raise ValueError(f"unsupported official provider: {self.provider}")
        for name in ("journal", "volume", "issue"):
            value = getattr(self, name)
            if not _SAFE_SCOPE_VALUE.fullmatch(value) or value in {".", ".."}:
                raise ValueError(f"invalid {name}: {value!r}")

        config = _journal_config(self.journal)
        configured_provider = config.get("provider") or (
            "acs" if config.get("acs_code") else ""
        )
        if configured_provider != self.provider:
            raise ValueError(
                f"journal {self.journal!r} belongs to provider "
                f"{configured_provider!r}, not {self.provider!r}"
            )

    @property
    def key(self) -> str:
        return f"{self.provider}/{self.journal}/{self.volume}/{self.issue}"


class _SingleValueAction(argparse.Action):
    """Reject repeated scope options instead of silently taking the last one."""

    def __call__(self, parser, namespace, values, option_string=None):
        if getattr(namespace, self.dest, None) is not None:
            parser.error(f"{option_string} may be supplied exactly once")
        setattr(namespace, self.dest, values)


def _journal_config(journal: str) -> dict[str, str]:
    try:
        return journal_configs[journal]
    except KeyError as exc:
        raise ValueError(f"unknown journal config: {journal!r}") from exc


def _fetch_official_issue(scope: IssueScope, runtime) -> AdapterResult:
    config = _journal_config(scope.journal)
    if scope.provider == "acs":
        from papertrack.acs_fetch import query_acs_issue_result

        return query_acs_issue_result(
            config["acs_code"],
            scope.volume,
            scope.issue,
            config["name"],
            provider_runtime=runtime,
        )
    if scope.provider == "aip":
        from papertrack.aip_fetch import query_aip_issue_result

        return query_aip_issue_result(
            config.get("slug", scope.journal),
            scope.volume,
            scope.issue,
            config["name"],
            provider_runtime=runtime,
        )

    from papertrack.aps_fetch import query_aps_issue_result

    return query_aps_issue_result(
        config.get("slug", scope.journal),
        scope.volume,
        scope.issue,
        config["name"],
        provider_runtime=runtime,
    )


def _write_report(scope: IssueScope, runtime, data_dir: Path) -> bool:
    from papertrack.report import filter_journal_to_md

    config = _journal_config(scope.journal)
    return filter_journal_to_md(
        journal_name=config["name"],
        journal_slug=config.get("slug", scope.journal),
        volume=scope.volume,
        issue=scope.issue,
        issn=config["issn"],
        md_folder=str(data_dir / scope.provider),
        zotero=None,
        acs_code=config.get("acs_code", ""),
        provider=config.get("provider", scope.provider),
        source_policy=SOURCE_POLICY,
        provider_runtime=runtime,
    )


def _cache_commit_status(result: AdapterResult) -> str:
    transport = result.metadata.get("official_transport", "unknown")
    if transport == "snapshot" and result.metadata.get("snapshot_path"):
        return "hit"
    if transport == "live" and result.metadata.get("snapshot_path"):
        return "committed"
    return "not_committed"


def _observation(
    scope: IssueScope,
    result: AdapterResult,
    diagnostics_dir: Path,
) -> dict:
    return {
        "provider": scope.provider,
        "issue_key": scope.key,
        "transport": result.metadata.get("official_transport", "unknown"),
        "outcome": result.outcome,
        "selector_counts": result.metadata.get("selector_counts", {}),
        "membership_count": len(result.entries),
        "cache_commit_status": _cache_commit_status(result),
        "diagnostic_path": str(diagnostics_dir / ATTEMPTS_FILE),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check one canonical official journal issue"
    )
    for name in ("provider", "journal", "volume", "issue"):
        parser.add_argument(
            f"--{name}",
            required=True,
            default=None,
            action=_SingleValueAction,
            choices=PROVIDERS if name == "provider" else None,
        )
    parser.add_argument("--profile-root", required=True, type=Path)
    parser.add_argument("--snapshot-root", required=True, type=Path)
    parser.add_argument("--diagnostics-dir", required=True, type=Path)
    parser.add_argument("--write-report", action="store_true")
    parser.add_argument(
        "--data-dir",
        type=Path,
        help="report root; required only with --write-report",
    )
    return parser


def main(
    argv: list[str] | None = None,
    *,
    runtime_factory=ProviderRuntime,
    fetch_issue: Callable[[IssueScope, object], AdapterResult] = _fetch_official_issue,
    write_report: Callable[[IssueScope, object, Path], bool] = _write_report,
) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.write_report and args.data_dir is None:
        parser.error("--data-dir is required with --write-report")
    try:
        scope = IssueScope(args.provider, args.journal, args.volume, args.issue)
    except ValueError as exc:
        parser.error(str(exc))

    configure_diagnostics(
        diagnostics_dir=str(args.diagnostics_dir),
        all_attempts=True,
    )
    try:
        with runtime_factory(
            provider=scope.provider,
            profile_root=args.profile_root,
            snapshot_root=args.snapshot_root,
        ) as runtime:
            result = fetch_issue(scope, runtime)
            if args.write_report and result.ok and result.entries:
                if not write_report(scope, runtime, args.data_dir):
                    result = AdapterResult(
                        entries={},
                        outcome=FETCH_FAILED,
                        provider=scope.provider,
                        source="journal",
                        message="verified issue could not be written",
                        metadata=result.metadata,
                    )
    except Exception as exc:
        result = AdapterResult(
            entries={},
            outcome=FETCH_FAILED,
            provider=scope.provider,
            source="journal",
            message=f"{type(exc).__name__}: {exc}",
        )

    print(
        json.dumps(
            _observation(scope, result, args.diagnostics_dir),
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if result.ok and bool(result.entries) else 1


if __name__ == "__main__":
    raise SystemExit(main())

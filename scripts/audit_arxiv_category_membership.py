#!/usr/bin/env python3
"""Compare durable arXiv Markdown ledgers with exact official membership."""

from __future__ import annotations

import argparse
from datetime import date, timedelta
import json
from pathlib import Path
import re
import sys
from typing import Callable

from papertrack.adapter_result import AdapterResult
from papertrack.arxiv_index_fetch import query_arxiv_result
from papertrack.codex import get_query_args, official_arxiv_category


_ARXIV_HEADING = re.compile(r"^###\s*(arXiv:\S+)", re.MULTILINE)


def _report_candidates(data_dir: Path, category: str, day: date) -> list[Path]:
    root = data_dir / "arxiv"
    category_root = root / category
    year = f"{day.year:04d}"
    month = f"{day.month:02d}"
    day_name = f"{day.day:02d}"
    filename = f"{day_name}.md"
    return [
        category_root / year / month / filename,
        category_root / year / month / day_name / category / filename,
        category_root / year / month / category / filename,
        root / year / month / day_name / category / filename,
        root / year / month / category / filename,
    ]


def _find_report_path(data_dir: Path, category: str, day: date) -> Path | None:
    matches = [
        path for path in _report_candidates(data_dir, category, day)
        if path.is_file()
    ]
    if len(matches) > 1:
        joined = ", ".join(str(path) for path in matches)
        raise ValueError(
            f"multiple arXiv reports found for {category} {day.isoformat()}: {joined}"
        )
    return matches[0] if matches else None


def _local_ids(path: Path) -> set[str]:
    return set(_ARXIV_HEADING.findall(path.read_text(encoding="utf-8")))


def _days(from_date: date, to_date: date):
    day = from_date
    while day <= to_date:
        yield day
        day += timedelta(days=1)


def audit_category_membership(
    *,
    data_dir: Path,
    categories: list[str],
    from_date: date,
    to_date: date,
    request_interval: float,
    query: Callable[..., AdapterResult] = query_arxiv_result,
) -> dict[str, list[dict]]:
    """Audit existing reports without modifying them or creating new reports."""
    if from_date > to_date:
        raise ValueError("from_date must not be after to_date")
    if request_interval < 0:
        raise ValueError("request_interval must be non-negative")

    query_args = get_query_args()
    unknown = sorted(set(categories) - set(query_args))
    if unknown:
        raise ValueError(f"unknown arXiv categories: {', '.join(unknown)}")

    scopes = []
    for category in sorted(set(categories)):
        for day in _days(from_date, to_date):
            report_path = _find_report_path(data_dir, category, day)
            if report_path is not None:
                scopes.append((category, day, report_path))
    manifest: dict[str, list[dict]] = {
        "exact": [],
        "missing_official": [],
        "non_member_local": [],
        "fetch_failed": [],
    }

    for category, day, report_path in scopes:
        day_string = day.isoformat()
        next_day_string = (day + timedelta(days=1)).isoformat()
        result = query(
            day_string,
            next_day_string,
            query_args[category],
            required_category=official_arxiv_category(category),
            request_delay=request_interval,
        )
        if not result.ok:
            manifest["fetch_failed"].append({
                "category": category,
                "date": day_string,
                "outcome": result.outcome,
                "message": result.message,
            })
        else:
            local_ids = _local_ids(report_path)
            official_ids = set(result.entries)
            common = {
                "category": category,
                "date": day_string,
                "local_ids": sorted(local_ids),
                "official_ids": sorted(official_ids),
            }
            missing_ids = sorted(official_ids - local_ids)
            non_member_ids = sorted(local_ids - official_ids)
            if not missing_ids and not non_member_ids:
                manifest["exact"].append(common)
            if missing_ids:
                manifest["missing_official"].append({
                    "category": category,
                    "date": day_string,
                    "missing_ids": missing_ids,
                    "local_ids": common["local_ids"],
                    "official_ids": common["official_ids"],
                })
            if non_member_ids:
                manifest["non_member_local"].append({
                    "category": category,
                    "date": day_string,
                    "non_member_ids": non_member_ids,
                    "local_ids": common["local_ids"],
                    "official_ids": common["official_ids"],
                })

    return manifest


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only audit of local arXiv reports against official results",
    )
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--categories", required=True)
    parser.add_argument("--from-date", required=True, type=_parse_date)
    parser.add_argument("--to-date", required=True, type=_parse_date)
    parser.add_argument("--request-interval", type=float, default=10.0)
    parser.add_argument("--output", type=Path)
    return parser


def _write_manifest(path: Path, manifest: dict[str, list[dict]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _validate_output_path(path: Path, data_dir: Path) -> None:
    output_path = path.resolve()
    arxiv_ledger = (data_dir / "arxiv").resolve()
    if output_path == arxiv_ledger or output_path.is_relative_to(arxiv_ledger):
        raise ValueError("manifest output must be outside the arXiv ledger")
    if path.suffix.lower() in {".md", ".markdown"}:
        raise ValueError("manifest output must not be Markdown")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    categories = [item.strip() for item in args.categories.split(",") if item.strip()]
    if not categories:
        raise SystemExit("--categories must contain at least one category")
    try:
        if args.output:
            _validate_output_path(args.output, args.data_dir)
        manifest = audit_category_membership(
            data_dir=args.data_dir,
            categories=categories,
            from_date=args.from_date,
            to_date=args.to_date,
            request_interval=args.request_interval,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    if args.output:
        _write_manifest(args.output, manifest)
    else:
        json.dump(manifest, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

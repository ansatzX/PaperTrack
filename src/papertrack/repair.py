"""Interactive repair workflow for damaged generated reports."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .codex import journal_configs, query_args
from .diagnostics import record_repair_event
from .migration import classify_report_file, validate_report_file
from .zotero_query import ZoteroQuery


@dataclass(frozen=True)
class RepairCandidate:
    path: str
    source: str
    reason: str
    provider: str = ""
    journal_slug: str = ""
    volume: str = ""
    issue: str = ""
    category: str = ""
    report_date: str = ""


@dataclass(frozen=True)
class RepairResult:
    path: str
    source: str
    outcome: str
    reason: str
    fetch_attempted: bool = False
    message: str = ""


def collect_repair_candidates(data_dir: str) -> list[RepairCandidate]:
    """Collect damaged or missing reports that can be repaired by refetching."""
    root = Path(data_dir)
    candidates: dict[str, RepairCandidate] = {}

    if root.exists():
        for path in sorted(root.rglob("*.md")):
            result = classify_report_file(str(path), data_dir=data_dir)
            if result.status != "corrupt":
                continue
            candidate = _candidate_from_path(path, data_dir, result.source, result.status)
            if candidate is not None:
                candidates[candidate.path] = candidate

    for candidate in _state_missing_candidates(data_dir):
        candidates.setdefault(candidate.path, candidate)

    return list(candidates.values())


def repair_reports(
    data_dir: str,
    candidates: list[RepairCandidate],
    zotero: ZoteroQuery | None = None,
    assume_yes: bool = False,
    no_fetch: bool = False,
    input_func: Callable[[str], str] = input,
) -> list[RepairResult]:
    """Repair candidates after one explicit fetch decision."""
    if not candidates:
        return []
    if no_fetch:
        return [_defer(candidate, "fetch disabled by user") for candidate in candidates]

    if not assume_yes:
        answer = input_func(
            f"Fetch authoritative sources to repair {len(candidates)} report(s)? [y/N] "
        )
        if answer.strip().lower() not in {"y", "yes"}:
            return [_defer(candidate, "fetch declined by user") for candidate in candidates]

    results: list[RepairResult] = []
    for candidate in candidates:
        results.append(_repair_one(data_dir, candidate, zotero))
    return results


def _repair_one(
    data_dir: str,
    candidate: RepairCandidate,
    zotero: ZoteroQuery | None,
) -> RepairResult:
    try:
        if candidate.source == "journal":
            _repair_journal(data_dir, candidate, zotero)
        elif candidate.source == "arxiv":
            _repair_arxiv(data_dir, candidate, zotero)
        else:
            return _unresolved(candidate, "unsupported repair source")
    except Exception as exc:
        return _unresolved(candidate, repr(exc), fetch_attempted=True)

    if validate_report_file(candidate.path, source=candidate.source):
        return _fixed(candidate)
    return _unresolved(candidate, "report is still missing or invalid", fetch_attempted=True)


def _repair_journal(
    data_dir: str,
    candidate: RepairCandidate,
    zotero: ZoteroQuery | None,
) -> None:
    from .report import filter_journal_to_md

    _, cfg = _journal_config(candidate.journal_slug)
    provider = cfg.get("provider") or candidate.provider or ("acs" if cfg.get("acs_code") else "")
    md_folder = os.path.join(data_dir, provider or "journals")
    filter_journal_to_md(
        journal_name=cfg["name"],
        journal_slug=cfg["slug"],
        volume=candidate.volume,
        issue=candidate.issue,
        issn=cfg["issn"],
        md_folder=md_folder,
        zotero=zotero,
        acs_code=cfg.get("acs_code", ""),
        provider=provider,
    )


def _repair_arxiv(
    data_dir: str,
    candidate: RepairCandidate,
    zotero: ZoteroQuery | None,
) -> None:
    from .calendar_plan import parse_iso_date
    from .report import filter_arxiv_days_to_md

    category = candidate.category
    if category not in query_args:
        raise RuntimeError(f"Category '{category}' not supported")
    filter_arxiv_days_to_md(
        days=[parse_iso_date(candidate.report_date)],
        md_folder=os.path.join(data_dir, "arxiv", category),
        query_args=query_args[category],
        category=category,
        zotero=zotero,
    )


def _candidate_from_path(
    path: Path,
    data_dir: str,
    source: str,
    reason: str,
) -> RepairCandidate | None:
    parts = _relative_parts(path, data_dir)
    if source == "arxiv" and len(parts) >= 5 and parts[0] == "arxiv":
        return RepairCandidate(
            path=str(path),
            source="arxiv",
            reason=reason,
            category=parts[1],
            report_date=f"{parts[2]}-{parts[3]}-{path.stem}",
        )
    if source == "journal" and len(parts) >= 4:
        return RepairCandidate(
            path=str(path),
            source="journal",
            reason=reason,
            provider=parts[-4],
            journal_slug=parts[-3],
            volume=parts[-2],
            issue=path.stem,
        )
    return None


def _state_missing_candidates(data_dir: str) -> list[RepairCandidate]:
    state_path = Path(os.getcwd()) / ".papertrack_state.json"
    if not state_path.exists():
        return []
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []

    candidates: list[RepairCandidate] = []
    slug_to_cfg = {cfg["slug"]: cfg for cfg in journal_configs.values()}
    for slug, journal_state in state.items():
        cfg = slug_to_cfg.get(slug)
        if not cfg:
            continue
        provider = cfg.get("provider") or ("acs" if cfg.get("acs_code") else "journals")
        for pair in journal_state.get("processed", []):
            if len(pair) != 2:
                continue
            volume, issue = str(pair[0]), str(pair[1])
            path = Path(data_dir) / provider / slug / volume / f"{issue}.md"
            if path.exists() and classify_report_file(str(path), data_dir=data_dir).status != "corrupt":
                continue
            candidates.append(
                RepairCandidate(
                    path=str(path),
                    source="journal",
                    reason="missing" if not path.exists() else "corrupt",
                    provider=provider,
                    journal_slug=slug,
                    volume=volume,
                    issue=issue,
                )
            )
    return candidates


def _journal_config(journal_slug: str) -> tuple[str, dict[str, str]]:
    for key, cfg in journal_configs.items():
        if cfg.get("slug") == journal_slug:
            return key, cfg
    raise RuntimeError(f"Journal '{journal_slug}' not supported")


def _fixed(candidate: RepairCandidate) -> RepairResult:
    result = RepairResult(
        path=candidate.path,
        source=candidate.source,
        outcome="fixed",
        reason=candidate.reason,
        fetch_attempted=True,
    )
    _record(candidate, result)
    return result


def _unresolved(
    candidate: RepairCandidate,
    message: str,
    fetch_attempted: bool = False,
) -> RepairResult:
    result = RepairResult(
        path=candidate.path,
        source=candidate.source,
        outcome="unresolved",
        reason=candidate.reason,
        fetch_attempted=fetch_attempted,
        message=message,
    )
    _record(candidate, result)
    return result


def _defer(candidate: RepairCandidate, message: str) -> RepairResult:
    result = RepairResult(
        path=candidate.path,
        source=candidate.source,
        outcome="deferred",
        reason=candidate.reason,
        fetch_attempted=False,
        message=message,
    )
    _record(candidate, result)
    return result


def _record(candidate: RepairCandidate, result: RepairResult) -> None:
    event = {
        "path": candidate.path,
        "source": candidate.source,
        "reason": candidate.reason,
        "outcome": result.outcome,
        "fetch_attempted": result.fetch_attempted,
    }
    if candidate.source == "journal":
        event.update({
            "provider": candidate.provider,
            "journal": candidate.journal_slug,
            "volume": candidate.volume,
            "issue": candidate.issue,
        })
    elif candidate.source == "arxiv":
        event.update({
            "category": candidate.category,
            "date": candidate.report_date,
        })
    if result.message:
        event["message"] = result.message
    record_repair_event(event)


def _relative_parts(path: Path, data_dir: str) -> tuple[str, ...]:
    try:
        return path.relative_to(data_dir).parts
    except ValueError:
        return path.parts

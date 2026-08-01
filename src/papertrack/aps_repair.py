"""Single-owner, three-phase APS issue/abstract repair protocol."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
import tempfile
from typing import Iterable

from .adapter_result import CIRCUIT_OPEN, SUCCESS
from .aps_fetch import fetch_aps_recent_page, query_aps_issue_result
from .identity import normalize_doi
from .journal_entry import JournalEntry
from .report import write_journal_entries_to_md


SCHEMA_VERSION = 1
ISSUE_PENDING = "pending"
ISSUE_CACHED = "cached"
ISSUE_RETRYABLE = "retryable_failure"
ABSTRACT_PENDING = "pending"
ABSTRACT_COMPLETE = "complete"
ABSTRACT_UNRESOLVED = "unresolved"
RENDER_PENDING = "pending"
RENDERED = "rendered"


@dataclass
class IssueRepairState:
    volume: str
    issue: str
    issue_status: str = ISSUE_PENDING
    abstract_status: str = ABSTRACT_PENDING
    render_status: str = RENDER_PENDING
    dois: list[str] = field(default_factory=list)
    earliest_date: str = ""
    latest_date: str = ""
    abstracts: dict[str, str] = field(default_factory=dict)
    message: str = ""

    @property
    def key(self) -> str:
        return f"{self.volume}/{self.issue}"


@dataclass
class ApsRepairPlan:
    journal_slug: str
    issues: dict[str, IssueRepairState]
    schema_version: int = SCHEMA_VERSION

    @classmethod
    def from_candidates(
        cls, journal_slug: str, candidates: Iterable[tuple[str, str]]
    ) -> "ApsRepairPlan":
        return cls(
            journal_slug=journal_slug,
            issues={
                f"{volume}/{issue}": IssueRepairState(volume, issue)
                for volume, issue in candidates
            },
        )

    def to_json(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "journal_slug": self.journal_slug,
            "issues": {key: asdict(state) for key, state in self.issues.items()},
        }

    @classmethod
    def from_json(cls, payload: dict) -> "ApsRepairPlan":
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("unsupported APS repair plan schema")
        journal_slug = payload.get("journal_slug")
        records = payload.get("issues")
        if not isinstance(journal_slug, str) or not isinstance(records, dict):
            raise ValueError("invalid APS repair plan")
        issues = {
            key: IssueRepairState(**record)
            for key, record in records.items()
            if isinstance(key, str) and isinstance(record, dict)
        }
        if len(issues) != len(records):
            raise ValueError("invalid APS repair plan issue record")
        return cls(journal_slug, issues)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=path.parent,
                prefix=f".{path.name}.", suffix=".tmp", delete=False,
            ) as stream:
                temporary = Path(stream.name)
                json.dump(self.to_json(), stream, indent=2)
                stream.write("\n")
                stream.flush()
            temporary.replace(path)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    @classmethod
    def load_or_create(
        cls, path: Path, journal_slug: str, candidates: Iterable[tuple[str, str]]
    ) -> "ApsRepairPlan":
        if path.exists():
            plan = cls.from_json(json.loads(path.read_text(encoding="utf-8")))
            if plan.journal_slug != journal_slug:
                raise ValueError("APS repair plan journal mismatch")
            for volume, issue in candidates:
                plan.issues.setdefault(
                    f"{volume}/{issue}", IssueRepairState(volume, issue)
                )
            return plan
        return cls.from_candidates(journal_slug, candidates)


def cache_issues(
    plan: ApsRepairPlan,
    journal_name: str,
    runtime,
    state_path: Path,
) -> None:
    """Phase 1: cache authoritative issue membership, newest first."""
    ordered = sorted(plan.issues.values(), key=_issue_sort_key, reverse=True)
    for state in ordered:
        if state.issue_status == ISSUE_CACHED:
            continue
        result = query_aps_issue_result(
            plan.journal_slug, state.volume, state.issue, journal_name, runtime,
            snapshot_only=True,
        )
        if result.outcome == CIRCUIT_OPEN:
            state.message = result.message
            plan.save(state_path)
            return
        if result.outcome != SUCCESS:
            state.issue_status = ISSUE_RETRYABLE
            state.message = result.message
            plan.save(state_path)
            continue
        _record_inventory(state, result.entries)
        state.issue_status = ISSUE_CACHED
        state.message = ""
        plan.save(state_path)


def index_recent_abstracts(
    plan: ApsRepairPlan,
    journal_name: str,
    runtime,
    state_path: Path,
) -> None:
    """Phase 2: one shared chronological recent stream joined by exact DOI."""
    if any(item.issue_status != ISSUE_CACHED for item in plan.issues.values()):
        return
    doi_to_issues: dict[str, list[IssueRepairState]] = {}
    for state in plan.issues.values():
        result = query_aps_issue_result(
            plan.journal_slug, state.volume, state.issue, journal_name, runtime
        )
        if result.outcome != SUCCESS:
            state.issue_status = ISSUE_RETRYABLE
            state.message = result.message
            plan.save(state_path)
            return
        for doi in state.dois:
            normalized = normalize_doi(doi)
            if normalized not in state.abstracts:
                doi_to_issues.setdefault(normalized, []).append(state)

    page = 1
    while doi_to_issues:
        parsed, outcome = fetch_aps_recent_page(plan.journal_slug, page, runtime)
        if parsed is None:
            for states in doi_to_issues.values():
                for state in states:
                    state.message = f"recent page {page}: {outcome}"
            plan.save(state_path)
            return
        for doi, abstract in parsed.abstracts.items():
            normalized = normalize_doi(doi)
            for state in doi_to_issues.pop(normalized, []):
                state.abstracts[normalized] = abstract
        for state in plan.issues.values():
            remaining = [doi for doi in state.dois if normalize_doi(doi) not in state.abstracts]
            if not remaining:
                state.abstract_status = ABSTRACT_COMPLETE
            elif parsed.oldest_published_date and state.earliest_date and (
                parsed.oldest_published_date <= state.earliest_date
            ):
                state.abstract_status = ABSTRACT_UNRESOLVED
                for doi in remaining:
                    doi_to_issues.pop(normalize_doi(doi), None)
        plan.save(state_path)
        if not parsed.has_next_page:
            break
        page += 1
    for state in plan.issues.values():
        if state.abstract_status == ABSTRACT_PENDING:
            state.abstract_status = ABSTRACT_UNRESOLVED
    plan.save(state_path)


def render_complete_issues(
    plan: ApsRepairPlan,
    journal_name: str,
    issn: str,
    md_folder: str,
    runtime,
    state_path: Path,
) -> None:
    """Phase 3: render complete reports only; this phase has no live fetches."""
    for state in plan.issues.values():
        if state.abstract_status != ABSTRACT_COMPLETE or state.render_status == RENDERED:
            continue
        result = query_aps_issue_result(
            plan.journal_slug, state.volume, state.issue, journal_name, runtime,
            snapshot_only=True,
        )
        if result.outcome != SUCCESS:
            raise RuntimeError("render phase requires a cached APS issue snapshot")
        for doi, entry in result.entries.items():
            entry.abstract = state.abstracts[normalize_doi(doi)]
        written = write_journal_entries_to_md(
            journal_name=journal_name, journal_slug=plan.journal_slug,
            volume=state.volume, issue=state.issue, entries=result.entries,
            md_folder=md_folder, provider="aps",
        )
        if written:
            state.render_status = RENDERED
            plan.save(state_path)


def _record_inventory(
    state: IssueRepairState, entries: dict[str, JournalEntry]
) -> None:
    dates = sorted(entry.date for entry in entries.values() if entry.date)
    state.dois = sorted(normalize_doi(doi) for doi in entries)
    state.earliest_date = dates[0] if dates else ""
    state.latest_date = dates[-1] if dates else ""


def _issue_sort_key(state: IssueRepairState) -> tuple[int, int]:
    """APS volume and issue identifiers are numeric; invalid legacy values sort last."""
    try:
        return int(state.volume), int(state.issue)
    except ValueError:
        return -1, -1

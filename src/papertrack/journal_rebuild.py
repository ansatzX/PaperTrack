"""Resumable, targeted rebuilds of audited formal journal issues."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import fcntl
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
from typing import Iterable

from .codex import journal_configs
from .report import filter_journal_to_md
from .source_policy import CrossrefMode, SourcePolicy


SCHEMA_VERSION = 1
RESOLVED = "resolved"
OFFICIAL_SUCCESS_UNRESOLVED = "official_success_unresolved"
FETCH_FAILED = "fetch_failed"
_TERMINAL_OUTCOMES = {
    RESOLVED,
    OFFICIAL_SUCCESS_UNRESOLVED,
    FETCH_FAILED,
}


@dataclass(frozen=True)
class RebuildCandidate:
    provider: str
    journal: str
    volume: str
    issue: str
    relative_path: str
    classification: str
    priority: int
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider", str(self.provider))
        object.__setattr__(self, "journal", str(self.journal))
        object.__setattr__(self, "volume", str(self.volume))
        object.__setattr__(self, "issue", str(self.issue))
        object.__setattr__(self, "relative_path", str(self.relative_path))
        object.__setattr__(self, "reasons", tuple(self.reasons))
        if self.relative_path != self.expected_relative_path:
            raise ValueError(
                "relative_path disagrees with provider/journal/volume/issue: "
                f"expected {self.expected_relative_path!r}, got {self.relative_path!r}"
            )

    @property
    def expected_relative_path(self) -> str:
        return PurePosixPath(
            self.provider,
            self.journal,
            self.volume,
            f"{self.issue}.md",
        ).as_posix()

    @property
    def key(self) -> str:
        return self.relative_path


@dataclass(frozen=True)
class ProgressRecord:
    outcome: str

    def __post_init__(self) -> None:
        if self.outcome not in _TERMINAL_OUTCOMES:
            raise ValueError(f"unknown journal rebuild outcome: {self.outcome!r}")


@dataclass
class ProviderProgress:
    provider: str
    issues: dict[str, ProgressRecord]
    schema_version: int = SCHEMA_VERSION

    def to_json(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "provider": self.provider,
            "issues": {
                key: asdict(record)
                for key, record in self.issues.items()
            },
        }


@dataclass(frozen=True)
class RebuildSummary:
    provider: str
    ordered_candidate_keys: tuple[str, ...]
    attempted_keys: tuple[str, ...]
    skipped_keys: tuple[str, ...]
    outcomes: dict[str, str]

    @property
    def resolved_keys(self) -> tuple[str, ...]:
        return tuple(
            key for key in self.ordered_candidate_keys
            if self.outcomes.get(key) == RESOLVED
        )

    @property
    def unresolved_keys(self) -> tuple[str, ...]:
        return tuple(
            key for key in self.ordered_candidate_keys
            if self.outcomes.get(key) == OFFICIAL_SUCCESS_UNRESOLVED
        )

    @property
    def fetch_failed_keys(self) -> tuple[str, ...]:
        return tuple(
            key for key in self.ordered_candidate_keys
            if self.outcomes.get(key) == FETCH_FAILED
        )

    @property
    def ok(self) -> bool:
        return not self.unresolved_keys and not self.fetch_failed_keys


def _ordered(candidates: Iterable[RebuildCandidate]) -> list[RebuildCandidate]:
    return sorted(candidates, key=lambda candidate: candidate.priority)


def materialize_manifest(
    path: str | os.PathLike,
    candidates: Iterable[RebuildCandidate],
) -> None:
    """Write a validated, stable-priority runtime manifest."""
    manifest_path = Path(path)
    issues = []
    for candidate in _ordered(candidates):
        item = asdict(candidate)
        item["reasons"] = list(candidate.reasons)
        issues.append(item)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "audit": {"kind": "offline_markdown_quality"},
        "issues": issues,
    }
    _write_json_atomic(manifest_path, payload)


def load_provider_candidates(
    path: str | os.PathLike,
    provider: str,
) -> list[RebuildCandidate]:
    """Load and validate one provider's candidates from a runtime manifest."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported journal rebuild manifest schema")
    if payload.get("audit") != {"kind": "offline_markdown_quality"}:
        raise ValueError("unexpected journal rebuild manifest audit kind")
    candidates = [RebuildCandidate(**item) for item in payload.get("issues", [])]
    return [
        candidate
        for candidate in _ordered(candidates)
        if candidate.provider == provider
    ]


def _progress_path(diagnostics_dir: Path, provider: str) -> Path:
    return diagnostics_dir / f"journal_rebuild_{provider}_state.json"


def _load_progress(path: Path, provider: str) -> ProviderProgress:
    if not path.exists():
        return ProviderProgress(provider=provider, issues={})
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported journal rebuild progress schema")
    if payload.get("provider") != provider:
        raise ValueError("journal rebuild progress provider mismatch")
    issues = {
        key: ProgressRecord(**record)
        for key, record in payload.get("issues", {}).items()
    }
    return ProviderProgress(provider=provider, issues=issues)


def _write_progress(path: Path, progress: ProviderProgress) -> None:
    """Durably replace progress JSON using a temporary file beside it."""
    _write_json_atomic(path, progress.to_json())


def _write_json_atomic(path: Path, payload: dict) -> None:
    """Durably replace JSON using a temporary file in the target directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            json.dump(payload, temporary, indent=2)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
        raise


def _materialize_provider_manifest(
    diagnostics_dir: Path,
    provider: str,
    candidates: list[RebuildCandidate],
) -> None:
    """Merge one worker's definitions into the shared runtime manifest."""
    diagnostics_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = diagnostics_dir / "journal_rebuild_manifest.json"
    lock_path = diagnostics_dir / ".journal_rebuild_manifest.lock"
    with lock_path.open("a", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        retained: list[RebuildCandidate] = []
        if manifest_path.exists():
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            if payload.get("schema_version") != SCHEMA_VERSION:
                raise ValueError("unsupported journal rebuild manifest schema")
            if payload.get("audit") != {"kind": "offline_markdown_quality"}:
                raise ValueError("unexpected journal rebuild manifest audit kind")
            retained = [
                candidate
                for candidate in (
                    RebuildCandidate(**item) for item in payload.get("issues", [])
                )
                if candidate.provider != provider
            ]
        materialize_manifest(manifest_path, (*retained, *candidates))


@dataclass(frozen=True)
class _GeneratedEntry:
    doi: str
    title: str
    authors: str
    abstract: str

    @property
    def generated_metadata(self) -> str:
        return "\n".join((self.title, self.authors, self.abstract))


_ENTRY_HEADING = re.compile(r"^###\s+(10\.\S+)\s*$", re.MULTILINE)
_EXEMPT_TITLE = re.compile(
    r"^\s*(?:"
    r"addendum|comment(?:\s+on)?|correction(?:\s+to)?|corrigendum|editorial|"
    r"erratum|expression\s+of\s+concern|"
    r"publisher(?:['\N{RIGHT SINGLE QUOTATION MARK}]s)?\s+note|"
    r"reply(?:\s+to)?|retraction(?:\s+of)?"
    r")\b",
    re.IGNORECASE,
)
_RAW_HTML = re.compile(r"</?[A-Za-z][^>]*>")
_RAW_MATHML = re.compile(r"<(?:mml:)?math\b", re.IGNORECASE)


def _field(entry_body: str, name: str) -> str:
    match = re.search(rf"^{re.escape(name)}:\s*(.*?)\s*$", entry_body, re.MULTILINE)
    return match.group(1) if match else ""


def _abstract(entry_body: str) -> str:
    marker = re.search(r"^Abstract:\s*$", entry_body, re.MULTILINE)
    if not marker:
        return ""
    lines = []
    for line in entry_body[marker.end():].splitlines():
        quote = re.match(r"^>\s?(.*)$", line)
        if not quote:
            if lines or line.strip():
                break
            continue
        value = quote.group(1).strip()
        if value and not value.startswith("[!quote]"):
            lines.append(value)
    return "\n".join(lines)


def _generated_entries(content: str) -> tuple[_GeneratedEntry, ...]:
    matches = list(_ENTRY_HEADING.finditer(content))
    entries = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        body = content[match.end():end]
        entries.append(
            _GeneratedEntry(
                doi=match.group(1),
                title=_field(body, "Title"),
                authors=_field(body, "Authors"),
                abstract=_abstract(body),
            )
        )
    return tuple(entries)


def _targeted_defect_remains(content: str, reason: str) -> bool:
    entries = _generated_entries(content)
    if reason == "raw_html":
        return any(_RAW_HTML.search(entry.generated_metadata) for entry in entries)
    if reason == "raw_mathml":
        return any(_RAW_MATHML.search(entry.generated_metadata) for entry in entries)
    if reason == "lowercase_crossref_doi":
        return any(entry.doi.startswith("10.1103/physrevlett.") for entry in entries)
    if reason == "whole_issue_missing_abstracts":
        return not entries or all(not entry.abstract for entry in entries)
    if reason == "ordinary_article_missing_abstract":
        return any(
            not entry.abstract and not _EXEMPT_TITLE.search(entry.title)
            for entry in entries
        ) or not entries
    raise ValueError(f"unsupported journal rebuild reason: {reason!r}")


def _assess_report(path: Path, candidate: RebuildCandidate) -> str:
    if not path.exists():
        return OFFICIAL_SUCCESS_UNRESOLVED
    content = path.read_text(encoding="utf-8")
    if any(_targeted_defect_remains(content, reason) for reason in candidate.reasons):
        return OFFICIAL_SUCCESS_UNRESOLVED
    return RESOLVED


def _restore_report(path: Path, original: bytes | None) -> None:
    if original is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(original)


def _journal_config(candidate: RebuildCandidate) -> dict[str, str]:
    try:
        config = journal_configs[candidate.journal]
    except KeyError as exc:
        raise ValueError(f"unknown journal config: {candidate.journal!r}") from exc
    configured_provider = config.get("provider") or (
        "acs" if config.get("acs_code") else ""
    )
    if configured_provider != candidate.provider:
        raise ValueError(
            f"candidate provider {candidate.provider!r} disagrees with journal config "
            f"provider {configured_provider!r}"
        )
    return config


def run_provider_rebuild(
    provider: str,
    candidates: Iterable[RebuildCandidate],
    data_dir: str | os.PathLike,
    diagnostics_dir: str | os.PathLike,
    retry_unresolved: bool = False,
    reset_progress: bool = False,
    dry_run: bool = False,
) -> RebuildSummary:
    """Sequentially rebuild one provider's audited issues with durable progress."""
    ordered = _ordered(candidates)
    for candidate in ordered:
        if candidate.provider != provider:
            raise ValueError(
                f"candidate {candidate.key!r} does not belong to provider {provider!r}"
            )
    ordered_keys = tuple(candidate.key for candidate in ordered)
    if dry_run:
        return RebuildSummary(provider, ordered_keys, (), (), {})

    data_path = Path(data_dir)
    diagnostics_path = Path(diagnostics_dir)
    _materialize_provider_manifest(diagnostics_path, provider, ordered)
    progress_path = _progress_path(diagnostics_path, provider)
    progress = (
        ProviderProgress(provider=provider, issues={})
        if reset_progress
        else _load_progress(progress_path, provider)
    )
    if reset_progress:
        _write_progress(progress_path, progress)

    attempted: list[str] = []
    skipped: list[str] = []
    outcomes: dict[str, str] = {}
    for candidate in ordered:
        previous = progress.issues.get(candidate.key)
        if previous and (
            previous.outcome == RESOLVED
            or (
                previous.outcome == OFFICIAL_SUCCESS_UNRESOLVED
                and not retry_unresolved
            )
        ):
            skipped.append(candidate.key)
            outcomes[candidate.key] = previous.outcome
            continue

        config = _journal_config(candidate)
        report_path = data_path / PurePosixPath(candidate.relative_path)
        original = report_path.read_bytes() if report_path.exists() else None
        attempted.append(candidate.key)
        try:
            success = filter_journal_to_md(
                journal_name=config["name"],
                journal_slug=config.get("slug", candidate.journal),
                volume=candidate.volume,
                issue=candidate.issue,
                issn=config["issn"],
                md_folder=str(data_path / provider),
                zotero=None,
                acs_code=config.get("acs_code", ""),
                provider=config.get("provider", provider),
                source_policy=SourcePolicy(CrossrefMode.METADATA),
            )
        except BaseException as exc:
            _restore_report(report_path, original)
            if not isinstance(exc, Exception):
                raise
            outcome = FETCH_FAILED
        else:
            if not success:
                _restore_report(report_path, original)
                outcome = FETCH_FAILED
            else:
                try:
                    outcome = _assess_report(report_path, candidate)
                except (OSError, UnicodeError):
                    outcome = OFFICIAL_SUCCESS_UNRESOLVED

        outcomes[candidate.key] = outcome
        progress.issues[candidate.key] = ProgressRecord(outcome)
        _write_progress(progress_path, progress)

    return RebuildSummary(
        provider=provider,
        ordered_candidate_keys=ordered_keys,
        attempted_keys=tuple(attempted),
        skipped_keys=tuple(skipped),
        outcomes=outcomes,
    )

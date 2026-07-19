"""Resumable, targeted rebuilds of audited formal journal issues."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import fcntl
import json
import logging
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile
from typing import Iterable

from .codex import journal_configs
from .report import filter_journal_to_md
from .provider_runtime import ProviderRuntime, ProviderRunStats
from .source_policy import SourcePolicy


logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
RESOLVED = "resolved"
OFFICIAL_SUCCESS_UNRESOLVED = "official_success_unresolved"
FETCH_FAILED = "fetch_failed"
_TERMINAL_OUTCOMES = {
    RESOLVED,
    OFFICIAL_SUCCESS_UNRESOLVED,
    FETCH_FAILED,
}
_ASSESSMENT_REASONS = {
    "lowercase_crossref_doi",
    "ordinary_article_missing_abstract",
    "raw_html",
    "raw_mathml",
    "whole_issue_missing_abstracts",
}
_INFORMATIONAL_REASONS = {"jcim_priority"}
_SUPPORTED_REASONS = _ASSESSMENT_REASONS | _INFORMATIONAL_REASONS
_CANDIDATE_FIELDS = {
    "provider",
    "journal",
    "volume",
    "issue",
    "relative_path",
    "classification",
    "priority",
    "reasons",
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
        for field_name in (
            "provider",
            "journal",
            "volume",
            "issue",
            "relative_path",
            "classification",
        ):
            value = getattr(self, field_name)
            if type(value) is not str or not value:
                raise ValueError(f"candidate {field_name} must be a non-empty string")
        if type(self.priority) is not int:
            raise ValueError("candidate priority must be an integer")
        if not isinstance(self.reasons, (tuple, list)) or not self.reasons:
            raise ValueError("candidate reasons must be a non-empty sequence")
        reasons = tuple(self.reasons)
        if any(type(reason) is not str for reason in reasons):
            raise ValueError("candidate reasons must contain only strings")
        unknown = set(reasons) - _SUPPORTED_REASONS
        if unknown:
            raise ValueError(f"candidate reasons contain unsupported values: {sorted(unknown)!r}")
        object.__setattr__(self, "reasons", reasons)
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
        if type(self.outcome) is not str:
            raise ValueError("journal rebuild outcome must be a string")
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
    runtime_stats: ProviderRunStats = field(default_factory=ProviderRunStats)

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
    return sorted(
        candidates,
        key=lambda candidate: (candidate.priority, candidate.relative_path),
    )


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
    candidates = _load_manifest_candidates(Path(path))
    return [
        candidate
        for candidate in _ordered(candidates)
        if candidate.provider == provider
    ]


def _read_json_object(path: Path, kind: str) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid {kind} {path}: JSON read failed: {exc}") from exc
    if type(payload) is not dict:
        raise ValueError(f"invalid {kind} {path}: top level must be an object")
    return payload


def _require_exact_keys(
    payload: dict,
    expected: set[str],
    context: str,
    level: str = "top level",
) -> None:
    actual = set(payload)
    if actual == expected:
        return
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    raise ValueError(
        f"invalid {context}: {level} keys must be exactly {sorted(expected)!r}; "
        f"missing={missing!r}, extra={extra!r}"
    )


def _load_manifest_candidates(path: Path) -> list[RebuildCandidate]:
    payload = _read_json_object(path, "journal rebuild manifest")
    context = f"journal rebuild manifest {path}"
    _require_exact_keys(payload, {"schema_version", "audit", "issues"}, context)
    if type(payload.get("schema_version")) is not int:
        raise ValueError(f"invalid {context}: schema_version must be an integer")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"invalid {context}: unsupported schema_version")
    if type(payload.get("audit")) is not dict:
        raise ValueError(f"invalid {context}: audit must be an object")
    if payload.get("audit") != {"kind": "offline_markdown_quality"}:
        raise ValueError(
            f"invalid {context}: audit must be exactly "
            "{'kind': 'offline_markdown_quality'}"
        )
    if "issues" not in payload or type(payload["issues"]) is not list:
        raise ValueError(f"invalid {context}: issues must be a list")
    candidates = []
    for index, item in enumerate(payload["issues"]):
        item_context = f"{context} issues[{index}]"
        if type(item) is not dict:
            raise ValueError(f"invalid {item_context}: record must be an object")
        if set(item) != _CANDIDATE_FIELDS:
            raise ValueError(
                f"invalid {item_context}: record must contain exactly "
                "the candidate fields"
            )
        if type(item["reasons"]) is not list:
            raise ValueError(f"invalid {item_context}.reasons: must be a list")
        try:
            candidates.append(RebuildCandidate(**item))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid {item_context}: {exc}") from exc
    return candidates


def _progress_path(diagnostics_dir: Path, provider: str) -> Path:
    return diagnostics_dir / f"journal_rebuild_{provider}_state.json"


def _load_progress(path: Path, provider: str) -> ProviderProgress:
    if not path.exists():
        return ProviderProgress(provider=provider, issues={})
    payload = _read_json_object(path, "journal rebuild progress")
    context = f"journal rebuild progress {path}"
    _require_exact_keys(payload, {"schema_version", "provider", "issues"}, context)
    if type(payload.get("schema_version")) is not int:
        raise ValueError(f"invalid {context}: schema_version must be an integer")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"invalid {context}: unsupported schema_version")
    if type(payload.get("provider")) is not str:
        raise ValueError(f"invalid {context}: provider must be a string")
    if payload.get("provider") != provider:
        raise ValueError(f"invalid {context}: provider mismatch")
    if "issues" not in payload or type(payload["issues"]) is not dict:
        raise ValueError(f"invalid {context}: issues must be an object")
    issues = {}
    for key, record in payload["issues"].items():
        record_context = f"{context} issues[{key!r}]"
        if type(key) is not str or not key:
            raise ValueError(f"invalid {context}: issues keys must be strings")
        key_parts = PurePosixPath(key).parts
        if (
            len(key_parts) != 4
            or key_parts[0] != provider
            or key_parts[-1] in {".md", "..md"}
            or not key_parts[-1].endswith(".md")
            or any(part in {".", ".."} for part in key_parts)
        ):
            raise ValueError(f"invalid {record_context}: invalid provider issue path")
        if type(record) is not dict or set(record) != {"outcome"}:
            raise ValueError(
                f"invalid {record_context}: record must contain exactly outcome"
            )
        try:
            issues[key] = ProgressRecord(**record)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid {record_context}: {exc}") from exc
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
            retained = [
                candidate
                for candidate in _load_manifest_candidates(manifest_path)
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
_MATHML_ELEMENTS = frozenset({
    "annotation",
    "annotation-xml",
    "maction",
    "maligngroup",
    "malignmark",
    "math",
    "menclose",
    "merror",
    "mfenced",
    "mfrac",
    "mglyph",
    "mi",
    "mlabeledtr",
    "mlongdiv",
    "mmultiscripts",
    "mn",
    "mo",
    "mover",
    "mpadded",
    "mphantom",
    "mprescripts",
    "mroot",
    "mrow",
    "ms",
    "mscarries",
    "mscarry",
    "msgroup",
    "msline",
    "mspace",
    "msqrt",
    "msrow",
    "mstack",
    "mstyle",
    "msub",
    "msubsup",
    "msup",
    "mtable",
    "mtd",
    "mtext",
    "mtr",
    "munder",
    "munderover",
    "none",
    "semantics",
})
_RAW_TAG_NAME = re.compile(r"</?\s*([A-Za-z][\w.-]*(?::[A-Za-z][\w.-]*)?)\b")


def _contains_raw_mathml(value: str) -> bool:
    for match in _RAW_TAG_NAME.finditer(value):
        tag = match.group(1).lower()
        if tag.startswith("mml:") or tag in _MATHML_ELEMENTS:
            return True
    return False


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


def _existing_collected_dois(content: str) -> set[str]:
    section = re.search(
        r"^## collected\s*$\n(.*?)^## not collected\s*$",
        content,
        re.MULTILINE | re.DOTALL,
    )
    if not section:
        return set()
    return set(_ENTRY_HEADING.findall(section.group(1)))


def _targeted_defect_remains(content: str, reason: str) -> bool:
    entries = _generated_entries(content)
    if reason in _INFORMATIONAL_REASONS:
        return False
    if reason == "raw_html":
        return any(_RAW_HTML.search(entry.generated_metadata) for entry in entries)
    if reason == "raw_mathml":
        return any(_contains_raw_mathml(entry.generated_metadata) for entry in entries)
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
        raise FileNotFoundError(
            f"truthy journal rebuild did not produce the expected report: {path}"
        )
    content = path.read_text(encoding="utf-8")
    if any(_targeted_defect_remains(content, reason) for reason in candidate.reasons):
        return OFFICIAL_SUCCESS_UNRESOLVED
    return RESOLVED


def _fsync_directory(directory: Path) -> None:
    """Fsync a directory or raise a contextual durability error."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(directory, flags)
    except OSError as exc:
        raise RuntimeError(f"unable to open directory for fsync: {directory}") from exc
    try:
        try:
            os.fsync(descriptor)
        except OSError as exc:
            raise RuntimeError(f"unable to fsync directory: {directory}") from exc
    finally:
        os.close(descriptor)


def _create_report_backup(path: Path) -> Path | None:
    """Create and fsync a same-directory backup before external mutation."""
    if not path.exists():
        return None
    backup_name: str | None = None
    try:
        with path.open("rb") as source, tempfile.NamedTemporaryFile(
            "wb",
            dir=path.parent,
            prefix=f".{path.name}.papertrack-rebuild-",
            suffix=".bak",
            delete=False,
        ) as backup:
            backup_name = backup.name
            shutil.copyfileobj(source, backup)
            os.fchmod(backup.fileno(), path.stat().st_mode)
            backup.flush()
            os.fsync(backup.fileno())
        _fsync_directory(path.parent)
        return Path(backup_name)
    except BaseException:
        if backup_name is not None:
            try:
                os.unlink(backup_name)
            except FileNotFoundError:
                pass
        raise


def _restore_report(path: Path, backup: Path | None) -> None:
    if backup is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        else:
            _fsync_directory(path.parent)
        return
    os.replace(backup, path)
    _fsync_directory(path.parent)


def _discard_report_backup(backup: Path | None) -> None:
    if backup is None:
        return
    try:
        backup.unlink()
    except FileNotFoundError:
        pass
    else:
        _fsync_directory(backup.parent)


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


def _rebuild_candidate(
    candidate: RebuildCandidate,
    data_path: Path,
    provider_runtime: ProviderRuntime,
) -> str:
    """Rebuild one candidate while preserving its prior report on failure."""
    config = _journal_config(candidate)
    report_path = data_path / PurePosixPath(candidate.relative_path)
    backup = _create_report_backup(report_path)
    try:
        collected_dois = (
            _existing_collected_dois(backup.read_text(encoding="utf-8"))
            if backup is not None
            else set()
        )
    except UnicodeError:
        collected_dois = set()
    try:
        success = filter_journal_to_md(
            journal_name=config["name"],
            journal_slug=config.get("slug", candidate.journal),
            volume=candidate.volume,
            issue=candidate.issue,
            issn=config["issn"],
            md_folder=str(data_path / candidate.provider),
            zotero=None,
            acs_code=config.get("acs_code", ""),
            provider=config.get("provider", candidate.provider),
            source_policy=SourcePolicy.from_environment(),
            existing_collected_dois=collected_dois,
            provider_runtime=provider_runtime,
        )
    except BaseException as exc:
        _restore_report(report_path, backup)
        if not isinstance(exc, Exception):
            raise
        return FETCH_FAILED

    if not success:
        _restore_report(report_path, backup)
        return FETCH_FAILED

    try:
        outcome = _assess_report(report_path, candidate)
    except BaseException as exc:
        _restore_report(report_path, backup)
        if not isinstance(exc, Exception):
            raise
        logger.warning(
            "Journal rebuild assessment failed for %s at %s; "
            "restored prior state and recorded fetch_failed: %s",
            candidate.key,
            report_path,
            exc,
        )
        return FETCH_FAILED

    _discard_report_backup(backup)
    return outcome


def run_provider_rebuild(
    provider: str,
    candidates: Iterable[RebuildCandidate],
    data_dir: str | os.PathLike,
    diagnostics_dir: str | os.PathLike,
    *,
    profile_root: str | os.PathLike,
    snapshot_root: str | os.PathLike,
    runtime_factory=ProviderRuntime,
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
    with runtime_factory(
        provider=provider,
        profile_root=profile_root,
        snapshot_root=snapshot_root,
    ) as provider_runtime:
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

            attempted.append(candidate.key)
            outcome = _rebuild_candidate(
                candidate,
                data_path,
                provider_runtime,
            )
            outcomes[candidate.key] = outcome
            progress.issues[candidate.key] = ProgressRecord(outcome)
            _write_progress(progress_path, progress)

    return RebuildSummary(
        provider=provider,
        ordered_candidate_keys=ordered_keys,
        attempted_keys=tuple(attempted),
        skipped_keys=tuple(skipped),
        outcomes=outcomes,
        runtime_stats=provider_runtime.stats,
    )

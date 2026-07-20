"""Markdown report schema migration and safe report writes."""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .diagnostics import record_all_attempts, record_migration_event
from .report_tags import arxiv_report_tag, journal_report_tag

CURRENT_ARXIV_FORMAT = "arxiv-v2"
CURRENT_JOURNAL_FORMAT = "journal-v2"
_GENERATED_TOC_IMAGE_PATTERN = (
    r"(?m)^[ \t]*!\[\|700\|700x24\]\([^\n]*\)[ \t]*$"
)


@dataclass
class MigrationResult:
    path: str
    status: str
    source: str = ""
    from_format: str = ""
    to_format: str = ""
    backup_path: str = ""
    message: str = ""


@dataclass(frozen=True)
class _TaskState:
    box: str
    suffix: str


def preserve_task_state(new_content: str, old_content: str | None) -> str:
    """Carry supported Obsidian task edits from old generated Markdown."""
    if not old_content:
        return new_content
    states = _extract_task_states(old_content)
    if not states:
        return new_content

    lines = []
    for line in new_content.splitlines(keepends=True):
        newline = ""
        body = line
        if line.endswith("\r\n"):
            body = line[:-2]
            newline = "\r\n"
        elif line.endswith("\n"):
            body = line[:-1]
            newline = "\n"
        lines.append(_apply_task_state_to_line(body, states) + newline)
    return "".join(lines)


def write_report_file(path: str, content: str, source: str) -> None:
    """Write generated Markdown with backup, validation, and rollback."""
    if not validate_report_content(content, source=source):
        raise ValueError(f"generated {source} report failed format validation")

    _atomic_write_report(Path(path), content)


def _atomic_write_report(report_path: Path, content: str) -> None:
    """Durably replace one report while retaining a rollback copy."""

    report_path.parent.mkdir(parents=True, exist_ok=True)
    backup_path = _backup_path(report_path)
    had_existing = report_path.exists()
    if had_existing:
        shutil.copy2(report_path, backup_path)
        with backup_path.open("rb") as backup:
            os.fsync(backup.fileno())
        _fsync_directory(report_path.parent)

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=report_path.parent,
            prefix=f".{report_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, report_path)
        temporary_path = None
        _fsync_directory(report_path.parent)
    except Exception:
        if had_existing and backup_path.exists():
            os.replace(backup_path, report_path)
            _fsync_directory(report_path.parent)
        elif not had_existing and report_path.exists():
            report_path.unlink()
            _fsync_directory(report_path.parent)
        raise
    else:
        if had_existing and backup_path.exists():
            backup_path.unlink()
            _fsync_directory(report_path.parent)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def migrate_reports(data_dir: str) -> list[MigrationResult]:
    """Migrate every recognized Markdown report under data_dir."""
    root = Path(data_dir)
    if not root.exists():
        return []
    results: list[MigrationResult] = []
    for path in sorted(root.rglob("*.md")):
        if path.name.endswith(".bak"):
            continue
        results.append(migrate_report_file(str(path), data_dir=data_dir))
    return results


def migrate_report_file(path: str, data_dir: str = "") -> MigrationResult:
    """Migrate one generated Markdown report to the current schema."""
    report_path = Path(path)
    if not report_path.exists():
        return MigrationResult(path=path, status="missing", message="file does not exist")

    try:
        old_content = report_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return MigrationResult(
            path=path,
            status="failed",
            message=f"could not read report: {exc}",
        )
    current_format = report_format(old_content)
    source = _detect_source(old_content, report_path, data_dir)

    if current_format == CURRENT_ARXIV_FORMAT:
        try:
            new_content = _repair_current_arxiv_identity(
                old_content,
                report_path,
                data_dir,
            )
        except ValueError as exc:
            return MigrationResult(
                path=path,
                status="failed",
                source="arxiv",
                from_format=current_format,
                to_format=current_format,
                message=str(exc),
            )
        if new_content != old_content:
            return _write_migrated_content(
                report_path=report_path,
                old_content=old_content,
                new_content=new_content,
                source="arxiv",
                from_format=current_format,
                to_format=current_format,
                status="repaired",
            )
        if not _validate_report_structure(old_content, source="arxiv"):
            return _failed_current_validation(report_path, "arxiv", current_format)
        return MigrationResult(
            path=path,
            status="current",
            source="arxiv",
            from_format=current_format,
            to_format=current_format,
        )
    if current_format == CURRENT_JOURNAL_FORMAT:
        try:
            new_content = _repair_current_journal_tags(old_content)
        except ValueError as exc:
            return MigrationResult(
                path=path,
                status="failed",
                source=source,
                from_format=current_format,
                to_format=current_format,
                message=str(exc),
            )
        if _has_markdown_images(new_content):
            new_content = _remove_markdown_images(new_content)
        if new_content != old_content:
            return _write_migrated_content(
                report_path=report_path,
                old_content=old_content,
                new_content=new_content,
                source=source,
                from_format=current_format,
                to_format=current_format,
                status="repaired",
                allow_preexisting_quality_failure=has_journal_quality_failure(
                    old_content
                ),
            )
        if not _validate_report_structure(old_content, source="journal"):
            return _failed_current_validation(report_path, source, current_format)
        return MigrationResult(
            path=path,
            status="current",
            source=source,
            from_format=current_format,
            to_format=current_format,
        )
    if not source:
        return MigrationResult(path=path, status="unsupported", message="not a PaperTrack report")
    if not _legacy_skeleton_ok(old_content, source):
        return MigrationResult(
            path=path,
            status="corrupt",
            source=source,
            from_format="legacy",
            message="missing generated report skeleton",
        )

    try:
        if source == "journal":
            new_content = _migrate_journal_legacy(old_content, report_path, data_dir)
            to_format = CURRENT_JOURNAL_FORMAT
        else:
            new_content = _migrate_arxiv_legacy(old_content, report_path, data_dir)
            to_format = CURRENT_ARXIV_FORMAT
    except ValueError as exc:
        return MigrationResult(
            path=path,
            status="failed",
            source=source,
            from_format="legacy",
            message=str(exc),
        )

    return _write_migrated_content(
        report_path=report_path,
        old_content=old_content,
        new_content=new_content,
        source=source,
        from_format="legacy",
        to_format=to_format,
        status="migrated",
    )


def classify_report_file(path: str, data_dir: str = "") -> MigrationResult:
    """Classify one report without modifying it."""
    report_path = Path(path)
    if not report_path.exists():
        source = _detect_source("", report_path, data_dir)
        return MigrationResult(path=path, status="missing", source=source)

    content = report_path.read_text(encoding="utf-8")
    fmt = report_format(content)
    source = _detect_source(content, report_path, data_dir)
    if fmt in {CURRENT_ARXIV_FORMAT, CURRENT_JOURNAL_FORMAT}:
        if validate_report_content(content, source=source):
            return MigrationResult(
                path=path,
                status="current",
                source=source,
                from_format=fmt,
                to_format=fmt,
            )
        return MigrationResult(
            path=path,
            status="corrupt",
            source=source,
            from_format=fmt,
            to_format=fmt,
            message="current report failed format validation",
        )
    if not source:
        return MigrationResult(path=path, status="unsupported", message="not a PaperTrack report")
    if _legacy_skeleton_ok(content, source):
        return MigrationResult(
            path=path,
            status="migratable",
            source=source,
            from_format="legacy",
            to_format=CURRENT_JOURNAL_FORMAT if source == "journal" else CURRENT_ARXIV_FORMAT,
        )
    return MigrationResult(
        path=path,
        status="corrupt",
        source=source,
        from_format="legacy",
        message="missing generated report skeleton",
    )


def report_format(content: str) -> str:
    match = re.search(r"^papertrack_format:\s*([^\n]+)\s*$", content, re.MULTILINE)
    if not match:
        return "legacy"
    return match.group(1).strip().strip("\"'")


def validate_report_content(content: str, source: str = "") -> bool:
    if not _validate_report_structure(content, source):
        return False
    if source == "journal":
        return not has_journal_quality_failure(content)
    return True


def _validate_report_structure(content: str, source: str = "") -> bool:
    if "## collected" not in content or "## not collected" not in content:
        return False
    if "```dataview" not in content:
        return False
    fmt = report_format(content)
    if source == "journal":
        return (
            fmt == CURRENT_JOURNAL_FORMAT
            and bool(re.search(r"^###\s*10\.\S+", content, re.MULTILINE))
            and _generated_tag_layout_is_valid(content, source)
        )
    if source == "arxiv":
        return (
            fmt == CURRENT_ARXIV_FORMAT
            and bool(re.search(r"^###\s*arXiv:\S+", content, re.MULTILINE))
            and _generated_tag_layout_is_valid(content, source)
        )
    return fmt in {CURRENT_ARXIV_FORMAT, CURRENT_JOURNAL_FORMAT} and "### " in content


def _generated_tag_layout_is_valid(content: str, source: str) -> bool:
    frontmatter_end = content.find("\n---", 4) if content.startswith("---\n") else -1
    if frontmatter_end < 0:
        return False
    frontmatter_end += 4
    frontmatter = content[:frontmatter_end]
    body = content[frontmatter_end:]

    if source == "arxiv":
        categories = _frontmatter_values(frontmatter, "papertrack_category")
        report_dates = _frontmatter_values(frontmatter, "papertrack_date")
        if len(categories) != 1 or len(report_dates) != 1:
            return False
        category = categories[0]
        report_date = report_dates[0]
        expected_tag = arxiv_report_tag(category, report_date)
        candidate_sources = {f"#{category}-{report_date}", f"#{expected_tag}"}
    elif source == "journal":
        journal_slugs = _frontmatter_values(frontmatter, "papertrack_journal")
        volumes = _frontmatter_values(frontmatter, "papertrack_volume")
        issues = _frontmatter_values(frontmatter, "papertrack_issue")
        if len(journal_slugs) != 1 or len(volumes) != 1 or len(issues) != 1:
            return False
        journal_slug = journal_slugs[0]
        volume = volumes[0]
        issue = issues[0]
        expected_tag = journal_report_tag(journal_slug, volume, issue)
        candidate_sources = {f"#{expected_tag}"}
    else:
        return True

    try:
        tag_start, tag_end = _frontmatter_list_property_span(frontmatter, "tags")
    except ValueError:
        return False
    tag_block = frontmatter[tag_start:tag_end]
    yaml_matches = re.findall(
        rf"^  -[ \t]+{re.escape(expected_tag)}[ \t]*$",
        tag_block,
        re.MULTILINE,
    )
    if len(yaml_matches) != 1:
        return False

    generated_sources = []
    for block_match in re.finditer(r"```dataview\n.*?\n```", body, re.DOTALL):
        block = block_match.group(0)
        if not re.search(r"^TASK(?:[ \t]+FROM\b|[ \t]*$)", block, re.MULTILINE):
            continue
        for match in re.finditer(
            r"^(?:from|FROM|TASK FROM)[ \t]+(#\S+)[ \t]*$",
            block,
            re.MULTILINE,
        ):
            if match.group(1) in candidate_sources:
                generated_sources.append(match.group(1))
    return not generated_sources or generated_sources == [f"#{expected_tag}"]


def _frontmatter_value(frontmatter: str, field: str) -> str:
    values = _frontmatter_values(frontmatter, field)
    return values[0] if values else ""


def _frontmatter_values(frontmatter: str, field: str) -> list[str]:
    matches = re.finditer(
        rf'^{re.escape(field)}:[ \t]*["\']?([^\n"\']+)["\']?[ \t]*$',
        frontmatter,
        re.MULTILINE,
    )
    return [match.group(1).strip() for match in matches]


def _failed_current_validation(
    report_path: Path,
    source: str,
    current_format: str,
) -> MigrationResult:
    return MigrationResult(
        path=str(report_path),
        status="failed",
        source=source,
        from_format=current_format,
        to_format=current_format,
        message="current report failed format validation",
    )


def has_journal_quality_failure(content: str) -> bool:
    """Detect generated journal reports polluted by known bad metadata sources."""
    case_insensitive_patterns = (
        r"^###\s*10\.1021/(?:civ|ctv)\d+i\d+[_\w-]*\s*$",
        r"^Title:\s+Issue\s+(?:Editorial\s+Masthead|Publication\s+Information)\s*$",
        r"<(?:mml:)?math\b",
    )
    if any(
        re.search(pattern, content, re.IGNORECASE | re.MULTILINE)
        for pattern in case_insensitive_patterns
    ):
        return True
    return bool(
        re.search(
            r"^###\s*10\.1103/physrevlett\.",
            content,
            re.MULTILINE,
        )
    )


def validate_report_file(path: str, source: str = "") -> bool:
    report_path = Path(path)
    if not report_path.exists():
        return False
    return validate_report_content(report_path.read_text(encoding="utf-8"), source=source)


def _migrate_journal_legacy(content: str, path: Path, data_dir: str) -> str:
    metadata = _journal_metadata(content, path, data_dir)
    legacy_frontmatter = _legacy_frontmatter_content(content)
    body = _remove_legacy_frontmatter(content)
    body = _remove_markdown_images(body)
    tag = journal_report_tag(
        metadata["journal_slug"],
        metadata["volume"],
        metadata["issue"],
    )
    body = _repair_generated_dataview_query(body, {f"#{tag}"}, f"#{tag}")
    generated_fields = [
        f"papertrack_format: {CURRENT_JOURNAL_FORMAT}",
        "papertrack_source: journal",
        f"papertrack_provider: {metadata['provider']}",
        f"papertrack_journal: {metadata['journal_slug']}",
        f"papertrack_volume: \"{metadata['volume']}\"",
        f"papertrack_issue: \"{metadata['issue']}\"",
    ]
    frontmatter = _merge_legacy_frontmatter(
        legacy_frontmatter,
        generated_fields,
        {tag},
        tag,
    )
    return frontmatter + body.lstrip()


def _migrate_arxiv_legacy(content: str, path: Path, data_dir: str) -> str:
    metadata = _arxiv_metadata(content, path, data_dir)
    legacy_frontmatter = _legacy_frontmatter_content(content)
    body = _remove_legacy_frontmatter(content)
    tag = arxiv_report_tag(metadata["category"], metadata["date"])
    legacy_source = f"#{metadata['category']}-{metadata['date']}"
    body = _repair_generated_dataview_query(
        body,
        {legacy_source, f"#{tag}"},
        f"#{tag}",
    )
    generated_fields = [
        f"papertrack_format: {CURRENT_ARXIV_FORMAT}",
        "papertrack_source: arxiv",
        f"papertrack_category: {metadata['category']}",
        f"papertrack_date: \"{metadata['date']}\"",
    ]
    frontmatter = _merge_legacy_frontmatter(
        legacy_frontmatter,
        generated_fields,
        {metadata["category"] + "-" + metadata["date"], tag},
        tag,
    )
    return frontmatter + body.lstrip()


def _repair_current_arxiv_identity(content: str, path: Path, data_dir: str) -> str:
    """Make generated arXiv identity fields agree with the stable file path."""
    parts = _relative_parts(path, data_dir)
    if len(parts) < 5 or parts[0] != "arxiv":
        return content

    category = parts[1]
    report_date = f"{parts[2]}-{parts[3]}-{path.stem}"
    frontmatter_end = content.find("\n---", 4) if content.startswith("---\n") else -1
    if frontmatter_end < 0:
        return content
    frontmatter_end += 4
    frontmatter = content[:frontmatter_end]
    body = content[frontmatter_end:]

    category_match = re.search(
        r"^papertrack_category:[ \t]*(\S+)[ \t]*$",
        frontmatter,
        re.MULTILINE,
    )
    date_match = re.search(
        r'^papertrack_date:[ \t]*["\']?(\d{4}-\d{2}-\d{2})["\']?[ \t]*$',
        frontmatter,
        re.MULTILINE,
    )
    if category_match is None or date_match is None:
        return content
    old_category = category_match.group(1)
    old_date = date_match.group(1)
    canonical_tag = arxiv_report_tag(category, report_date)
    candidate_tags = {
        f"{old_category}-{old_date}",
        f"{category}-{report_date}",
        canonical_tag,
    }

    frontmatter = re.sub(
        r"^papertrack_category:[ \t]*\S+[ \t]*$",
        f"papertrack_category: {category}",
        frontmatter,
        count=1,
        flags=re.MULTILINE,
    )
    frontmatter = re.sub(
        r'^papertrack_date:[ \t]*["\']?\d{4}-\d{2}-\d{2}["\']?[ \t]*$',
        f'papertrack_date: "{report_date}"',
        frontmatter,
        count=1,
        flags=re.MULTILINE,
    )
    frontmatter = _repair_generated_tag_list(
        frontmatter,
        candidate_tags,
        canonical_tag,
    )
    body = _repair_generated_dataview_query(
        body,
        {f"#{tag}" for tag in candidate_tags},
        f"#{canonical_tag}",
    )
    return frontmatter + body


def _repair_generated_tag_list(
    frontmatter: str,
    candidate_tags: set[str],
    canonical_tag: str,
) -> str:
    block_start, block_end = _frontmatter_list_property_span(frontmatter, "tags")
    block = frontmatter[block_start:block_end]
    matches = []
    for match in re.finditer(r"^  -[ \t]+(#?\S+)[ \t]*$", block, re.MULTILINE):
        value = match.group(1).removeprefix("#")
        if value in candidate_tags:
            matches.append(match)
    if not matches:
        raise ValueError("current report is missing a recognized generated tag identity")
    if len(matches) != 1:
        raise ValueError("current report has ambiguous generated tag identity")
    match = matches[0]
    absolute_start = block_start + match.start()
    absolute_end = block_start + match.end()
    return (
        frontmatter[:absolute_start]
        + f"  - {canonical_tag}"
        + frontmatter[absolute_end:]
    )


def _frontmatter_list_property_span(frontmatter: str, field: str) -> tuple[int, int]:
    matches = list(
        re.finditer(
            rf"^{re.escape(field)}:[^\n]*$",
            frontmatter,
            re.MULTILINE,
        )
    )
    if len(matches) != 1:
        raise ValueError(f"current report must contain exactly one {field} list")
    if matches[0].group(0).strip() != f"{field}:":
        raise ValueError(f"current report {field} must use block-list syntax")
    start = matches[0].end()
    end_match = re.search(
        r"^(?:[A-Za-z_][A-Za-z0-9_-]*:|---[ \t]*$)",
        frontmatter[start:],
        re.MULTILINE,
    )
    end = start + end_match.start() if end_match else len(frontmatter)
    return start, end


def _repair_generated_dataview_query(
    body: str,
    candidate_sources: set[str],
    canonical_source: str,
) -> str:
    block_matches = [
        match
        for match in re.finditer(r"```dataview\n.*?\n```", body, re.DOTALL)
        if re.search(r"^TASK(?:[ \t]+FROM\b|[ \t]*$)", match.group(0), re.MULTILINE)
    ]
    candidates = []
    for block_match in block_matches:
        for match in re.finditer(
            r"^(from|FROM|TASK FROM)[ \t]+(#\S+)[ \t]*$",
            block_match.group(0),
            re.MULTILINE,
        ):
            if match.group(2) in candidate_sources:
                candidates.append((block_match, match))
    if not candidates:
        return body
    if len(candidates) != 1:
        raise ValueError("current report has ambiguous generated Dataview identity")
    block_match, match = candidates[0]
    block = block_match.group(0)
    repaired_block = (
        block[:match.start()]
        + f"{match.group(1)} {canonical_source}"
        + block[match.end():]
    )
    return body[:block_match.start()] + repaired_block + body[block_match.end():]


def _repair_current_journal_tags(content: str) -> str:
    frontmatter_end = content.find("\n---", 4) if content.startswith("---\n") else -1
    if frontmatter_end < 0:
        return content
    frontmatter_end += 4
    frontmatter = content[:frontmatter_end]
    body = content[frontmatter_end:]
    values = {}
    for field in ("papertrack_journal", "papertrack_volume", "papertrack_issue"):
        match = re.search(
            rf'^\s*{field}:[ \t]*["\']?([^\n"\']+)["\']?[ \t]*$',
            frontmatter,
            re.MULTILINE,
        )
        if match is None:
            return content
        values[field] = match.group(1).strip()
    tag = journal_report_tag(
        values["papertrack_journal"],
        values["papertrack_volume"],
        values["papertrack_issue"],
    )
    frontmatter = _repair_generated_tag_list(
        frontmatter,
        {tag},
        tag,
    )
    body = _repair_generated_dataview_query(body, {f"#{tag}"}, f"#{tag}")
    return frontmatter + body


def _journal_metadata(content: str, path: Path, data_dir: str) -> dict[str, str]:
    title_match = re.search(
        r"^#\s*(.*?)\s+Vol\.\s*([^,]+),\s*Iss\.\s*([^\s]+)\s*(?:\u2014|-)\s*(.*)$",
        content,
        re.MULTILINE,
    )
    provider, slug, volume, issue = _path_metadata(path, data_dir)
    if not (provider and slug and volume and issue):
        candidates = set()
        for source in _legacy_generated_identity_sources(content):
            match = re.fullmatch(r"#(.+)-v([^-\s]+)-i([^-\s]+)", source)
            if match:
                candidates.add(match.groups())
        if title_match:
            title_identity = (
                title_match.group(2).strip(),
                title_match.group(3).strip(),
            )
            candidates = {
                candidate
                for candidate in candidates
                if candidate[1:] == title_identity
            }
        if len(candidates) > 1:
            raise ValueError("legacy journal report has ambiguous generated identity")
        if len(candidates) == 1:
            slug, volume, issue = next(iter(candidates))
    if title_match:
        volume = volume or title_match.group(2).strip()
        issue = issue or title_match.group(3).strip()
    return {
        "provider": provider or "unknown",
        "journal_slug": slug or "unknown",
        "volume": volume or "unknown",
        "issue": issue or path.stem,
    }


def _arxiv_metadata(content: str, path: Path, data_dir: str) -> dict[str, str]:
    parts = _relative_parts(path, data_dir)
    if len(parts) >= 5 and parts[0] == "arxiv":
        return {
            "category": parts[1],
            "date": f"{parts[2]}-{parts[3]}-{path.stem}",
        }

    title_match = re.search(r"^#\s*(\d{4}-\d{2}-\d{2})\b", content, re.MULTILINE)
    candidates = set()
    for source in _legacy_generated_identity_sources(content):
        match = re.fullmatch(r"#(.+)-(\d{4}-\d{2}-\d{2})", source)
        if match:
            candidates.add(match.groups())
    if title_match:
        candidates = {
            candidate for candidate in candidates if candidate[1] == title_match.group(1)
        }
    if len(candidates) > 1:
        raise ValueError("legacy arXiv report has ambiguous generated identity")
    if len(candidates) == 1:
        category, report_date = next(iter(candidates))
    else:
        category = ""
        report_date = title_match.group(1) if title_match else ""
    return {
        "category": category or "unknown",
        "date": report_date or "unknown",
    }


def _path_metadata(path: Path, data_dir: str) -> tuple[str, str, str, str]:
    parts = _relative_parts(path, data_dir)
    if len(parts) == 4:
        return parts[0], parts[1], parts[2], path.stem
    return "", "", "", ""


def _legacy_generated_identity_sources(content: str) -> set[str]:
    sources: set[str] = set()
    frontmatter = _legacy_frontmatter_content(content)
    tag_headers = list(re.finditer(r"^tags:[^\n]*$", frontmatter, re.MULTILINE))
    if len(tag_headers) > 1:
        raise ValueError("legacy report has ambiguous tags properties")
    if tag_headers:
        header = tag_headers[0]
        if header.group(0).strip() != "tags:":
            raise ValueError("legacy report tags must use block-list syntax")
        block_start = header.end()
        end_match = re.search(
            r"^[A-Za-z_][A-Za-z0-9_-]*:",
            frontmatter[block_start:],
            re.MULTILINE,
        )
        block_end = block_start + end_match.start() if end_match else len(frontmatter)
        for match in re.finditer(
            r"^  -[ \t]+(#\S+)[ \t]*$",
            frontmatter[block_start:block_end],
            re.MULTILINE,
        ):
            sources.add(match.group(1))

    body = _remove_legacy_frontmatter(content)
    for block_match in re.finditer(r"```dataview\n.*?\n```", body, re.DOTALL):
        block = block_match.group(0)
        if not re.search(r"^TASK(?:[ \t]+FROM\b|[ \t]*$)", block, re.MULTILINE):
            continue
        for match in re.finditer(
            r"^(?:from|FROM|TASK FROM)[ \t]+(#\S+)[ \t]*$",
            block,
            re.MULTILINE,
        ):
            sources.add(match.group(1))
    return sources


def _relative_parts(path: Path, data_dir: str) -> tuple[str, ...]:
    if data_dir:
        try:
            return path.relative_to(data_dir).parts
        except ValueError:
            pass
    return path.parts


def _remove_legacy_frontmatter(content: str) -> str:
    if content.startswith("---\n"):
        end = content.find("\n---", 4)
        if end != -1:
            return content[end + 4:].lstrip("\n")

    lines = content.splitlines()
    if not lines:
        return content
    idx = 1 if lines[0].startswith("# ") else 0
    while idx < len(lines) and not lines[idx].strip():
        idx += 1
    if idx >= len(lines) or lines[idx].strip() != "---":
        return content
    end = idx + 1
    while end < len(lines) and lines[end].strip() != "---":
        end += 1
    if end >= len(lines):
        return content
    new_lines = lines[:idx] + lines[end + 1:]
    return "\n".join(new_lines).rstrip() + "\n"


def _legacy_frontmatter_content(content: str) -> str:
    lines = content.splitlines()
    if not lines:
        return ""
    idx = 0
    if lines[0].startswith("# "):
        idx = 1
        while idx < len(lines) and not lines[idx].strip():
            idx += 1
    if idx >= len(lines) or lines[idx].strip() != "---":
        return ""
    end = idx + 1
    while end < len(lines) and lines[end].strip() != "---":
        end += 1
    if end >= len(lines):
        return ""
    return "\n".join(lines[idx + 1:end])


def _merge_legacy_frontmatter(
    legacy_frontmatter: str,
    generated_fields: list[str],
    candidate_tags: set[str],
    canonical_tag: str,
) -> str:
    preserved_tags: list[str] = []
    remaining = legacy_frontmatter
    tag_headers = list(re.finditer(r"^tags:[^\n]*$", remaining, re.MULTILINE))
    if len(tag_headers) > 1:
        raise ValueError("legacy report has ambiguous tags properties")
    if tag_headers:
        header = tag_headers[0]
        if header.group(0).strip() != "tags:":
            raise ValueError("legacy report tags must use block-list syntax")
        block_start = header.end()
        end_match = re.search(
            r"^[A-Za-z_][A-Za-z0-9_-]*:",
            remaining[block_start:],
            re.MULTILINE,
        )
        block_end = block_start + end_match.start() if end_match else len(remaining)
        generated_count = 0
        for line in remaining[block_start:block_end].splitlines():
            item = re.match(r"^  -[ \t]+(#?\S+)[ \t]*$", line)
            if item and item.group(1).removeprefix("#") in candidate_tags:
                generated_count += 1
                continue
            if line.strip():
                preserved_tags.append(line)
        if generated_count > 1:
            raise ValueError("legacy report has ambiguous generated tag identity")
        remaining = remaining[:header.start()] + remaining[block_end:]

    generated_keys = {field.split(":", 1)[0] for field in generated_fields}
    preserved_properties = []
    lines = remaining.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        property_match = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*):", line)
        if property_match is None:
            if line.strip():
                preserved_properties.append(line)
            index += 1
            continue
        end = index + 1
        while end < len(lines) and not re.match(
            r"^[A-Za-z_][A-Za-z0-9_-]*:", lines[end]
        ):
            end += 1
        if property_match.group(1) not in generated_keys:
            preserved_properties.extend(lines[index:end])
        index = end

    frontmatter = ["---", *generated_fields, "tags:"]
    frontmatter.extend(preserved_tags)
    frontmatter.append(f"  - {canonical_tag}")
    frontmatter.extend(preserved_properties)
    frontmatter.extend(["---", ""])
    return "\n".join(frontmatter)


def _has_markdown_images(content: str) -> bool:
    return bool(re.search(_GENERATED_TOC_IMAGE_PATTERN, content))


def _remove_markdown_images(content: str) -> str:
    return re.sub(_GENERATED_TOC_IMAGE_PATTERN + r"\n?", "", content)


def _write_migrated_content(
    report_path: Path,
    old_content: str,
    new_content: str,
    source: str,
    from_format: str,
    to_format: str,
    status: str,
    allow_preexisting_quality_failure: bool = False,
) -> MigrationResult:
    try:
        valid = validate_report_content(new_content, source=source)
        if (
            not valid
            and allow_preexisting_quality_failure
            and source == "journal"
            and has_journal_quality_failure(old_content)
            and _validate_report_structure(new_content, source=source)
        ):
            valid = True
        if not valid:
            raise ValueError("migrated report failed format validation")
        _atomic_write_report(report_path, new_content)
    except Exception as exc:
        record_migration_event({
            "path": str(report_path),
            "source": source,
            "from_format": from_format,
            "to_format": to_format,
            "outcome": "failed",
            "exception": repr(exc),
        })
        return MigrationResult(
            path=str(report_path),
            status="failed",
            source=source,
            from_format=from_format,
            to_format=to_format,
            message=str(exc),
        )

    if record_all_attempts():
        record_migration_event({
            "path": str(report_path),
            "source": source,
            "from_format": from_format,
            "to_format": to_format,
            "outcome": status,
        })

    return MigrationResult(
        path=str(report_path),
        status=status,
        source=source,
        from_format=from_format,
        to_format=to_format,
    )


def _detect_source(content: str, path: Path, data_dir: str) -> str:
    fmt = report_format(content)
    if fmt == CURRENT_ARXIV_FORMAT:
        return "arxiv"
    if fmt == CURRENT_JOURNAL_FORMAT:
        return "journal"
    if re.search(r"^###\s*arXiv:\S+", content, re.MULTILINE):
        return "arxiv"
    if re.search(r"^###\s*10\.\S+", content, re.MULTILINE):
        return "journal"
    parts = _relative_parts(path, data_dir)
    if parts and parts[0] == "arxiv":
        return "arxiv"
    if len(parts) >= 4 and parts[-1].endswith(".md"):
        return "journal"
    return ""


def _legacy_skeleton_ok(content: str, source: str) -> bool:
    if "## collected" not in content or "## not collected" not in content:
        return False
    if source == "journal":
        return bool(re.search(r"^###\s*10\.\S+", content, re.MULTILINE))
    if source == "arxiv":
        return bool(re.search(r"^###\s*arXiv:\S+", content, re.MULTILINE))
    return False


def _extract_task_states(content: str) -> dict[tuple[str, str], _TaskState]:
    states: dict[tuple[str, str], _TaskState] = {}
    for line in content.splitlines():
        parsed = _parse_task_line(line)
        if parsed is None:
            continue
        identifier, style, state = parsed
        states[(identifier, style)] = state
    return states


def _apply_task_state_to_line(
    line: str, states: dict[tuple[str, str], _TaskState]
) -> str:
    parsed = _parse_task_line(line)
    if parsed is None:
        return line
    identifier, style, _ = parsed
    state = states.get((identifier, style))
    completed_state = next(
        (
            candidate
            for (candidate_id, _), candidate in states.items()
            if candidate_id == identifier and candidate.box == "x"
        ),
        None,
    )
    if completed_state is not None and (state is None or state.box != "x"):
        state = completed_state
    if state is None:
        return line

    box_match = re.match(r"^(\s*-\s*)\[[ xX]\](.*)$", line)
    if box_match is None:
        return line
    link_match = _task_link_match(box_match.group(2))
    if link_match is None:
        return line
    prefix = box_match.group(1)
    rest = box_match.group(2)
    return f"{prefix}[{state.box}]{rest[:link_match.end()]}{state.suffix}"


def _parse_task_line(line: str) -> tuple[str, str, _TaskState] | None:
    box_match = re.match(r"^\s*-\s*\[([ xX])\](.*)$", line)
    if box_match is None:
        return None
    rest = box_match.group(2)
    link_match = _task_link_match(rest)
    if link_match is None:
        return None
    style = link_match.group("style")
    identifier = link_match.group("id").strip()
    box = "x" if box_match.group(1).lower() == "x" else " "
    suffix = rest[link_match.end():]
    return identifier, style, _TaskState(box=box, suffix=suffix)


def _task_link_match(rest: str) -> re.Match[str] | None:
    wiki = re.search(r"(?P<style>\[\[)#(?P<id>[^\]]+)\]\]", rest)
    if wiki:
        return wiki
    markdown = re.search(r"(?P<style>\[)(?P<id>[^\]]+)\]\([^)]+\)", rest)
    if markdown:
        identifier = markdown.group("id").strip()
        if identifier.startswith("arXiv:") or identifier.startswith("10."):
            return markdown
    return None


def _backup_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".bak")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

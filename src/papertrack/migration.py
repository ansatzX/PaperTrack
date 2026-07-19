"""Markdown report schema migration and safe report writes."""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .diagnostics import record_all_attempts, record_migration_event

CURRENT_ARXIV_FORMAT = "arxiv-v2"
CURRENT_JOURNAL_FORMAT = "journal-v2"


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

    report_path = Path(path)
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

    old_content = report_path.read_text(encoding="utf-8")
    current_format = report_format(old_content)
    source = _detect_source(old_content, report_path, data_dir)

    if current_format in {CURRENT_ARXIV_FORMAT, CURRENT_JOURNAL_FORMAT}:
        if _has_markdown_images(old_content):
            new_content = _remove_markdown_images(old_content)
            return _write_migrated_content(
                report_path=report_path,
                old_content=old_content,
                new_content=new_content,
                source=source,
                from_format=current_format,
                to_format=current_format,
                status="cleaned",
            )
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

    if source == "journal":
        new_content = _migrate_journal_legacy(old_content, report_path, data_dir)
        to_format = CURRENT_JOURNAL_FORMAT
    else:
        new_content = _migrate_arxiv_legacy(old_content, report_path, data_dir)
        to_format = CURRENT_ARXIV_FORMAT

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
    if "## collected" not in content or "## not collected" not in content:
        return False
    if "```dataview" not in content:
        return False
    fmt = report_format(content)
    if source == "journal":
        return (
            fmt == CURRENT_JOURNAL_FORMAT
            and bool(re.search(r"^###\s*10\.\S+", content, re.MULTILINE))
            and not has_journal_quality_failure(content)
        )
    if source == "arxiv":
        return fmt == CURRENT_ARXIV_FORMAT and bool(re.search(r"^###\s*arXiv:\S+", content, re.MULTILINE))
    return fmt in {CURRENT_ARXIV_FORMAT, CURRENT_JOURNAL_FORMAT} and "### " in content


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
    body = _remove_legacy_frontmatter(content)
    body = _remove_markdown_images(body)
    frontmatter = [
        "---",
        f"papertrack_format: {CURRENT_JOURNAL_FORMAT}",
        "papertrack_source: journal",
        f"papertrack_provider: {metadata['provider']}",
        f"papertrack_journal: {metadata['journal_slug']}",
        f"papertrack_volume: \"{metadata['volume']}\"",
        f"papertrack_issue: \"{metadata['issue']}\"",
        "tags:",
        f"  - #{metadata['journal_slug']}-v{metadata['volume']}-i{metadata['issue']}",
        "---",
        "",
    ]
    return "\n".join(frontmatter) + body.lstrip()


def _migrate_arxiv_legacy(content: str, path: Path, data_dir: str) -> str:
    metadata = _arxiv_metadata(content, path, data_dir)
    body = _remove_legacy_frontmatter(content)
    frontmatter = [
        "---",
        f"papertrack_format: {CURRENT_ARXIV_FORMAT}",
        "papertrack_source: arxiv",
        f"papertrack_category: {metadata['category']}",
        f"papertrack_date: \"{metadata['date']}\"",
        "tags:",
        f"  - #{metadata['category']}-{metadata['date']}",
        "---",
        "",
    ]
    return "\n".join(frontmatter) + body.lstrip()


def _journal_metadata(content: str, path: Path, data_dir: str) -> dict[str, str]:
    tag_match = re.search(r"#(.+)-v([^-\s]+)-i([^-\s]+)", content)
    title_match = re.search(
        r"^#\s*(.*?)\s+Vol\.\s*([^,]+),\s*Iss\.\s*([^\s]+)\s*(?:\u2014|-)\s*(.*)$",
        content,
        re.MULTILINE,
    )
    provider, slug, volume, issue = _path_metadata(path, data_dir)
    if tag_match:
        slug = tag_match.group(1)
        volume = tag_match.group(2)
        issue = tag_match.group(3)
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
    tag_match = re.search(r"#(.+)-(\d{4}-\d{2}-\d{2})", content)
    title_match = re.search(r"^#\s*(\d{4}-\d{2}-\d{2})\b", content, re.MULTILINE)
    category = ""
    report_date = ""
    if tag_match:
        category = tag_match.group(1)
        report_date = tag_match.group(2)
    if title_match:
        report_date = report_date or title_match.group(1)
    parts = _relative_parts(path, data_dir)
    if len(parts) >= 5 and parts[0] == "arxiv":
        category = category or parts[1]
        report_date = report_date or f"{parts[2]}-{parts[3]}-{path.stem}"
    return {
        "category": category or "unknown",
        "date": report_date or "unknown",
    }


def _path_metadata(path: Path, data_dir: str) -> tuple[str, str, str, str]:
    parts = _relative_parts(path, data_dir)
    if len(parts) >= 4:
        return parts[-4], parts[-3], parts[-2], path.stem
    return "", "", "", ""


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


def _has_markdown_images(content: str) -> bool:
    return bool(re.search(r"(?m)^\s*!\[[^\]\n]*\]\([^\n]*\)\s*$", content))


def _remove_markdown_images(content: str) -> str:
    return re.sub(r"(?m)^\s*!\[[^\]\n]*\]\([^\n]*\)\s*\n?", "", content)


def _write_migrated_content(
    report_path: Path,
    old_content: str,
    new_content: str,
    source: str,
    from_format: str,
    to_format: str,
    status: str,
) -> MigrationResult:
    backup_path = _backup_path(report_path)
    shutil.copy2(report_path, backup_path)
    try:
        report_path.write_text(new_content, encoding="utf-8")
        written = report_path.read_text(encoding="utf-8")
        if not validate_report_content(written, source=source):
            raise ValueError("migrated report failed format validation")
    except Exception as exc:
        report_path.write_text(old_content, encoding="utf-8")
        if backup_path.exists():
            backup_path.unlink()
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

    if backup_path.exists():
        backup_path.unlink()

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

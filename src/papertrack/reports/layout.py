"""Local migration to one durable volume/issue.md report per journal issue."""
from __future__ import annotations

import os
from pathlib import Path
import re

from papertrack.reports.markdown import repair_presentation
from papertrack.reports.migration import MigrationResult, _atomic_write_report, preserve_task_state


def _comparison(content: str) -> str:
    # Only this historical generated ACS image form is obsolete. Personal
    # figures, notes, metadata, anchors and article text remain significant.
    content = re.sub(
        r'(?m)^!\[\|700\]\(https://pubs\.acs\.org/cms/10\.[^\s)]+/asset/images/[^)]+\)[ \t]*$',
        '', content)
    content = repair_presentation(content)
    lines = []
    for line in content.splitlines():
        if re.match(r'^\s*-\s*\[[xX]\]', line):
            line = re.sub(r' ✅ \d{4}-\d{2}-\d{2}[ \t]*$', '', line)
            line = re.sub(r'^(\s*-\s*)\[[xX]\]', r'\1[ ]', line)
        lines.append(line)
    return re.sub(r'\s+', ' ', '\n'.join(lines)).strip()


def _merge_completion(canonical: str, legacy: str) -> str:
    # Completed tasks from either copy survive. An already completed canonical
    # task keeps its own timestamp and notes; no paper field is regenerated.
    done = '\n'.join(line for line in legacy.splitlines()
                     if re.match(r'^\s*-\s*\[[xX]\]', line))
    return ''.join(line if re.match(r'^\s*-\s*\[[xX]\]', line)
                   else preserve_task_state(line, done)
                   for line in canonical.splitlines(keepends=True))


def _backup(path: Path, original: bytes, root: Path, backups: Path) -> Path:
    destination = backups / path.relative_to(root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if destination.read_bytes() != original:
            raise ValueError(f'Backup already contains different data: {destination}')
    else:
        with destination.open('xb') as stream:
            stream.write(original)
            stream.flush()
            os.fsync(stream.fileno())
    return destination


def migrate_journal_layout(data_dir: str, backup_dir: str) -> list[MigrationResult]:
    """Back up and retire issue/report.md; never fetch or choose between notes.

    A duplicate may differ only in generated presentation or task completion.
    Other differences fail visibly with both originals untouched. A lone legacy
    report moves byte-for-byte. Only empty old issue directories are removed.
    """
    if not backup_dir:
        raise ValueError('Journal layout migration requires --backup-dir')
    root, backups = Path(data_dir).resolve(), Path(backup_dir).resolve()
    if backups == root or root in backups.parents:
        raise ValueError('Journal layout backups must be outside the report data directory')
    results = []
    for provider in ('acs', 'aip', 'aps'):
        for legacy in sorted((root / provider).glob('*/*/*/report.md')):
            if not legacy.parent.name.isdecimal() or not legacy.parent.parent.name.isdecimal():
                continue
            canonical = legacy.parent.with_suffix('.md')
            try:
                if legacy.resolve() != legacy or canonical.resolve() != canonical:
                    raise ValueError('Journal layout migration refuses symbolic links')
                old_bytes = legacy.read_bytes()
                current = canonical.read_bytes() if canonical.exists() else None
                if current is not None:
                    old_text, current_text = old_bytes.decode('utf-8'), current.decode('utf-8')
                    if _comparison(old_text) != _comparison(current_text):
                        raise ValueError(f'Conflicting report text or anchors; both preserved: {canonical}')
                    merged = _merge_completion(current_text, old_text).encode('utf-8')
                else:
                    merged = old_bytes
                backup = _backup(legacy, old_bytes, root, backups)
                if current is not None:
                    _backup(canonical, current, root, backups)
                if legacy.read_bytes() != old_bytes or (canonical.read_bytes() if canonical.exists() else None) != current:
                    raise ValueError('Report changed during migration; no replacement made')
                if current is None:
                    # Same filesystem: rename retains the exact bytes and mode.
                    legacy.rename(canonical)
                else:
                    if merged != current:
                        _atomic_write_report(canonical, merged.decode('utf-8'))
                    legacy.unlink()
                if not any(legacy.parent.iterdir()):
                    legacy.parent.rmdir()
                results.append(MigrationResult(path=str(canonical), status='migrated', source='journal',
                                               backup_path=str(backup), message=f'Retired {legacy}'))
            except (OSError, UnicodeError, ValueError) as error:
                results.append(MigrationResult(path=str(legacy), status='failed', source='journal', message=str(error)))
    return results

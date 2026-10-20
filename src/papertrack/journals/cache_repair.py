"""Offline, evidence-bound upgrade of historical p-only Extract captures.

Never fetches, writes Markdown, or changes the original run. This module is
intentionally independent of the live refresh path.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile

from bs4 import BeautifulSoup

from papertrack.journals.capture import (
    _reading_panel, _silverchair_reading_nodes, read_browser_issue,
    validate_reading_controls,
)
from papertrack.journals.metadata import normalize_doi


def _extract_text(nodes) -> str:
    # Match the fixed collector's legacy sup/sub notation without mutating DOM.
    nodes = deepcopy(nodes)
    for paragraph in nodes:
        for node in paragraph.select('sup, sub'):
            node.replace_with(('^' if node.name == 'sup' else '_') + '{' + node.get_text() + '}')
    return '\n\n'.join(node.get_text() for node in nodes).strip()


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _evidence_files(capture: Path) -> list[Path]:
    files = []
    for path in sorted(capture.parent.iterdir()):
        if path.name != capture.name and not path.name.startswith(capture.name + '.'):
            continue
        if path.is_symlink():
            raise ValueError(f'Symlink evidence is unsupported: {path}')
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            for child in sorted(path.rglob('*')):
                if child.is_symlink():
                    raise ValueError(f'Symlink evidence is unsupported: {child}')
                if child.is_file():
                    files.append(child)
    return files


def repair_cached_extract_capture(
    source_capture: str | Path, output_dir: str | Path, *, provider: str,
    code: str, volume: str, issue: str, journal_name: str, dry_run: bool = False,
) -> dict:
    """Publish a validated copy, or dry-run without publishing any capture.

    output_dir must not exist. Original evidence bytes and row provenance are
    retained. Only proven p-only Extract omissions are eligible; other conflicts
    and unsupported mathematics fail the operation before publication.
    """
    source = Path(source_capture).resolve(strict=True)
    destination = Path(output_dir).absolute()
    if provider not in ('acs', 'aip'):
        raise ValueError('Only Silverchair Extract captures can be upgraded')
    if destination.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    files = _evidence_files(source)
    hashes = {str(p.relative_to(source.parent)): _hash(p) for p in files}
    staging = Path(tempfile.mkdtemp(prefix='.extract-repair-', dir=destination.parent))
    try:
        for file in files:
            target = staging / file.relative_to(source.parent)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file, target)
            if _hash(target) != hashes[str(file.relative_to(source.parent))]:
                raise ValueError(f'Evidence changed while copying: {file}')
        capture = staging / source.name
        data = json.loads(capture.read_text(encoding='utf-8'))
        html_path = Path(str(capture) + '.html')
        soup = BeautifulSoup(html_path.read_text(encoding='utf-8'), 'lxml')
        changed = []
        for row in data.get('entries', []):
            if row.get('abstract_type') != 'extract':
                continue
            panel = _reading_panel(row, data, html_path, soup)
            if panel is None:
                raise ValueError(f'Extract source panel missing: {row.get("doi")}')
            nodes, _ = _silverchair_reading_nodes(panel, 'extract')
            full = _extract_text(nodes)
            raw = row.get('abstract', '')
            compact = lambda value: ' '.join(value.split())
            if compact(raw) == compact(full):
                continue
            old = _extract_text(panel.select('p'))
            extra_blocks = [n for n in nodes if n.name == 'div' and 'block-child-p' in n.get('class', [])]
            if not old or compact(raw) != compact(old) or not extra_blocks:
                raise ValueError(f'Not a proven legacy p-only Extract omission: {normalize_doi(row["doi"])}')
            row['abstract'] = full
            changed.append(normalize_doi(row['doi']))
        if not changed:
            raise ValueError('No eligible legacy p-only Extract omissions')
        capture.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        validate_reading_controls(data, html_path, provider=provider)
        result = read_browser_issue(str(capture), provider=provider, code=code,
                                    volume=volume, issue=issue, journal_name=journal_name)
        if not result.ok:
            raise ValueError(f'Repaired capture did not pass import validation: {result.outcome}')
        # Do not publish a snapshot if the original evidence changed mid-operation.
        if {str(p.relative_to(source.parent)): _hash(p) for p in _evidence_files(source)} != hashes:
            raise ValueError('Original evidence changed during repair')
        record = {
            'schema': 'papertrack-cached-extract-repair-v1',
            'repaired_at': datetime.now(timezone.utc).isoformat(),
            'source_capture': str(source), 'source_sha256': hashes,
            'changed_dois': changed, 'articles': len(result.entries),
            'validation': {'reading_controls': 'passed', 'read_browser_issue': 'passed'},
            'capture': str(destination / source.name), 'dry_run': dry_run,
        }
        (staging / 'extract-repair.json').write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        if not dry_run:
            if destination.exists():
                raise FileExistsError(destination)
            os.rename(staging, destination)
        return record
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source_capture')
    parser.add_argument('output_dir')
    for name in ('provider', 'code', 'volume', 'issue', 'journal-name'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--dry-run', action='store_true')
    args = vars(parser.parse_args())
    print(json.dumps(repair_cached_extract_capture(**args), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

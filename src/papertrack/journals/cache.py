"""Repair and import eligible local Silverchair Extract captures during refresh."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
from types import SimpleNamespace
import uuid
from urllib.parse import urlsplit

from papertrack.journals.capture import validate_reading_controls
from papertrack.journals.cache_repair import repair_cached_extract_capture
from papertrack.journals.task import issue_url
from papertrack.journals.storage import runs_root

logger = logging.getLogger(__name__)
EXTRACT_CONFLICT = 'Extract text conflicts with HTML evidence'


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _save_result(run_dir: Path, record: dict) -> None:
    path = run_dir / 'result.json'
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def _canonical_official_capture(record: dict, folder: Path, journal: str, cfg: dict):
    volume, issue = str(record.get('volume') or ''), str(record.get('issue') or '')
    if not volume.isdecimal() or not issue.isdecimal() or record.get('kind') == 'archive':
        return None
    capture = Path(record.get('capture_path') or folder / 'capture.json')
    if not capture.is_file() or not Path(str(capture) + '.html').is_file():
        return None
    data = _json(capture)
    if not data or data.get('schema') != 'papertrack-browser-issue-v1':
        return None
    if (str(data.get('volume')) != volume or str(data.get('issue')) != issue
            or data.get('capture_method') != 'fixed_browser'
            or data.get('collector') != 'papertrack-browser-v2'):
        return None
    expected = urlsplit(issue_url(cfg, volume, issue))
    actual = urlsplit(str(data.get('url') or record.get('url') or ''))
    if (actual.scheme != 'https' or actual.netloc != expected.netloc
            or actual.path.rstrip('/') != expected.path.rstrip('/')
            or (record.get('journal') and record.get('journal') != journal)):
        return None
    return volume, issue, capture, data


def _latest_official_captures(data_dir: str | Path, journal: str, cfg: dict) -> list[tuple[dict, Path, str, str, Path, dict]]:
    """Return the best complete issue-owned capture per formal issue identity."""
    from papertrack.journals.sync import _records

    records = _records(Path(data_dir), journal)  # newest first
    selected = {}
    for record, folder in records:
        candidate = _canonical_official_capture(record, folder, journal, cfg)
        if candidate is None:
            continue
        volume, issue, capture, data = candidate
        provider = cfg.get('provider') or 'acs'
        if provider not in ('acs', 'aip'):
            return []
        # A recent partial/failed run must not mask an older usable capture.
        # Require capture-level completion evidence as well as issue identity.
        if (data.get('remaining_abstracts') != 0 or data.get('errors')
                or not isinstance(data.get('entries'), list)):
            continue
        rank = (2 if record.get('cache_repaired') else
                1 if record.get('status') == 'complete' else 0)
        if rank == 0:
            continue
        key = (volume, issue)
        old = selected.get(key)
        # _records is newest-first; keep that order within an equal rank, but
        # always prefer the package-repaired capture over the original.
        if old is None or rank > old[0]:
            selected[key] = (rank, record, folder, volume, issue, capture, data)
    return [row[1:] for row in selected.values()]


def _run_directory(runs_root: Path, journal: str, volume: str, issue: str) -> Path:
    while True:
        path = runs_root / f'{journal}-{volume}-{issue}-cached-repair-{uuid.uuid4().hex[:8]}'
        if not path.exists():
            return path


def _report_path(data_dir: str | Path, cfg: dict, volume: str, issue: str) -> Path:
    provider = cfg.get('provider') or ('acs' if cfg.get('acs_code') else 'journals')
    return Path(data_dir) / provider / cfg['slug'] / volume / f'{issue}.md'


def _clear_presentation_capture_cache() -> None:
    from papertrack.reports.presentation import _captures
    _captures.cache_clear()


def _import_repaired(issue_record: dict, *, data_dir: str | Path, journal: str,
                     cfg: dict, zotero, import_issue) -> dict:
    run_dir = Path(issue_record['run_directory'])
    capture = Path(issue_record['capture_path'])
    task = SimpleNamespace(
        journal=journal, volume=str(issue_record['volume']), issue=str(issue_record['issue']),
        year=0, browser_snapshot=str(capture), data_dir=str(data_dir),
    )
    try:
        import_issue(task, cfg, zotero)
    except Exception as error:
        issue_record['status'] = 'cache_repaired'
        issue_record['import_error'] = f'{type(error).__name__}: {error}'
        issue_record['updated_at'] = datetime.now(timezone.utc).isoformat()
        _save_result(run_dir, issue_record)
        _clear_presentation_capture_cache()
        raise
    report = _report_path(data_dir, cfg, task.volume, task.issue)
    if not report.is_file():
        issue_record['status'] = 'cache_repaired'
        issue_record['import_error'] = f'Importer returned without writing report: {report}'
        issue_record['updated_at'] = datetime.now(timezone.utc).isoformat()
        _save_result(run_dir, issue_record)
        _clear_presentation_capture_cache()
        raise RuntimeError(issue_record['import_error'])
    issue_record.update(status='complete', report_path=str(report.resolve()),
                        updated_at=datetime.now(timezone.utc).isoformat())
    issue_record.pop('import_error', None)
    _save_result(run_dir, issue_record)
    _clear_presentation_capture_cache()
    return issue_record


def prepare_cached_extract_repairs(data_dir: str | Path, journal: str, cfg: dict,
                                   zotero, import_issue) -> dict:
    """Repair only explicit Silverchair Extract/HTML conflicts in local captures.

    All failures are returned per issue; a failed issue never stops later repair
    candidates or archive discovery. This function performs no network access.
    """
    root = Path(data_dir)
    runs_directory = runs_root()
    provider = cfg.get('provider') or 'acs'
    if provider not in ('acs', 'aip'):
        return {'journal': journal, 'repaired': [], 'failures': [], 'skipped': []}
    code = cfg.get('acs_code') or cfg['slug']
    repaired, failures, skipped = [], [], []

    for record, source_run_dir, volume, issue, capture, data in _latest_official_captures(root, journal, cfg):
        already_repaired = bool(record.get('cache_repaired'))
        if already_repaired:
            if record.get('status') == 'complete' and record.get('report_path'):
                skipped.append({'volume': volume, 'issue': issue, 'reason': 'already_imported_repair'})
                continue
            try:
                outcome = _import_repaired(record, data_dir=root, journal=journal, cfg=cfg,
                                           zotero=zotero, import_issue=import_issue)
                repaired.append({'volume': volume, 'issue': issue,
                                 'run_directory': record['run_directory'],
                                 'status': outcome['status'], 'changed_dois': record.get('cache_repair', {}).get('changed_dois', [])})
            except Exception as error:
                detail = {'volume': volume, 'issue': issue, 'capture': str(capture),
                          'failure': f'{type(error).__name__}: {error}', 'phase': 'retry_package_import'}
                failures.append(detail)
                logger.exception('Cached Extract repair import failed: %s %s/%s', journal, volume, issue)
            continue

        if (record.get('status') == 'complete' and record.get('cache_repair')
                and record.get('report_path')):
            skipped.append({'volume': volume, 'issue': issue, 'reason': 'already_imported_repair'})
            continue

        html = Path(str(capture) + '.html')
        # This narrow migration is only for Extract-bearing Silverchair
        # captures. Do not re-parse other captures as part of daily refresh.
        if not any((entry.get('abstract_type') or '').lower() == 'extract'
                   for entry in data['entries'] if isinstance(entry, dict)):
            skipped.append({'volume': volume, 'issue': issue, 'reason': 'no_extract_entries'})
            continue
        try:
            validate_reading_controls(data, html, provider=provider)
        except Exception as error:
            message = str(error)
            if EXTRACT_CONFLICT not in message:
                detail = {'volume': volume, 'issue': issue, 'capture': str(capture),
                          'failure': f'{type(error).__name__}: {message}',
                          'phase': 'source_validation_not_eligible'}
                failures.append(detail)
                logger.warning('Cached capture not eligible for Extract repair: %s %s/%s: %s',
                               journal, volume, issue, message)
                continue

        else:
            skipped.append({'volume': volume, 'issue': issue, 'reason': 'source_validation_passed'})
            continue

        source_hash = _sha256(capture)
        run_dir = _run_directory(runs_directory, journal, volume, issue)
        try:
            repair = repair_cached_extract_capture(
                capture, run_dir, provider=provider, code=code, volume=volume,
                issue=issue, journal_name=cfg['name'])
            repaired_capture = Path(repair['capture']).resolve()
            source_path = Path(capture).resolve()
            result_data = _json(repaired_capture)
            url = str((result_data or {}).get('url') or record.get('url') or '')
            new_record = {
                'journal': journal, 'provider': provider, 'volume': volume, 'issue': issue,
                'url': url, 'status': 'cache_repaired', 'kind': 'cached_extract_repair',
                'updated_at': datetime.now(timezone.utc).isoformat(),
                'capture_path': str(repaired_capture), 'article_count': repair['articles'],
                'cache_repaired': True, 'agent_invoked': False,
                'source_run_directory': str(source_run_dir.resolve()),
                'source_capture': str(source_path), 'source_capture_sha256': source_hash,
                'cache_repair': {
                    'evidence_path': str((run_dir / 'extract-repair.json').resolve()),
                    'changed_dois': repair['changed_dois'],
                    'source_sha256': repair['source_sha256'],
                },
                'run_directory': str(run_dir.resolve()),
            }
            _save_result(run_dir, new_record)
            _clear_presentation_capture_cache()
            outcome = _import_repaired(new_record, data_dir=root, journal=journal, cfg=cfg,
                                       zotero=zotero, import_issue=import_issue)
            repaired.append({'volume': volume, 'issue': issue,
                             'run_directory': str(run_dir.resolve()),
                             'status': outcome['status'], 'changed_dois': repair['changed_dois']})
        except Exception as error:
            detail = {'volume': volume, 'issue': issue, 'capture': str(capture),
                      'failure': f'{type(error).__name__}: {error}',
                      'phase': 'repair_or_package_import'}
            failures.append(detail)
            logger.exception('Cached Extract repair failed: %s %s/%s', journal, volume, issue)

    return {'journal': journal, 'repaired': repaired, 'failures': failures, 'skipped': skipped}

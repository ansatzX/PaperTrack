"""Date-range journal reconciliation: archive first, validate cache, fetch holes."""
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
import json
import logging
import tempfile

from papertrack.journals.capture import read_browser_issue, validate_reading_controls
from papertrack.journals.agent import run_issue_agent
from papertrack.journals.inventory import canonical_url, parse_archive_page, validate_inventory
from papertrack.journals.task import archive_url

logger = logging.getLogger(__name__)


def _previously_observed_issues(root, journal, cfg):
    """Discovery history is not collection success; retain failed issue identities."""
    known = set()
    # Durable reports predate sync plans and remain the best local evidence
    # that an issue identity was already known. They are not capture cache hits.
    provider = cfg.get('provider') or ('acs' if cfg.get('acs_code') else 'journals')
    reports = Path(root) / provider / cfg['slug']
    if reports.is_dir():
        for volume_dir in reports.iterdir():
            if not volume_dir.is_dir() or not volume_dir.name.isdecimal():
                continue
            for issue_file in volume_dir.iterdir():
                if (issue_file.is_file() and issue_file.suffix == '.md'
                        and issue_file.stem.isdecimal()):
                    known.add((volume_dir.name, issue_file.stem))
                elif (issue_file.is_dir() and issue_file.name.isdecimal()
                      and (issue_file / 'report.md').is_file()):
                    known.add((volume_dir.name, issue_file.name))

    for path in (root / '.papertrack-runs').glob('*-sync-*/plan.json'):
        try:
            plan = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if plan.get('journal') != journal or not plan.get('archive_evidence'):
            continue
        # The inventory is persisted before issue work begins. A crash can
        # therefore leave rows marked "missing" that were never attempted.
        # Only count statuses proving that the issue reached an attempt or
        # completed an import; leave pre-attempt rows eligible for resumption.
        observed_statuses = {
            'attempt_started', 'failed', 'fetched', 'reused', 'repair_required',
        }
        known.update((str(row['volume']), str(row['issue']))
                     for row in plan.get('issues', [])
                     if row.get('volume') and row.get('issue')
                     and row.get('status') in observed_statuses)
    return known


def _records(root, journal):
    records = []
    for path in (root / '.papertrack-runs').glob('*/result.json'):
        try:
            record = json.loads(path.read_text())
        except (ValueError, OSError):
            continue
        if record.get('journal') == journal:
            records.append((record, path.parent))
    return sorted(records, key=lambda x: x[0].get('updated_at', ''), reverse=True)


def _archive_cache(records, url, today):
    for record, folder in records:
        # A completed issue's timestamp says nothing about when its legacy
        # archive HTML was captured. Only archive-owned metadata proves freshness.
        if record.get('status') != 'archive_captured':
            continue
        capture = folder / 'capture.json'
        evidence = Path(str(capture) + '.html')
        try:
            data = json.loads(capture.read_text())
            requested = data.get('requested_url')
            observed = data.get('url')
            if (data.get('schema') != 'papertrack-browser-archive-v1'
                    or not isinstance(requested, str) or not requested
                    or not isinstance(observed, str) or not observed
                    or canonical_url(requested) != canonical_url(url)
                    or canonical_url(record.get('url', '')) != canonical_url(requested)
                    or canonical_url(record.get('observed_url', '')) != canonical_url(observed)
                    or (record.get('capture_path')
                        and Path(record['capture_path']).resolve() != capture.resolve())):
                continue
            captured = datetime.fromisoformat(data.get('captured_at', '').replace('Z', '+00:00'))
            # Fixed collector timestamps include an offset. Naive/date-only old
            # values cannot establish a local-day boundary reliably.
            if captured.tzinfo is None or captured.astimezone().date() != today:
                continue
        except (OSError, ValueError, TypeError, AttributeError):
            continue
        if evidence.is_file():
            return evidence, observed
    return None


def discover_inventory(args, cfg, start, end):
    root = Path(args.data_dir)
    records = _records(root, args.journal)
    entry_url = archive_url(cfg)
    if cfg.get('provider') == 'aip':
        entry_url += '/browse-by-year'
    queue, visited, issues, evidence = [entry_url], set(), {}, []
    while queue:
        url = canonical_url(queue.pop(0))
        if url in visited:
            continue
        if len(visited) >= 64:
            raise ValueError('Archive traversal exceeded 64 pages; inspect retained evidence')
        cached = _archive_cache(records, url, date.today())
        if cached:
            html, observed = cached
            logger.info('Archive cache: %s', url)
        else:
            run = run_issue_agent(journal=args.journal, cfg=cfg, volume='', issue='',
                                  year=start.year, data_dir=args.data_dir, model=args.agent_model,
                                  timeout=args.agent_timeout, archive_page=url)
            html = Path(str(run.capture_path) + '.html')
            observed = run.record['observed_url']
        rows, links = parse_archive_page(cfg, html, observed, start, end)
        visited.update((url, canonical_url(observed)))
        evidence.append(str(html))
        for row in rows:
            key = (row.volume, row.issue)
            if key in issues and issues[key] != row:
                raise ValueError(f'Conflicting archive identity/date: {key}')
            issues[key] = row
        queue.extend(link for link in links if canonical_url(link) not in visited)
    rows = sorted(issues.values(), key=lambda r: (r.published, int(r.volume), int(r.issue)))
    validate_inventory(rows, start, end)
    return rows, evidence


def validated_cached_issue(records, cfg, row):
    extractor_errors = []
    for record, folder in records:
        if (str(record.get('volume')), str(record.get('issue'))) != (row.volume, row.issue):
            continue
        # A failed import may still retain a complete valid capture. Validate the
        # artifact itself; result status and existence of Markdown prove nothing.
        capture = folder / 'capture.json'
        if not capture.is_file():
            capture = Path(record.get('capture_path') or capture)
        try:
            data = json.loads(capture.read_text())
            if (data.get('collector') != 'papertrack-browser-v2' or
                    data.get('capture_method') != 'fixed_browser' or
                    data.get('remaining_abstracts') != 0):
                raise ValueError('Incomplete fixed-collector capture')
            parsed = read_browser_issue(str(capture), provider=cfg.get('provider') or 'acs',
                                        code=cfg.get('acs_code') or cfg['slug'], volume=row.volume,
                                        issue=row.issue, journal_name=cfg['name'])
            unavailable = set(parsed.metadata['abstract_unavailable_dois'])
            if any(not e.abstract and e.doi not in unavailable for e in parsed.entries.values()):
                raise ValueError('Unresolved abstract')
            validate_reading_controls(data, Path(str(capture) + '.html'),
                                      provider=cfg.get('provider') or 'acs')
        except (OSError, ValueError, KeyError, TypeError) as error:
            logger.warning('Invalid cache %s: %s', capture, error)
            if str(error).startswith(('Unsupported Math', 'Reading source cannot be rendered:')):
                extractor_errors.append(f'{capture}: {error}')
            continue
        return capture
    if extractor_errors:
        raise ValueError('Cached HTML needs an extractor update, not a refetch: ' + '; '.join(extractor_errors))
    return None


def sync_journal(args, cfg, *, from_date, to_date):
    from papertrack.cli import _import_journal_issue, _run_journal_issue, _build_zotero

    start, end = date.fromisoformat(from_date), date.fromisoformat(to_date)
    if start > end:
        raise ValueError('Journal date range is reversed')
    root = Path(args.data_dir)
    runs = root / '.papertrack-runs'
    known = _previously_observed_issues(root, args.journal, cfg)
    runs.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix=f'{args.journal}-sync-', dir=runs))
    plan = dict(journal=args.journal, from_date=from_date, to_date=to_date,
                status='discovering', issues=[])

    def save():
        tmp = folder / 'plan.tmp'
        tmp.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + '\n')
        tmp.replace(folder / 'plan.json')

    save()
    try:
        inventory, evidence = discover_inventory(args, cfg, start, end)
        records = _records(root, args.journal)
        plan.update(status='checking_cache', archive_evidence=evidence)
        captures = []
        for row in inventory:
            cache_error = ''
            try:
                capture = validated_cached_issue(records, cfg, row)
            except ValueError as error:
                # Retained source needing a parser fix must not cause a refetch,
                # or prevent newly discovered independent issues from running.
                capture, cache_error = None, str(error)
            captures.append(capture)
            plan['issues'].append(dict(volume=row.volume, issue=row.issue,
                                       date=row.published.isoformat(), url=row.url,
                                       status='repair_required' if cache_error else 'cached' if capture else 'missing',
                                       cache_error=cache_error,
                                       capture=str(capture) if capture else None))
        plan['status'] = 'syncing'
        save()
        logger.info('%s inventory: %s issues, %s cached, %s missing; plan: %s',
                    args.journal, len(inventory), sum(p is not None for p in captures),
                    sum(p is None for p in captures), folder / 'plan.json')
        zotero = _build_zotero()
        failures = []
        for row, capture, item in zip(inventory, captures, plan['issues']):
            if item['cache_error']:
                item['error'] = item['cache_error']
                failures.append(f'{row.volume}/{row.issue}')
                logger.error('Cached issue needs extractor repair: %s %s/%s: %s',
                             args.journal, row.volume, row.issue, item['cache_error'])
                save()
                continue
            if (not capture and getattr(args, 'new_issues_only', False)
                    and (row.volume, row.issue) in known):
                item.update(status='repair_required',
                            error='Previously observed issue has no valid cache; daily discovery does not retry historical gaps')
                failures.append(f'{row.volume}/{row.issue}')
                logger.error('Known issue needs repair: %s %s/%s; no daily refetch',
                             args.journal, row.volume, row.issue)
                save()
                continue
            task = SimpleNamespace(**vars(args))
            task.volume, task.issue, task.year = row.volume, row.issue, row.published.year
            task.journal_command = 'issue'
            task.browser_snapshot = str(capture) if capture else ''
            task.capture_path = ''
            task.run_agent = capture is None
            try:
                if capture:
                    _import_journal_issue(task, cfg, zotero)
                    item['status'] = 'reused'
                else:
                    # Persist the attempt boundary before invoking the agent;
                    # a process interruption must not silently retry it daily.
                    item['status'] = 'attempt_started'
                    save()
                    run = _run_journal_issue(task, zotero)
                    item['status'] = 'fetched'
                    if run is not None:
                        item['capture'] = str(run.capture_path)
            except Exception as error:
                item.update(status='failed', error=str(error))
                failures.append(f'{row.volume}/{row.issue}')
                logger.exception('Issue sync failed: %s %s/%s', args.journal, row.volume, row.issue)
            save()
        if failures:
            raise RuntimeError('Unresolved issue cache gaps: ' + ', '.join(failures))
        plan['status'] = 'complete'
        save()
    except BaseException as error:
        plan.update(status='failed', error=str(error))
        save()
        raise

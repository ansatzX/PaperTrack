import argparse
import logging
import os
import time
from datetime import date
from pathlib import Path

from papertrack.calendar_plan import (
    iter_date_range,
    month_days,
    parse_iso_date,
    rolling_refresh_days,
)
from papertrack.config import journal_configs, query_args
from papertrack.diagnostics import configure_diagnostics
from papertrack.reports.migration import migrate_reports, migrate_report_file, remove_report_provenance
from papertrack.reports.repair import collect_repair_candidates, repair_reports
from papertrack.reports.render import ArxivRateLimitError, filter_arxiv_days_to_md, filter_journal_to_md
from papertrack.journals.task import issue_task, discovery_task
from papertrack.journals.agent import DEFAULT_MODEL, DEFAULT_TIMEOUT, run_issue_agent
from papertrack.zotero_query import ZoteroQuery

logger = logging.getLogger(__name__)


DEFAULT_DATA_DIR = "/home/ansatz/data/obsidian/1/papertrack_datas/"


def _journal_output_root(base_dir: str, cfg: dict[str, str]) -> str:
    """Return the provider-specific journal output root."""
    provider = cfg.get("provider", "")
    if not provider and cfg.get("acs_code"):
        provider = "acs"
    if not provider:
        provider = "journals"
    return os.path.join(base_dir, provider)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="papertrack")
    parser.add_argument("--debug", action="store_true", help="enable debug logging")
    parser.add_argument("--diagnostics-dir", default="",
                        help="directory for diagnostics JSONL and snapshots")
    parser.add_argument("--diagnostics-all-attempts", action="store_true",
                        help="record successful fetch attempts as well as abnormal ones")
    parser.add_argument("--no-failure-snapshots", action="store_true",
                        help="disable full HTML snapshots for abnormal fetch attempts")
    subparsers = parser.add_subparsers(dest="command", required=True)

    refresh = subparsers.add_parser('refresh', help='daily journal and arXiv refresh')
    _add_common_arxiv_args(refresh)
    refresh.add_argument('--journals', required=True, help='comma-separated journal keys')
    refresh.add_argument('--refresh-days', type=int, default=30)
    refresh.add_argument('--from-date', default='2026-01-01', help='journal range start and initial arXiv backfill start')
    refresh.add_argument('--agent-model', default=DEFAULT_MODEL)
    refresh.add_argument('--agent-timeout', type=float, default=DEFAULT_TIMEOUT)
    refresh.add_argument('--repair-issue', action='append', default=[],
                         help='also recollect a specific journal:volume:issue (repeatable)')
    refresh.set_defaults(handler=_run_refresh)

    arxiv = subparsers.add_parser("arxiv", help="arXiv reading ledger commands")
    arxiv_sub = arxiv.add_subparsers(dest="arxiv_command", required=True)

    arxiv_refresh = arxiv_sub.add_parser(
        "refresh",
        help="refresh recent arXiv day buckets",
    )
    _add_common_arxiv_args(arxiv_refresh)
    arxiv_refresh.add_argument(
        "--refresh-days",
        default=30,
        type=int,
        help="number of days before anchor date to refresh",
    )
    arxiv_refresh.add_argument(
        "--anchor-date",
        default="",
        help="YYYY-MM-DD anchor date; defaults to local today",
    )
    arxiv_refresh.set_defaults(handler=_run_arxiv_refresh)

    arxiv_backfill = arxiv_sub.add_parser(
        "backfill",
        help="backfill historical arXiv days",
    )
    _add_common_arxiv_args(arxiv_backfill)
    arxiv_backfill.add_argument("--month", default="", help="YYYY-MM month to backfill")
    arxiv_backfill.add_argument(
        "--from-date",
        default="",
        help="YYYY-MM-DD start date for explicit backfill range",
    )
    arxiv_backfill.add_argument(
        "--to-date",
        default="",
        help="YYYY-MM-DD end date for explicit backfill range",
    )
    arxiv_backfill.set_defaults(handler=_run_arxiv_backfill)

    journal = subparsers.add_parser("journal", help="journal issue ledger commands")
    journal_sub = journal.add_subparsers(dest="journal_command", required=True)

    journal_latest = journal_sub.add_parser(
        "latest",
        help="print an agent task to discover the latest complete formal issue",
    )
    _add_common_journal_args(journal_latest)
    journal_latest.set_defaults(handler=_run_journal_latest)

    journal_refresh = journal_sub.add_parser(
        'refresh', help='reconcile formal issues in a date range using validated caches first')
    _add_common_journal_args(journal_refresh)
    journal_refresh.add_argument('--from-date', default='2026-01-01')
    journal_refresh.add_argument('--to-date', default='')
    journal_refresh.add_argument('--agent-model', default=DEFAULT_MODEL)
    journal_refresh.add_argument('--agent-timeout', type=float, default=DEFAULT_TIMEOUT)
    journal_refresh.set_defaults(handler=_run_journal_refresh, run_agent=True,
                                 volume='', issue='', year=0, capture_path='', browser_snapshot='')

    journal_backfill = journal_sub.add_parser(
        "backfill",
        help="print an agent task to discover historical formal issues",
    )
    _add_common_journal_args(journal_backfill)
    journal_backfill.add_argument(
        "--from-year",
        default=0,
        type=int,
        help="start backfill from this year",
    )
    journal_backfill.set_defaults(handler=_run_journal_backfill)

    journal_issue = journal_sub.add_parser(
        "issue",
        help="run an agent, print an issue task, or import a capture",
    )
    _add_common_journal_args(journal_issue)
    journal_issue.add_argument("--volume", required=True, help="volume number")
    journal_issue.add_argument("--issue", required=True, help="issue number")
    journal_issue.add_argument(
        "--year",
        default=0,
        type=int,
        help="publication year for this issue",
    )
    issue_mode = journal_issue.add_mutually_exclusive_group()
    issue_mode.add_argument("--browser-snapshot", default="",
                               help="local official-page capture returned by an agent; imports without network enrichment")
    issue_mode.add_argument("--run-agent", action="store_true",
                            help="launch Codex, validate its capture, then write the report")
    journal_issue.add_argument("--capture-path", default="",
                               help="new capture path (agent runs default to a unique run directory)")
    journal_issue.add_argument("--resume-capture", default="",
                               help="clone a partial fixed capture into a new bounded --run-agent call")
    journal_issue.add_argument("--agent-model", default=DEFAULT_MODEL,
                               help="Codex model for --run-agent (default: %(default)s)")
    journal_issue.add_argument("--agent-timeout", type=float, default=DEFAULT_TIMEOUT,
                               help="agent process deadline in seconds (default: %(default)s)")
    journal_issue.set_defaults(handler=_run_journal_issue)

    migrate = subparsers.add_parser(
        "migrate",
        help="migrate existing generated Markdown reports to the current schema",
    )
    migrate.add_argument("--data-dir", default=DEFAULT_DATA_DIR, help="base folder for output")
    migration_mode = migrate.add_mutually_exclusive_group()
    migration_mode.add_argument("--remove-provenance-only", action="store_true",
                         help="remove generated source JSON without parsing captures or changing report fields")
    migration_mode.add_argument("--journal-layout-only", action="store_true",
                                help="merge volume/issue/report.md into volume/issue.md without fetching")
    migrate.add_argument("--backup-dir", default="",
                         help="required backup directory for a focused local migration")
    migrate.set_defaults(handler=_run_migrate)

    repair = subparsers.add_parser(
        "repair",
        help="collect damaged reports and optionally refetch authoritative sources",
    )
    repair.add_argument("--data-dir", default=DEFAULT_DATA_DIR, help="base folder for output")
    repair.add_argument("--yes", action="store_true", help="fetch without prompting")
    repair.add_argument("--no-fetch", action="store_true", help="record repair candidates as deferred")
    repair.set_defaults(handler=_run_repair)

    return parser


def _add_common_arxiv_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR, help="base folder for output")
    parser.add_argument(
        "--category",
        default="quant-ph",
        help="comma-separated arXiv categories",
    )


def _add_common_journal_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR, help="base folder for output")
    parser.add_argument(
        "--journal",
        required=True,
        help="journal key from categories.toml, e.g. jctc or jcp",
    )


def _build_zotero() -> ZoteroQuery | None:
    try:
        return ZoteroQuery()
    except ConnectionError as e:
        logger.warning("Zotero not available: %s", e)
    except Exception as e:
        logger.error("Unexpected Zotero error: %s", e)
    return None


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s[%(levelname)s] %(message)s")
    parser = _build_parser()
    args = parser.parse_args()
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
    configure_diagnostics(
        diagnostics_dir=args.diagnostics_dir or None,
        all_attempts=args.diagnostics_all_attempts,
        failure_snapshots=not args.no_failure_snapshots,
    )
    task_only = args.command == 'refresh' or (
        args.command == "journal" and not getattr(args, "browser_snapshot", ""))
    zotero = None if task_only else _build_zotero()
    args.handler(args, zotero)


def _run_refresh(args, zotero: ZoteroQuery | None) -> None:
    """Own daily sequencing and logging; run.sh only supplies deployment arguments."""
    import fcntl
    repairs = []
    for value in getattr(args, 'repair_issue', []):
        parts = value.split(':')
        if (len(parts) != 3 or parts[0] not in journal_configs or
                not all(p.isascii() and p.isdecimal() for p in parts[1:])):
            raise ValueError('Expected --repair-issue journal:volume:issue')
        if tuple(parts) not in repairs:
            repairs.append(tuple(parts))
    root = Path(args.data_dir)
    root.mkdir(parents=True, exist_ok=True)
    diagnostics = Path(args.diagnostics_dir or '.papertrack_diagnostics')
    diagnostics.mkdir(parents=True, exist_ok=True)
    lock_path = Path(os.environ.get('XDG_RUNTIME_DIR', str(root))) / 'papertrack-daily.lock'
    with lock_path.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('A daily refresh is already running') from None
        handler = logging.FileHandler(diagnostics / 'daily.log', encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(asctime)s[%(levelname)s] %(message)s'))
        logging.getLogger().addHandler(handler)
        failures = []
        try:
            logger.info('Daily refresh started')
            def journals():
                failures = []
                for journal, volume, issue in repairs:
                    task = argparse.Namespace(
                        journal=journal, volume=volume, issue=issue, year=0,
                        journal_command='issue', run_agent=True, data_dir=args.data_dir,
                        capture_path='', browser_snapshot='', agent_model=args.agent_model,
                        agent_timeout=args.agent_timeout)
                    try:
                        _run_journal_issue(task, None)
                    except Exception:
                        failures.append(f'{journal}:{volume}:{issue}')
                        logger.exception('Requested issue repair failed: %s %s/%s',
                                         journal, volume, issue)
                for journal in args.journals.split(','):
                    journal = journal.strip()
                    if not journal:
                        continue
                    task = argparse.Namespace(
                        journal=journal, data_dir=args.data_dir, journal_command='refresh',
                        run_agent=True, volume='', issue='', year=0, capture_path='',
                        browser_snapshot='', agent_model=args.agent_model,
                        agent_timeout=args.agent_timeout, from_date=args.from_date,
                        to_date=date.today().isoformat(), new_issues_only=True)
                    try:
                        cfg = _get_journal_cfg(journal)
                        journal_dir = Path(_journal_output_root(args.data_dir, cfg)) / cfg['slug']
                        migration_failed = False
                        migrated = 0
                        from papertrack.journals.cache import prepare_cached_extract_repairs
                        try:
                            cache_repairs = prepare_cached_extract_repairs(
                                args.data_dir, journal, cfg, None, _import_journal_issue)
                            for item in cache_repairs['repaired']:
                                logger.info('Cached Extract issue repaired and imported: %s %s/%s; '
                                            'changed DOI(s): %s; no agent call',
                                            journal, item['volume'], item['issue'],
                                            ', '.join(item['changed_dois']))
                            for item in cache_repairs['failures']:
                                failures.append(f"{journal}:cached-extract:{item['volume']}/{item['issue']}")
                                logger.error('Cached Extract repair unavailable: %s %s/%s; phase=%s; %s',
                                             journal, item['volume'], item['issue'], item['phase'],
                                             item['failure'])
                        except Exception:
                            failures.append(journal + ':cached-extract-preparation')
                            logger.exception('Cached Extract preparation failed for journal: %s', journal)
                        for path in sorted(journal_dir.rglob('*.md')):
                            migration = migrate_report_file(str(path), data_dir=args.data_dir)
                            if migration.status == 'failed':
                                migration_failed = True
                                logger.error('Journal migration failed: %s: %s', path, migration.message)
                            elif migration.status not in ('current', 'unsupported', 'missing'):
                                migrated += 1
                        if migration_failed:
                            failures.append(journal + ':migration')
                        logger.info('Journal template migration: %s; changed=%s; failed=%s',
                                    journal, migrated, migration_failed)
                        _run_journal_refresh(task, None)
                    except Exception:
                        failures.append(journal)
                        logger.exception('Journal refresh failed: %s', journal)
                return failures

            def arxiv():
                failures = []
                zotero = _build_zotero()
                for category in args.category.split(','):
                    category = category.strip()
                    if not category:
                        continue
                    task = argparse.Namespace(
                        data_dir=args.data_dir, category=category,
                        refresh_days=args.refresh_days, anchor_date='', month='',
                        from_date=args.from_date, to_date=date.today().isoformat())
                    try:
                        if next((root / 'arxiv' / category).rglob('*.md'), None):
                            _run_arxiv_refresh(task, zotero)
                        else:
                            _run_arxiv_backfill(task, zotero)
                    except ArxivRateLimitError:
                        failures.append(category)
                        logger.exception('arXiv rate limited: stopping remaining categories')
                        break
                    except Exception:
                        failures.append(category)
                        logger.exception('arXiv refresh failed: %s', category)
                return failures

            def run_pipeline(name, worker):
                import threading
                output = logging.FileHandler(diagnostics / f'{name}.log', encoding='utf-8')
                owner = threading.get_ident()
                output.addFilter(lambda record: record.thread == owner)
                output.setFormatter(handler.formatter)
                logging.getLogger().addHandler(output)
                try:
                    logger.info('%s pipeline started', name)
                    return worker()
                finally:
                    logger.info('%s pipeline finished', name)
                    logging.getLogger().removeHandler(output)
                    output.close()

            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix='papertrack') as pool:
                jobs = {name: pool.submit(run_pipeline, name, worker) for name, worker in
                        [('journals', journals), ('arxiv', arxiv)]}
                for name, job in jobs.items():
                    try:
                        failures.extend(job.result())
                    except Exception:
                        failures.append(name)
                        logger.exception('%s pipeline failed', name)
            logger.info('Daily refresh finished; failed sources: %s', failures)
        finally:
            logging.getLogger().removeHandler(handler)
            handler.close()
        if failures:
            raise RuntimeError('Refresh failed for: ' + ', '.join(failures))


def _run_arxiv_refresh(args, zotero: ZoteroQuery | None) -> None:
    if args.anchor_date:
        anchor = parse_iso_date(args.anchor_date)
    else:
        localtime = time.localtime()
        anchor = date(localtime.tm_year, localtime.tm_mon, localtime.tm_mday)
    days = rolling_refresh_days(anchor, args.refresh_days)
    _run_arxiv_days(
        args.data_dir,
        args.category,
        days,
        zotero,
        refresh_existing=True,
    )


def _run_arxiv_backfill(args, zotero: ZoteroQuery | None) -> None:
    has_month = bool(args.month)
    has_range = bool(args.from_date or args.to_date)
    if has_month == has_range:
        raise RuntimeError("arxiv backfill requires exactly one of --month or --from-date/--to-date")
    if has_month:
        days = month_days(args.month)
    else:
        if not args.from_date or not args.to_date:
            raise RuntimeError("arxiv backfill date range requires both --from-date and --to-date")
        days = iter_date_range(parse_iso_date(args.from_date), parse_iso_date(args.to_date))
    _run_arxiv_days(
        args.data_dir,
        args.category,
        days,
        zotero,
        refresh_existing=False,
    )


def _run_arxiv_days(
    base_dir: str,
    categories: str,
    days: list[date],
    zotero: ZoteroQuery | None,
    refresh_existing: bool = False,
) -> None:
    for cat_ in categories.split(","):
        cat_ = cat_.strip()
        if not cat_:
            continue
        try:
            qa = query_args[cat_]
        except KeyError:
            logger.error("Category '%s' not supported. Available: %s", cat_, list(query_args.keys()))
            raise RuntimeError(f"Category '{cat_}' not supported") from None
        logger.info("Fetching %s for %d day buckets", cat_, len(days))
        filter_arxiv_days_to_md(
            days=days,
            md_folder=os.path.join(base_dir, "arxiv", cat_),
            query_args=qa,
            category=cat_,
            zotero=zotero,
            refresh_existing=refresh_existing,
        )


def _get_journal_cfg(journal_key: str) -> dict[str, str]:
    try:
        return journal_configs[journal_key]
    except KeyError:
        logger.error("Journal '%s' not supported. Available: %s", journal_key, list(journal_configs.keys()))
        raise RuntimeError(f"Journal '{journal_key}' not supported") from None


def _run_journal_latest(args, zotero: ZoteroQuery | None) -> None:
    print(discovery_task(args.journal, _get_journal_cfg(args.journal), args.data_dir))


def _run_journal_refresh(args, zotero: ZoteroQuery | None) -> None:
    from papertrack.journals.sync import sync_journal
    sync_journal(args, _get_journal_cfg(args.journal), from_date=args.from_date,
                 to_date=args.to_date or date.today().isoformat())


def _run_journal_backfill(args, zotero: ZoteroQuery | None) -> None:
    print(discovery_task(args.journal, _get_journal_cfg(args.journal), args.data_dir,
                         from_year=args.from_year))


def _run_journal_issue(args, zotero: ZoteroQuery | None) -> None:
    cfg = _get_journal_cfg(args.journal)
    run = None
    if getattr(args, 'resume_capture', '') and not getattr(args, 'run_agent', False):
        raise ValueError('--resume-capture requires --run-agent')
    if getattr(args, "run_agent", False):
        run = run_issue_agent(journal=args.journal, cfg=cfg, volume=args.volume,
                              issue=args.issue, year=args.year, data_dir=args.data_dir,
                              capture_path=args.capture_path, model=args.agent_model,
                              timeout=args.agent_timeout,
                              **({'resume_capture': args.resume_capture} if getattr(args, 'resume_capture', '') else {}),
                              **({'discover_latest': True} if args.journal_command == 'refresh' else {}))
        args.volume, args.issue = run.record['volume'], run.record['issue']
        args.browser_snapshot = str(run.capture_path)
        zotero = _build_zotero()
    if not args.browser_snapshot:
        print(issue_task(args.journal, cfg, args.volume, args.issue, args.year,
                         args.capture_path or "capture.json", args.data_dir))
        return
    try:
        _import_journal_issue(args, cfg, zotero)
    except Exception as error:
        if run:
            run.save("import_failed", error=str(error))
        raise
    if run:
        report_path = os.path.join(_journal_output_root(args.data_dir, cfg), cfg["slug"],
                                   str(args.volume), f"{args.issue}.md")
        run.save("complete", report_path=os.path.abspath(report_path))
        logger.info('Journal refreshed: %s %s/%s; report: %s',
                    args.journal, args.volume, args.issue, report_path)
        coverage = run.record['reading_coverage']
        print(f"Abstract: {coverage['abstract']}; Extract: {coverage['extract']}; "
              f"source-confirmed unavailable: {coverage['missing']}; "
              f"excluded: {coverage['excluded']}")
        print(f"Report: {os.path.abspath(report_path)}\nRun evidence: {run.run_dir}")
    return run


def _import_journal_issue(args, cfg: dict, zotero: ZoteroQuery | None) -> None:
    ok = filter_journal_to_md(
        journal_name=cfg["name"],
        journal_slug=cfg["slug"],
        volume=args.volume,
        issue=args.issue,
        issn=cfg["issn"],
        md_folder=_journal_output_root(args.data_dir, cfg),
        zotero=zotero,
        year=args.year or None,
        acs_code=cfg.get("acs_code", ""),
        provider=cfg.get("provider", ""),
        **({"browser_snapshot": args.browser_snapshot} if getattr(args, "browser_snapshot", "") else {}),
    )

    if ok is False:
        raise RuntimeError("Issue report was not written; see fetch diagnostics")


def _run_migrate(args, zotero: ZoteroQuery | None) -> None:
    del zotero
    if getattr(args, 'remove_provenance_only', False):
        results = remove_report_provenance(args.data_dir, args.backup_dir)
    elif getattr(args, 'journal_layout_only', False):
        from papertrack.reports.layout import migrate_journal_layout
        results = migrate_journal_layout(args.data_dir, args.backup_dir)
    else:
        results = migrate_reports(args.data_dir)
    counts: dict[str, int] = {}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    logger.info("Migration summary for %s: %s", args.data_dir, counts)
    failed = counts.get("failed", 0)
    if failed:
        for result in results:
            if result.status == 'failed':
                logger.error('Migration failed for %s: %s', result.path, result.message)
        raise RuntimeError(f"migration failed for {failed} report(s)")


def _run_repair(args, zotero: ZoteroQuery | None) -> None:
    candidates = collect_repair_candidates(args.data_dir)
    if not candidates:
        logger.info("No repair candidates found under %s", args.data_dir)
        return
    logger.warning("Found %d repair candidate(s) under %s", len(candidates), args.data_dir)
    results = repair_reports(
        args.data_dir,
        candidates,
        zotero=zotero,
        assume_yes=args.yes,
        no_fetch=args.no_fetch,
    )
    counts: dict[str, int] = {}
    for result in results:
        counts[result.outcome] = counts.get(result.outcome, 0) + 1
    logger.info("Repair summary for %s: %s", args.data_dir, counts)
    unresolved = counts.get("unresolved", 0)
    if unresolved:
        raise RuntimeError(f"repair unresolved for {unresolved} report(s)")

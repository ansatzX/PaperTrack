import argparse
import logging
import os
import time
from datetime import date

from .calendar_plan import (
    iter_date_range,
    month_days,
    parse_iso_date,
    rolling_refresh_days,
)
from .codex import journal_configs, query_args
from .diagnostics import configure_diagnostics
from .migration import migrate_reports
from .report import filter_arxiv_days_to_md, filter_journal_auto, filter_journal_to_md
from .zotero_query import ZoteroQuery

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
        help="process the latest discovered journal issue",
    )
    _add_common_journal_args(journal_latest)
    journal_latest.set_defaults(handler=_run_journal_latest)

    journal_backfill = journal_sub.add_parser(
        "backfill",
        help="backfill discovered journal issues",
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
        help="process an explicit journal issue",
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
    journal_issue.set_defaults(handler=_run_journal_issue)

    migrate = subparsers.add_parser(
        "migrate",
        help="migrate existing generated Markdown reports to the current schema",
    )
    migrate.add_argument("--data-dir", default=DEFAULT_DATA_DIR, help="base folder for output")
    migrate.set_defaults(handler=_run_migrate)

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
    parser = _build_parser()
    args = parser.parse_args()
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
    configure_diagnostics(
        diagnostics_dir=args.diagnostics_dir or None,
        all_attempts=args.diagnostics_all_attempts,
        failure_snapshots=not args.no_failure_snapshots,
    )
    zotero = _build_zotero()
    args.handler(args, zotero)


def _run_arxiv_refresh(args, zotero: ZoteroQuery | None) -> None:
    if args.anchor_date:
        anchor = parse_iso_date(args.anchor_date)
    else:
        localtime = time.localtime()
        anchor = date(localtime.tm_year, localtime.tm_mon, localtime.tm_mday)
    days = rolling_refresh_days(anchor, args.refresh_days)
    _run_arxiv_days(args.data_dir, args.category, days, zotero)


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
    _run_arxiv_days(args.data_dir, args.category, days, zotero)


def _run_arxiv_days(
    base_dir: str,
    categories: str,
    days: list[date],
    zotero: ZoteroQuery | None,
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
        )


def _get_journal_cfg(journal_key: str) -> dict[str, str]:
    try:
        return journal_configs[journal_key]
    except KeyError:
        logger.error("Journal '%s' not supported. Available: %s", journal_key, list(journal_configs.keys()))
        raise RuntimeError(f"Journal '{journal_key}' not supported") from None


def _run_journal_latest(args, zotero: ZoteroQuery | None) -> None:
    cfg = _get_journal_cfg(args.journal)
    filter_journal_auto(
        journal_name=cfg["name"],
        journal_slug=cfg["slug"],
        issn=cfg["issn"],
        acs_code=cfg.get("acs_code", ""),
        md_folder=_journal_output_root(args.data_dir, cfg),
        zotero=zotero,
        provider=cfg.get("provider", "acs"),
        backfill=False,
        from_year=0,
    )


def _run_journal_backfill(args, zotero: ZoteroQuery | None) -> None:
    cfg = _get_journal_cfg(args.journal)
    filter_journal_auto(
        journal_name=cfg["name"],
        journal_slug=cfg["slug"],
        issn=cfg["issn"],
        acs_code=cfg.get("acs_code", ""),
        md_folder=_journal_output_root(args.data_dir, cfg),
        zotero=zotero,
        provider=cfg.get("provider", "acs"),
        backfill=True,
        from_year=args.from_year,
    )


def _run_journal_issue(args, zotero: ZoteroQuery | None) -> None:
    cfg = _get_journal_cfg(args.journal)
    filter_journal_to_md(
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
    )


def _run_migrate(args, zotero: ZoteroQuery | None) -> None:
    del zotero
    results = migrate_reports(args.data_dir)
    counts: dict[str, int] = {}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    logger.info("Migration summary for %s: %s", args.data_dir, counts)
    failed = counts.get("failed", 0)
    if failed:
        raise RuntimeError(f"migration failed for {failed} report(s)")

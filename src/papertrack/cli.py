import time
import os
import argparse
import logging

from .report import filter_arxiv_to_md, filter_journal_to_md, filter_journal_auto
from .zotero_query import ZoteroQuery
from .codex import query_args, journal_configs

logger = logging.getLogger(__name__)


def _journal_output_root(base_dir: str, cfg: dict[str, str]) -> str:
    """Return the provider-specific journal output root."""
    provider = cfg.get("provider", "")
    if not provider and cfg.get("acs_code"):
        provider = "acs"
    if not provider:
        provider = "journals"
    return os.path.join(base_dir, provider)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--debug", action="store_true",
                        help="enable debug logging")
    parser.add_argument("--source", default="arxiv", choices=["arxiv", "journal"],
                        help="data source type (default: arxiv)")
    parser.add_argument("--data_dir", default="/home/ansatz/data/obsidian/1/papertrack_datas/",
                        help="base folder for output", type=str)

    # arXiv mode
    parser.add_argument("--time", default="1949.10",
                        help="[arxiv] time to query (YYYY.MM). 1949.10 = current month.", type=str)
    parser.add_argument("--category", default="quant-ph",
                        help="[arxiv] comma-separated categories", type=str)
    parser.add_argument("--output_format", default="category/year/month/day",
                        help="[arxiv] output folder format", type=str)

    # Journal mode
    parser.add_argument("--journal", default="",
                        help="[journal] journal key (e.g. jctc)", type=str)
    parser.add_argument("--volume", default="",
                        help="[journal] volume number (omit for auto-detect)", type=str)
    parser.add_argument("--issue", default="",
                        help="[journal] issue number (omit for auto-detect)", type=str)
    parser.add_argument("--year", default=0, type=int,
                        help="[journal] publication year for explicit volume/issue mode")
    parser.add_argument("--backfill", action="store_true",
                        help="[journal] process all historical issues")
    parser.add_argument("--from_year", default=0, type=int,
                        help="[journal] start backfill from this year")

    args = parser.parse_args()
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    zotero: ZoteroQuery | None = None
    try:
        zotero = ZoteroQuery()
    except ConnectionError as e:
        logger.warning("Zotero not available: %s", e)
    except Exception as e:
        logger.error("Unexpected Zotero error: %s", e)

    if args.source == "journal":
        _run_journal(args, zotero)
    else:
        _run_arxiv(args, zotero)


def _run_arxiv(args, zotero: ZoteroQuery | None):
    base_dir = args.data_dir
    categories = args.category
    output_format = args.output_format
    times = args.time

    for time_str in times.split(","):
        for cat_ in categories.split(","):
            try:
                qa = query_args[cat_]
            except KeyError:
                logger.error("Category '%s' not supported. Available: %s", cat_, list(query_args.keys()))
                raise RuntimeError(f"Category '{cat_}' not supported") from None

            if time_str == "1949.10":
                localtime = time.localtime()
                year = int(localtime.tm_year)
                month = int(localtime.tm_mon)
            else:
                year = int(time_str.split(".")[0])
                month = int(time_str.split(".")[1])

            logger.info("Fetching %s for %s.%02d", cat_, year, month)
            filter_arxiv_to_md(
                year=year,
                month=month,
                md_folder=os.path.join(base_dir, "arxiv", cat_),
                query_args=qa,
                category=cat_,
                output_format=output_format,
                zotero=zotero,
            )


def _run_journal(args, zotero: ZoteroQuery | None):
    if not args.journal:
        logger.error("--journal is required for journal source")
        raise RuntimeError("--journal required")

    try:
        cfg = journal_configs[args.journal]
    except KeyError:
        logger.error("Journal '%s' not supported. Available: %s", args.journal, list(journal_configs.keys()))
        raise RuntimeError(f"Journal '{args.journal}' not supported") from None

    md_folder = _journal_output_root(args.data_dir, cfg)

    if args.volume and args.issue:
        # Explicit mode
        filter_journal_to_md(
            journal_name=cfg["name"],
            journal_slug=cfg["slug"],
            volume=args.volume,
            issue=args.issue,
            issn=cfg["issn"],
            md_folder=md_folder,
            zotero=zotero,
            year=args.year or None,
            acs_code=cfg.get("acs_code", ""),
            provider=cfg.get("provider", ""),
        )
    else:
        # Auto-discover mode
        filter_journal_auto(
            journal_name=cfg["name"],
            journal_slug=cfg["slug"],
            issn=cfg["issn"],
            acs_code=cfg.get("acs_code", ""),
            md_folder=md_folder,
            zotero=zotero,
            provider=cfg.get("provider", "acs"),
            backfill=args.backfill,
            from_year=args.from_year,
        )

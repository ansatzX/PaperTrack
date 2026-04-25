import time
import os
import argparse
import logging

from .report import filter_arxiv_to_md
from .zotero_query import ZoteroQuery
from .codex import query_args

logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--debug", action="store_true",
                        help="enable debug logging")
    parser.add_argument("--time", default="1949.10",
                        help="time to query (YYYY.MM). Use 1949.10 for current date.", type=str)
    parser.add_argument("--arxiv_folder", default="/home/ansatz/data/obsidian/1/arxiv_datas/",
                        help="place to store arxiv datas", type=str)
    parser.add_argument("--category", default="quant-ph",
                        help="category of arxivs also the folder", type=str)
    parser.add_argument("--output_format", default="category/year/month/day",
                        help="output folder format", type=str)

    args = parser.parse_args()
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
    arxiv_folder = args.arxiv_folder
    categories = args.category
    output_format = args.output_format
    times = args.time

    # ZoteroQuery is constructed once and shared across all categories and
    # time periods. This avoids re-building the DOI+URL index for each
    # category, which would re-fetch the entire Zotero library N times.
    zotero: ZoteroQuery | None = None
    try:
        zotero = ZoteroQuery()
    except ConnectionError as e:
        logger.warning("Zotero not available: %s", e)
    except Exception as e:
        logger.error("Unexpected Zotero error: %s", e)

    for time_str in times.split(","):
        for cat_ in categories.split(","):
            try:
                qa = query_args[cat_]
            except KeyError:
                logger.error("Category '%s' not supported. Available: %s", cat_, list(query_args.keys()))
                raise RuntimeError(f"Category '{cat_}' not supported") from None

            # The sentinel 1949.10 (a date before arXiv existed) means
            # "use the current year and month". This avoids requiring the
            # crontab entry to be updated every month.
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
                md_folder=os.path.join(arxiv_folder, cat_),
                query_args=qa,
                category=cat_,
                output_format=output_format,
                zotero=zotero,
            )

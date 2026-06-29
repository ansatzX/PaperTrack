import re
import time
import logging
from copy import deepcopy
from urllib.parse import urlencode

import feedparser
from bs4 import BeautifulSoup

from .adapter_result import AdapterResult, FETCH_FAILED, PARSE_FAILED, SUCCESS
from .arxiv_entry import ArxivEntry
from .diagnostics import record_fetch_attempt

logger = logging.getLogger(__name__)

SEARCH_URL = "https://arxiv.org/search/advanced?"
MAX_RETRIES = 3
RETRY_BACKOFF = (1, 2, 4)


def _parse_arxiv_result(res) -> ArxivEntry | None:
    """Parse a single <li class='arxiv-result'> element into an ArxivEntry.

    res is already a BeautifulSoup Tag from the outer parse — we operate on
    it directly rather than re-parsing with BeautifulSoup(str(res), 'lxml').
    """

    aso = res

    list_title = aso.find(class_="list-title")
    if not list_title:
        return None
    arxiv_id = list_title.find("a").text.strip()

    title_el = aso.find(class_="title")
    title = title_el.text.strip() if title_el else ""

    authors = []
    authors_el = aso.find(class_="authors")
    if authors_el:
        for a in authors_el.find_all("a"):
            authors.append(a.text.strip())

    abstract = ""
    abstract_full = aso.find(class_="abstract-full")
    if abstract_full:
        abstract = abstract_full.text.strip()

    external_doi = ""
    external_doi_url = ""
    subjects = []

    for tag in aso.find_all(class_="tag"):
        tag_classes = tag.get("class", [])
        # Subject tags have is-link (primary category) or is-grey (cross-list) classes.
        if "is-link" in tag_classes or "is-grey" in tag_classes:
            subjects.append(tag.text.strip())
        # External DOI tags: <span class="tag is-light is-size-7"> with a child <a> link.
        # arXiv updates these after the paper is formally published.
        if "is-light" in tag_classes and "is-size-7" in tag_classes:
            a_tag = tag.find("a")
            if a_tag:
                external_doi = tag.text.strip()
                external_doi_url = a_tag.get("href", "")

    # A single paper can have TWO <p class="comments"> elements:
    # one with "Comments:" (page count, etc.) and one with "Journal ref:".
    # The journal_ref is filled in by arXiv when the paper is published.
    journal_ref = ""
    comments = ""

    for comment_el in aso.find_all(class_="comments"):
        text = comment_el.get_text(" ", strip=True)
        if text.startswith("Journal ref:"):
            journal_ref = text[len("Journal ref:"):].strip()
        elif text.startswith("Comments:"):
            comments = text[len("Comments:"):].strip()

    return ArxivEntry(
        arxiv_id=arxiv_id,
        title=title,
        authors=authors,
        abstract=abstract,
        external_doi=external_doi,
        external_doi_url=external_doi_url,
        journal_ref=journal_ref,
        comments=comments,
        subjects=subjects,
    )


def query_arxiv_result(date_from_date: str = "2025-02-01",
                       date_to_date: str = "2025-02-02",
                       query_args: dict | None = None) -> AdapterResult[dict[str, ArxivEntry]]:
    """Query arXiv advanced search for papers submitted on a date range.

    Returns an AdapterResult whose entries map arxiv_id to ArxivEntry.

    The query_args dict is deep-copied before mutation to avoid corrupting
    the module-level template dict (quant_ph in codex.py).
    """

    if query_args is None:
        from .codex import quant_ph
        query_args = deepcopy(quant_ph)
    else:
        query_args = deepcopy(query_args)

    query_args["date-from_date"] = date_from_date
    query_args["date-to_date"] = date_to_date
    query_args.pop("start", None)

    page_size = _page_size(query_args)
    result: dict[str, ArxivEntry] = {}
    urls: list[str] = []
    start = 0

    while True:
        url = _advanced_search_url(query_args, start)
        urls.append(url)
        results = _fetch_arxiv_page(url)
        if results is None:
            return AdapterResult(
                entries={},
                outcome=FETCH_FAILED,
                provider="arxiv",
                source="arxiv",
                message=f"arXiv advanced search fetch failed at start={start}",
                metadata={"url": url, "urls": urls, "start": start},
            )

        page_entries = _parse_arxiv_page(results, url)
        if page_entries is None:
            return AdapterResult(
                entries={},
                outcome=PARSE_FAILED,
                provider="arxiv",
                source="arxiv",
                message=f"arXiv advanced search parse failed at start={start}",
                metadata={"url": url, "urls": urls, "start": start},
            )

        before_count = len(result)
        result.update(page_entries)
        page_count = len(page_entries)
        if page_count < page_size:
            break
        if len(result) == before_count:
            logger.warning("arXiv pagination produced no new entries at start=%d; stopping", start)
            break
        start += page_size

    return AdapterResult(
        entries=result,
        outcome=SUCCESS,
        provider="arxiv",
        source="arxiv",
        metadata={"url": urls[0] if urls else "", "urls": urls},
    )


def _advanced_search_url(query_args: dict, start: int = 0) -> str:
    page_args = deepcopy(query_args)
    if start:
        page_args["start"] = str(start)
    url_args = re.sub("%2B", "+", urlencode(page_args))
    return SEARCH_URL + url_args


def _page_size(query_args: dict) -> int:
    try:
        page_size = int(query_args.get("size", 200))
    except (TypeError, ValueError):
        return 200
    return page_size if page_size > 0 else 200


def _fetch_arxiv_page(url: str):
    # arXiv sometimes returns transient errors under load; retry with backoff.
    last_exc = None
    for attempt in range(MAX_RETRIES):
        try:
            return feedparser.parse(url)
        except Exception as e:
            last_exc = e
            if attempt < MAX_RETRIES - 1:
                delay = RETRY_BACKOFF[attempt]
                logger.warning("arXiv fetch attempt %d/3 failed: %s. Retrying in %ds...", attempt + 1, e, delay)
                time.sleep(delay)
    logger.error("arXiv fetch failed after %d attempts: %s", MAX_RETRIES, last_exc)
    record_fetch_attempt({
        "source_role": "authoritative",
        "source": "arxiv",
        "provider": "arxiv",
        "url": url,
        "stage": "advanced_search_fetch",
        "outcome": "fetch_failed",
        "exception": repr(last_exc),
    })
    return None


def _parse_arxiv_page(results, url: str) -> dict[str, ArxivEntry] | None:
    if "feed" not in results or "summary" not in results["feed"]:
        record_fetch_attempt({
            "source_role": "authoritative",
            "source": "arxiv",
            "provider": "arxiv",
            "url": url,
            "stage": "advanced_search_parse",
            "outcome": "parse_failed",
            "message": "feedparser result missing feed summary",
        })
        return None

    result: dict[str, ArxivEntry] = {}
    summary_text = results["feed"]["summary"]
    soup = BeautifulSoup(summary_text, "lxml")
    find_results = soup.find_all(class_="arxiv-result")

    for res in find_results:
        entry = _parse_arxiv_result(res)
        if entry is not None:
            result[entry.arxiv_id] = entry

    return result


def query_arxiv_dict(date_from_date: str = "2025-02-01",
                     date_to_date: str = "2025-02-02",
                     query_args: dict | None = None) -> dict[str, ArxivEntry]:
    return query_arxiv_result(date_from_date, date_to_date, query_args).entries

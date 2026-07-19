import re
import time
import logging
import os
from copy import deepcopy
from dataclasses import dataclass
from urllib.parse import urlencode

import feedparser
from bs4 import BeautifulSoup

from .adapter_result import AdapterResult, FETCH_FAILED, PARSE_FAILED, RATE_LIMITED, SUCCESS
from .arxiv_entry import ArxivEntry
from .diagnostics import record_fetch_attempt

logger = logging.getLogger(__name__)

SEARCH_URL = "https://arxiv.org/search/advanced?"
MAX_RETRIES = 3
RETRY_BACKOFF = (1, 2, 4)
RATE_LIMIT_BACKOFF = (30, 60, 120)
_last_request_at = 0.0


@dataclass
class _PageFetchResult:
    results: object | None
    outcome: str = SUCCESS
    status_code: int | None = None
    message: str = ""


def _parse_arxiv_result(res) -> ArxivEntry | None:
    """Parse a single <li class='arxiv-result'> element into an ArxivEntry.

    res is already a BeautifulSoup Tag from the outer parse — we operate on
    it directly rather than re-parsing with BeautifulSoup(str(res), 'lxml').
    """

    aso = res

    list_title = aso.find(class_="list-title")
    if not list_title:
        return None
    arxiv_link = list_title.find("a")
    if arxiv_link is None:
        return None
    arxiv_id = arxiv_link.text.strip()

    title_el = aso.find(class_="title")
    title = title_el.text.strip() if title_el else ""

    authors = []
    authors_el = aso.find(class_="authors")
    if authors_el:
        for a in authors_el.find_all("a"):
            author = a.text.strip()
            if author:
                authors.append(author)

    abstract = ""
    abstract_full = aso.find(class_="abstract-full")
    if abstract_full:
        abstract = abstract_full.text.strip()

    if not arxiv_id or not title or not authors or not abstract:
        return None

    external_doi = ""
    external_doi_url = ""
    subjects = []

    for tag in aso.find_all(class_="tag"):
        tag_classes = tag.get("class", [])
        # Subject tags have is-link (primary), is-grey (cross-list), or
        # search-hit (the category that matched the current query).
        if any(cls in tag_classes for cls in ("is-link", "is-grey", "search-hit")):
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
        page_fetch = _fetch_arxiv_page(url)
        if page_fetch.results is None:
            return AdapterResult(
                entries={},
                outcome=page_fetch.outcome,
                provider="arxiv",
                source="arxiv",
                message=page_fetch.message or f"arXiv advanced search fetch failed at start={start}",
                metadata={
                    "url": url,
                    "urls": urls,
                    "start": start,
                    "http_status": page_fetch.status_code,
                },
            )

        page_entries = _parse_arxiv_page(page_fetch.results, url)
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


def _fetch_arxiv_page(url: str) -> _PageFetchResult:
    # arXiv sometimes returns transient errors under load; retry with backoff.
    last_exc = None
    last_status = None
    last_outcome = FETCH_FAILED
    last_message = "arXiv advanced search fetch failed"
    for attempt in range(MAX_RETRIES):
        try:
            _throttle_arxiv_request()
            parsed = feedparser.parse(url)
            status = getattr(parsed, "status", None)
            last_status = status
            if status == 429:
                last_outcome = RATE_LIMITED
                last_message = "arXiv advanced search returned HTTP 429"
                if attempt < MAX_RETRIES - 1:
                    delay = _rate_limit_delay(attempt)
                    logger.warning(
                        "arXiv rate limited request attempt %d/%d. Retrying in %ds...",
                        attempt + 1,
                        MAX_RETRIES,
                        delay,
                    )
                    time.sleep(delay)
                continue
            if status is not None and int(status) >= 400:
                last_outcome = FETCH_FAILED
                last_message = f"arXiv advanced search returned HTTP {status}"
                if attempt < MAX_RETRIES - 1:
                    delay = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
                    logger.warning(
                        "arXiv returned HTTP %s on attempt %d/%d. Retrying in %ds...",
                        status,
                        attempt + 1,
                        MAX_RETRIES,
                        delay,
                    )
                    time.sleep(delay)
                continue
            return _PageFetchResult(results=parsed, status_code=status)
        except Exception as e:
            last_exc = e
            last_message = repr(e)
            if attempt < MAX_RETRIES - 1:
                delay = RETRY_BACKOFF[attempt]
                logger.warning("arXiv fetch attempt %d/3 failed: %s. Retrying in %ds...", attempt + 1, e, delay)
                time.sleep(delay)
    logger.error("arXiv fetch failed after %d attempts: %s", MAX_RETRIES, last_message)
    record_fetch_attempt({
        "source_role": "authoritative",
        "source": "arxiv",
        "provider": "arxiv",
        "url": url,
        "stage": "advanced_search_fetch",
        "outcome": last_outcome,
        "http_status": last_status,
        "exception": repr(last_exc),
        "message": last_message,
    })
    return _PageFetchResult(
        results=None,
        outcome=last_outcome,
        status_code=last_status,
        message=last_message,
    )


def _throttle_arxiv_request() -> None:
    delay = _float_env("PAPERTRACK_ARXIV_REQUEST_DELAY", 0.0)
    if delay <= 0:
        return
    global _last_request_at
    now = time.monotonic()
    elapsed = now - _last_request_at
    if _last_request_at and elapsed < delay:
        time.sleep(delay - elapsed)
    _last_request_at = time.monotonic()


def _rate_limit_delay(attempt: int) -> int:
    raw = os.environ.get("PAPERTRACK_ARXIV_RATE_LIMIT_BACKOFF", "").strip()
    if raw:
        try:
            values = [int(item.strip()) for item in raw.split(",") if item.strip()]
            if values:
                return values[min(attempt, len(values) - 1)]
        except ValueError:
            logger.warning("Invalid PAPERTRACK_ARXIV_RATE_LIMIT_BACKOFF=%r; using defaults", raw)
    return RATE_LIMIT_BACKOFF[min(attempt, len(RATE_LIMIT_BACKOFF) - 1)]


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("Invalid %s=%r; using %s", name, raw, default)
        return default


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
        if entry is None:
            missing_field = _missing_arxiv_result_field(res)
            record_fetch_attempt({
                "source_role": "authoritative",
                "source": "arxiv",
                "provider": "arxiv",
                "url": url,
                "stage": "advanced_search_parse",
                "outcome": "parse_failed",
                "field": missing_field,
                "message": f"one arXiv result was missing required field {missing_field}",
            })
            return None
        result[entry.arxiv_id] = entry

    return result


def _missing_arxiv_result_field(res) -> str:
    list_title = res.find(class_="list-title")
    if (
        list_title is None
        or list_title.find("a") is None
        or not list_title.find("a").get_text(strip=True)
    ):
        return "arxiv_id"
    title = res.find(class_="title")
    if title is None or not title.get_text(strip=True):
        return "title"
    authors = res.find(class_="authors")
    if authors is None or not any(
        author.get_text(strip=True) for author in authors.find_all("a")
    ):
        return "authors"
    abstract = res.find(class_="abstract-full")
    if abstract is None or not abstract.get_text(strip=True):
        return "abstract"
    return "unknown"


def query_arxiv_dict(date_from_date: str = "2025-02-01",
                     date_to_date: str = "2025-02-02",
                     query_args: dict | None = None) -> dict[str, ArxivEntry]:
    return query_arxiv_result(date_from_date, date_to_date, query_args).entries

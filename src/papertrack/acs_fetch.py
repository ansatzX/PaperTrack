"""Direct ACS journal TOC scraper with resilient official-page fetching."""

import logging
import re

from bs4 import BeautifulSoup

from .adapter_result import (
    AdapterResult,
    BLOCKED,
    FETCH_FAILED,
    OFFICIAL_EMPTY_SUSPICIOUS,
    SUCCESS,
)
from .diagnostics import record_fetch_attempt
from .http_fetch import (
    CURL_IMPERSONATIONS,
    HttpFetchResult,
    fetch_html_resilient,
    is_blocked_response,
)
from .journal_entry import JournalEntry

logger = logging.getLogger(__name__)

ACS_TOC_URL = "https://pubs.acs.org/toc/{journal_code}/{volume}/{issue}"
ACS_HTTP_CLIENTS = (
    "curl_cffi",
    "cloudscraper",
    "primp",
    "tls_client",
    "pyreqwest_impersonate",
    "requests",
)
ACS_IMPERSONATIONS = CURL_IMPERSONATIONS
ACS_FETCH_ROUNDS = 3
ACS_ROUND_BACKOFF = (2, 5)

# Per-issue cache to avoid re-scraping on repeated runs.
_cache: dict[str, dict[str, JournalEntry]] = {}
_last_fetch_result: HttpFetchResult | None = None


def _is_blocked(html: str) -> bool:
    return is_blocked_response(html)


def _fetch_html(url: str) -> tuple[str, str, int | None]:
    """Fetch ACS HTML through the shared HTTP-only strategy chain."""
    global _last_fetch_result
    _last_fetch_result = fetch_html_resilient(
        url,
        clients=ACS_HTTP_CLIENTS,
        curl_impersonations=ACS_IMPERSONATIONS,
        rounds=ACS_FETCH_ROUNDS,
        backoff=ACS_ROUND_BACKOFF,
        log_label="ACS",
    )
    return _last_fetch_result.html, _last_fetch_result.outcome, _last_fetch_result.status_code


def _parse_acs_toc(html: str, journal_name: str) -> dict[str, JournalEntry]:
    """Parse ACS TOC page HTML into JournalEntry dict keyed by DOI."""
    soup = BeautifulSoup(html, "lxml")
    result: dict[str, JournalEntry] = {}

    for item in soup.select("div.issue-item.clearfix"):
        # Skip non-article items
        text_preview = item.get_text(" ", strip=True)
        if any(skip in text_preview for skip in
               ("Issue Publication Information", "Editorial Masthead", "Issue Editorial")):
            continue

        # --- DOI & Title ---
        title_a = item.select_one("h3.issue-item_title a, h4.issue-item_title a")
        if not title_a:
            continue
        href = title_a.get("href", "")
        doi_match = re.search(r"10\.1021/\S+", href)
        if not doi_match:
            continue
        doi = doi_match.group(0)
        title = title_a.get_text(" ", strip=True)

        # --- Authors ---
        authors = []
        for a_el in item.select("span.hlFld-ContribAuthor"):
            given = a_el.select_one("given-names")
            surname = a_el.select_one("surname")
            g = given.get_text(strip=True) if given else ""
            s = surname.get_text(strip=True) if surname else ""
            name = f"{g} {s}".strip()
            if name:
                authors.append(name)

        # --- Abstract ---
        abstract = ""
        abstract_el = item.select_one("span.hlFld-Abstract")
        if abstract_el:
            abstract = abstract_el.get_text(" ", strip=True)

        # --- TOC Image ---
        toc_image_url = ""
        img = item.select_one("div.issue-item_img img.lazy")
        if img:
            src = img.get("data-src", "")
            if src and not src.startswith("http"):
                src = "https://pubs.acs.org" + src
            toc_image_url = src

        # --- Volume, Issue, Pages, Date from info metadata ---
        volume = ""
        issue = ""
        pages = ""
        date = ""
        info = item.select_one(".issue-item_info")
        if info:
            vol_el = info.select_one(".issue-item_vol-num")
            if vol_el:
                volume = vol_el.get_text(strip=True)
            info_text = info.get_text(" ", strip=True)
            # Issue number: the number right after the volume
            vol_match = re.search(rf"{re.escape(volume)}\s*,?\s*(\d+)", info_text)
            if vol_match:
                issue = vol_match.group(1)
            # Pages
            page_match = re.search(r"(\d+[-\u2013]\d+)", info_text)
            if page_match:
                pages = page_match.group(1)
            # Publication date
            date_match = re.search(r"Publication Date[^:]*:\s*(.+?)$", info_text, re.MULTILINE)
            if not date_match:
                date_match = re.search(r"Publication Date\s*(.+?)$", info_text, re.MULTILINE)
            if not date_match:
                date_match = re.search(r"(?:Web|Print)[^:]*:\s*(.+?)$", info_text, re.MULTILINE)
            if date_match:
                date = _normalize_date(date_match.group(1).strip())

        if doi and title:
            result[doi] = JournalEntry(
                doi=doi,
                title=title,
                authors=authors,
                abstract=abstract,
                journal_name=journal_name,
                volume=volume,
                issue=issue,
                pages=pages,
                date=date,
                toc_image_url=toc_image_url,
            )

    return result


def _normalize_date(raw: str) -> str:
    """Convert various date formats to 'YYYY-MM-DD'.

    Handles: 'March 26, 2026', '(Web) : March 26, 2026', etc.
    """
    from datetime import datetime
    # Strip leading labels like "(Web) :" or "(Print) :"
    cleaned = re.sub(r"^\(?\w*\)?\s*:\s*", "", raw).strip()
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%B %d,%Y", "%b %d,%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(cleaned, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return cleaned


def query_acs_issue_result(journal_code: str, volume: str, issue: str,
                           journal_name: str = "") -> AdapterResult[dict[str, JournalEntry]]:
    """Scrape ACS TOC page for a specific volume/issue.

    Args:
        journal_code: ACS journal code (e.g. 'jctcce' for JCTC).
        volume: Volume number.
        issue: Issue number.
        journal_name: Display name for the journal.

    Returns:
        AdapterResult containing DOI -> JournalEntry mappings.
    """
    cache_key = f"{journal_code}:{volume}:{issue}"
    if cache_key in _cache:
        return AdapterResult(
            entries=_cache[cache_key],
            outcome=SUCCESS,
            provider="acs",
            source="journal",
        )

    url = ACS_TOC_URL.format(journal_code=journal_code, volume=volume, issue=issue)

    logger.info("Scraping ACS TOC: %s", url)
    html, failure_outcome, status_code = _fetch_html(url)
    if not html or failure_outcome:
        outcome = failure_outcome or FETCH_FAILED
        fetch_result = _last_fetch_result
        record_fetch_attempt(
            {
                "source_role": "authoritative",
                "source": "journal",
                "provider": "acs",
                "journal": journal_code,
                "volume": volume,
                "issue": issue,
                "url": url,
                "stage": "issue_page_fetch",
                "outcome": outcome,
                "http_status": status_code,
                "block_markers": ["Just a moment"] if outcome == BLOCKED else [],
                "client": fetch_result.client if fetch_result else "",
                "variant": fetch_result.variant if fetch_result else "",
                "attempt_count": len(fetch_result.history) if fetch_result else 0,
                "fetch_history": fetch_result.history[-20:] if fetch_result else [],
                "message": "ACS issue page could not be fetched",
            },
            html=html or None,
        )
        return AdapterResult(
            entries={},
            outcome=outcome,
            provider="acs",
            source="journal",
            message="ACS issue page could not be fetched",
            metadata={"url": url},
        )

    entries = _parse_acs_toc(html, journal_name)
    if not entries:
        record_fetch_attempt(
            {
                "source_role": "authoritative",
                "source": "journal",
                "provider": "acs",
                "journal": journal_code,
                "volume": volume,
                "issue": issue,
                "url": url,
                "stage": "issue_page_parse",
                "outcome": "official_empty_suspicious",
                "article_count": 0,
            },
            html=html,
        )
        return AdapterResult(
            entries={},
            outcome=OFFICIAL_EMPTY_SUSPICIOUS,
            provider="acs",
            source="journal",
            message="ACS issue page parsed to zero official articles",
            metadata={"url": url},
        )

    _cache[cache_key] = entries
    logger.info("ACS %s v%s i%s: %d articles scraped",
                journal_code, volume, issue, len(entries))
    return AdapterResult(
        entries=entries,
        outcome=SUCCESS,
        provider="acs",
        source="journal",
        metadata={"url": url},
    )


def query_acs_issue(journal_code: str, volume: str, issue: str,
                    journal_name: str = "") -> dict[str, JournalEntry]:
    return query_acs_issue_result(journal_code, volume, issue, journal_name).entries

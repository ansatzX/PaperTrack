"""Direct APS issue scraper for Physical Review journals."""

from __future__ import annotations

from datetime import datetime
import logging
import re
import time

from bs4 import BeautifulSoup
from curl_cffi import requests

from .adapter_result import AdapterResult, BLOCKED, FETCH_FAILED, OFFICIAL_EMPTY_SUSPICIOUS, SUCCESS
from .diagnostics import record_fetch_attempt
from .journal_entry import JournalEntry

logger = logging.getLogger(__name__)

APS_BASE = "https://journals.aps.org"
APS_ISSUE_URL = APS_BASE + "/{journal_slug}/issues/{volume}/{issue}"
APS_IMPERSONATIONS = (
    "firefox",
    "chrome136",
    "safari",
    "chrome131",
    "chrome124",
)
APS_FETCH_ROUNDS = 3
APS_ROUND_BACKOFF = (2, 5)

_cache: dict[str, dict[str, JournalEntry]] = {}


def _is_blocked(html: str) -> bool:
    head = html[:5000]
    return "Just a moment" in head or "cf-chl" in head or "Cloudflare" in head


def _fetch_html(url: str, required_selector: str = "") -> tuple[str, str]:
    """Fetch APS HTML, trying several browser fingerprints."""
    last_html = ""
    last_status = None
    last_error = None
    for round_idx in range(APS_FETCH_ROUNDS):
        for impersonate in APS_IMPERSONATIONS:
            try:
                resp = requests.get(url, impersonate=impersonate, timeout=30, allow_redirects=True)
                last_html = resp.text
                last_status = resp.status_code
                if resp.status_code != 200 or _is_blocked(resp.text):
                    logger.debug("APS %s via %s returned %s/block", url, impersonate, resp.status_code)
                    continue
                if required_selector:
                    soup = BeautifulSoup(resp.text, "lxml")
                    if not soup.select(required_selector):
                        logger.debug("APS %s via %s missing %s", url, impersonate, required_selector)
                        continue
                logger.info("APS fetch succeeded via %s: %s", impersonate, url)
                return resp.text, ""
            except Exception as exc:
                last_error = exc
                logger.debug("APS fetch via %s failed for %s: %s", impersonate, url, exc)
                time.sleep(1)
        if round_idx < APS_FETCH_ROUNDS - 1:
            delay = APS_ROUND_BACKOFF[min(round_idx, len(APS_ROUND_BACKOFF) - 1)]
            logger.info("Retrying APS fetch round %d/%d in %ds: %s",
                        round_idx + 2, APS_FETCH_ROUNDS, delay, url)
            time.sleep(delay)

    if last_html and _is_blocked(last_html):
        return last_html, BLOCKED
    if last_error:
        logger.warning("APS fetch failed for %s: %s", url, last_error)
    else:
        logger.warning("APS fetch failed for %s with status %s", url, last_status)
    return last_html, FETCH_FAILED


def _parse_aps_issue(
    html: str,
    journal_slug: str,
    journal_name: str,
    fallback_volume: str = "",
    fallback_issue: str = "",
) -> dict[str, JournalEntry]:
    """Parse an APS issue page into entries keyed by DOI."""
    soup = BeautifulSoup(html, "lxml")
    entries: dict[str, JournalEntry] = {}

    for card in soup.select(".browse-result-card.issue"):
        title_el = card.select_one('h3.title a[href*="/abstract/10.1103/"]')
        if not title_el:
            continue
        href = title_el.get("href", "")
        doi_match = re.search(r"10\.1103/[^/?#]+", href)
        if not doi_match:
            continue
        doi = doi_match.group(0)
        title = title_el.get_text(" ", strip=True)
        if JournalEntry.is_non_article(doi, title):
            continue

        citation_paragraphs = [
            p.get_text(" ", strip=True)
            for p in card.select(".citation-container p")
            if p.get_text(" ", strip=True)
        ]
        authors = _split_authors(citation_paragraphs[0]) if citation_paragraphs else []
        citation = citation_paragraphs[1] if len(citation_paragraphs) > 1 else ""
        volume, pages, year = _parse_citation(citation)
        date = _parse_published_date(citation) or year
        abstract = _parse_summary(card)

        entries[doi] = JournalEntry(
            doi=doi,
            title=title,
            authors=authors,
            abstract=abstract,
            journal_name=journal_name,
            volume=volume or fallback_volume,
            issue=fallback_issue,
            pages=pages,
            date=date,
        )

    logger.info("APS %s parsed %d issue entries", journal_slug, len(entries))
    return entries


def _split_authors(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []
    text = text.replace(", and ", ", ")
    text = re.sub(r"\s+and\s+", ", ", text)
    return [author.strip() for author in text.split(",") if author.strip()]


def _parse_citation(citation: str) -> tuple[str, str, str]:
    match = re.search(r"\b(\d+)\s*,\s*([A-Za-z0-9.-]+)\s*\((\d{4})\)", citation)
    if not match:
        return "", "", ""
    return match.group(1), match.group(2), match.group(3)


def _parse_published_date(citation: str) -> str:
    match = re.search(r"Published\s+(.+?)(?:$|\s{2,})", citation)
    if not match:
        return ""
    raw = match.group(1).strip()
    for fmt in ("%d %B, %Y", "%d %b, %Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return raw


def _parse_summary(card) -> str:
    summary = card.select_one(".summary-container")
    if summary is None:
        return ""
    paragraphs = [
        p.get_text(" ", strip=True)
        for p in summary.select("p")
        if p.get_text(" ", strip=True)
    ]
    return paragraphs[-1] if paragraphs else ""


def query_aps_issue_result(journal_slug: str, volume: str, issue: str,
                           journal_name: str = "") -> AdapterResult[dict[str, JournalEntry]]:
    """Scrape a specific APS issue page without visiting article pages."""
    cache_key = f"{journal_slug}:{volume}:{issue}"
    if cache_key in _cache:
        return AdapterResult(
            entries=_cache[cache_key],
            outcome=SUCCESS,
            provider="aps",
            source="journal",
        )

    url = APS_ISSUE_URL.format(journal_slug=journal_slug, volume=volume, issue=issue)
    html, failure_outcome = _fetch_html(url, ".browse-result-card.issue")
    if not html or failure_outcome:
        outcome = failure_outcome or FETCH_FAILED
        record_fetch_attempt(
            {
                "source_role": "authoritative",
                "source": "journal",
                "provider": "aps",
                "journal": journal_slug,
                "volume": volume,
                "issue": issue,
                "url": url,
                "stage": "issue_page_fetch",
                "outcome": outcome,
                "message": "APS issue page could not be fetched with required selector",
            },
            html=html or None,
        )
        return AdapterResult(
            entries={},
            outcome=outcome,
            provider="aps",
            source="journal",
            message="APS issue page could not be fetched with required selector",
            metadata={"url": url},
        )

    entries = _parse_aps_issue(html, journal_slug, journal_name, volume, issue)
    if not entries:
        record_fetch_attempt(
            {
                "source_role": "authoritative",
                "source": "journal",
                "provider": "aps",
                "journal": journal_slug,
                "volume": volume,
                "issue": issue,
                "url": url,
                "stage": "issue_page_parse",
                "outcome": "official_empty_suspicious",
                "article_count": 0,
                "selector_counts": {
                    ".browse-result-card.issue": 0,
                    "h3.title a": 0,
                },
            },
            html=html,
        )
        return AdapterResult(
            entries={},
            outcome=OFFICIAL_EMPTY_SUSPICIOUS,
            provider="aps",
            source="journal",
            message="APS issue page parsed to zero official articles",
            metadata={"url": url},
        )

    _cache[cache_key] = entries
    logger.info("APS %s v%s i%s: %d articles scraped",
                journal_slug, volume, issue, len(entries))
    return AdapterResult(
        entries=entries,
        outcome=SUCCESS,
        provider="aps",
        source="journal",
        metadata={"url": url},
    )


def query_aps_issue(journal_slug: str, volume: str, issue: str,
                    journal_name: str = "") -> dict[str, JournalEntry]:
    return query_aps_issue_result(journal_slug, volume, issue, journal_name).entries

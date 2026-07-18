"""Direct APS issue scraper for Physical Review journals."""

from __future__ import annotations

from datetime import datetime
import json
import logging
import re
import subprocess
import sys

from bs4 import BeautifulSoup

from .adapter_result import AdapterResult, BLOCKED, FETCH_FAILED, OFFICIAL_EMPTY_SUSPICIOUS, SUCCESS
from .diagnostics import record_fetch_attempt
from .http_fetch import (
    HttpRequestPolicy,
    configured_interval,
    fetch_html_resilient,
    is_blocked_response,
    provider_throttle,
)
from .journal_entry import JournalEntry

logger = logging.getLogger(__name__)

APS_BASE = "https://journals.aps.org"
APS_ISSUE_URL = APS_BASE + "/{journal_slug}/issues/{volume}/{issue}"
APS_FETCH_STRATEGIES = (
    ("curl_cffi", "chrome136"),
    ("requests", "default"),
    ("requests", "default"),
)
APS_REQUEST_POLICY = HttpRequestPolicy(
    min_interval=configured_interval("PAPERTRACK_APS_ISSUE_INTERVAL", 30),
    max_attempts=3,
    backoff=(2.0, 5.0),
)

_cache: dict[str, dict[str, JournalEntry]] = {}

APS_BROWSER_TIMEOUT = 60


def _fetch_aps_browser(url: str) -> dict:
    provider_throttle("aps", APS_REQUEST_POLICY.min_interval)
    cmd = ["xvfb-run", "-a", sys.executable, "-m", "papertrack.aps_browser", url]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=APS_BROWSER_TIMEOUT)
        if proc.returncode != 0:
            return {"outcome": FETCH_FAILED, "message": proc.stderr or "browser helper failed", "html": ""}
        return json.loads(proc.stdout or "{}")
    except Exception as exc:
        return {"outcome": FETCH_FAILED, "message": str(exc), "html": ""}


def _is_blocked(html: str) -> bool:
    return is_blocked_response(html) or "Cloudflare" in html[:5000]


def _fetch_html(url: str, required_selector: str = "") -> tuple[str, str]:
    """Fetch APS HTML with a provider-wide 30-second request floor."""
    result = fetch_html_resilient(
        url,
        required_selector=required_selector,
        strategies=APS_FETCH_STRATEGIES,
        request_policy=APS_REQUEST_POLICY,
        provider="aps",
        log_label="APS",
    )
    return result.html, result.outcome


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
        title_el = card.select_one('.title a[href*="10.1103/"]')
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
        if outcome == BLOCKED:
            browser = _fetch_aps_browser(url)
            record_fetch_attempt({
                "source_role": "authoritative", "source": "journal", "provider": "aps",
                "journal": journal_slug, "volume": volume, "issue": issue, "url": url,
                "stage": "issue_page_browser_fallback", "outcome": browser.get("outcome", FETCH_FAILED),
                "fallback_used": True, "fallback_of": "issue_page_fetch",
                "message": browser.get("message", ""),
            }, html=browser.get("html") or None)
            if browser.get("outcome") == SUCCESS and browser.get("html"):
                html, failure_outcome = browser["html"], ""
            else:
                outcome = browser.get("outcome") or outcome
        if not html or failure_outcome:
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
        soup = BeautifulSoup(html, "lxml")
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
                    ".browse-result-card.issue": len(soup.select(".browse-result-card.issue")),
                    ".title a": len(soup.select('.title a[href*="10.1103/"]')),
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

"""Authoritative AIP issue scraper with bounded official abstract requests."""

from __future__ import annotations

from dataclasses import dataclass, field
from html import unescape
import json
import logging
import re
import subprocess
import sys
import time

from bs4 import BeautifulSoup
from curl_cffi import requests

from .adapter_result import (
    AdapterResult,
    BLOCKED,
    FETCH_FAILED,
    OFFICIAL_EMPTY_SUSPICIOUS,
    RATE_LIMITED,
    SUCCESS,
)
from .diagnostics import record_fetch_attempt
from .http_fetch import HttpFetchResult, is_blocked_response
from .journal_entry import JournalEntry

logger = logging.getLogger(__name__)

AIP_BASE = "https://pubs.aip.org"
AIP_ISSUE_URL = AIP_BASE + "/aip/{journal_slug}/issue/{volume}/{issue}"
AIP_LEGACY_ISSUE_URL = AIP_BASE + "/{journal_slug}/issue/{volume}/{issue}"
AIP_MAX_ATTEMPTS = 3
AIP_BACKOFF = (2.0, 5.0)
AIP_MIN_INTERVAL = 1.0
AIP_IMPERSONATE = "chrome136"


@dataclass(frozen=True)
class AipAbstractResult:
    abstract: str = ""
    outcome: str = ""
    status_code: int | None = None
    message: str = ""
    url: str = ""
    article_id: str = ""


@dataclass
class AipBrowserBundle:
    html: str = ""
    abstracts: dict[str, AipAbstractResult] = field(default_factory=dict)
    outcome: str = ""
    status_code: int | None = None
    message: str = ""


_cache: dict[str, AdapterResult[dict[str, JournalEntry]]] = {}
_abstract_cache: dict[str, AipAbstractResult] = {}
_session = None
_last_request_at: float | None = None


def clear_aip_caches() -> None:
    global _session, _last_request_at
    _cache.clear()
    _abstract_cache.clear()
    _session = None
    _last_request_at = None


def aip_abstract_url(journal_slug: str) -> str:
    return AIP_BASE + f"/aip/{journal_slug}/PlatformArticle/ArticleAbstractAjax"


def _is_blocked(html: str) -> bool:
    return is_blocked_response(html) or "Cloudflare" in html[:5000]


def _get_session():
    global _session
    if _session is None:
        _session = requests.Session()
    return _session


def _wait_for_aip_interval() -> None:
    global _last_request_at
    now = time.monotonic()
    if _last_request_at is not None:
        delay = max(0.0, _last_request_at + AIP_MIN_INTERVAL - now)
        if delay:
            time.sleep(delay)
            now = time.monotonic()
    _last_request_at = now


def _aip_get(url: str, **kwargs):
    _wait_for_aip_interval()
    kwargs.setdefault("impersonate", AIP_IMPERSONATE)
    kwargs.setdefault("timeout", 30)
    kwargs.setdefault("allow_redirects", True)
    return _get_session().get(url, **kwargs)


def _retry_delay(attempt_idx: int) -> float:
    return AIP_BACKOFF[min(attempt_idx, len(AIP_BACKOFF) - 1)]


def _retry_after(response) -> float | None:
    headers = getattr(response, "headers", {}) or {}
    raw = headers.get("Retry-After") or headers.get("retry-after")
    try:
        return max(0.0, float(raw)) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _fetch_issue_http(url: str) -> HttpFetchResult:
    history = []
    last_html = ""
    last_status = None
    last_error = ""
    outcome = FETCH_FAILED
    for attempt_idx in range(AIP_MAX_ATTEMPTS):
        event = {
            "attempt": attempt_idx + 1,
            "client": "curl_cffi",
            "variant": AIP_IMPERSONATE,
        }
        retry_after = None
        try:
            response = _aip_get(url)
            retry_after = _retry_after(response)
            last_html = str(getattr(response, "text", "") or "")
            last_status = int(getattr(response, "status_code", 0) or 0) or None
            event["status_code"] = last_status
            history.append(event)
            if last_status == 403 or _is_blocked(last_html):
                event["blocked"] = True
                outcome = BLOCKED
                break
            if last_status == 200:
                if BeautifulSoup(last_html, "lxml").select(".al-article-items"):
                    return HttpFetchResult(
                        html=last_html,
                        status_code=last_status,
                        client="curl_cffi",
                        variant=AIP_IMPERSONATE,
                        history=history,
                    )
                event["missing_selector"] = ".al-article-items"
                break
            if last_status == 429:
                outcome = RATE_LIMITED
            if last_status not in {408, 425, 429} and not (
                last_status and 500 <= last_status <= 599
            ):
                break
        except Exception as exc:
            last_error = repr(exc)
            event["exception"] = last_error
            history.append(event)
        if attempt_idx + 1 < AIP_MAX_ATTEMPTS:
            delay = retry_after if retry_after is not None else _retry_delay(attempt_idx)
            if retry_after is not None:
                event["retry_after"] = retry_after
            event["retry_delay"] = delay
            time.sleep(delay)
    return HttpFetchResult(
        html=last_html,
        outcome=outcome,
        status_code=last_status,
        client="curl_cffi",
        variant=AIP_IMPERSONATE,
        error=last_error,
        history=history,
    )


def _clean_abstract_html(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for selector in ("h1", "h2", "h3", ".title"):
        for element in soup.select(selector):
            if element.get_text(" ", strip=True).lower() == "abstract":
                element.decompose()
    text = soup.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", unescape(text)).strip()


def _fetch_aip_abstract(
    article_id: str,
    referer_url: str,
    journal_slug: str,
) -> AipAbstractResult:
    """Fetch one official issue-page abstract AJAX response."""
    cache_key = f"{journal_slug}:{article_id}"
    if cache_key in _abstract_cache:
        return _abstract_cache[cache_key]
    url = aip_abstract_url(journal_slug)
    params = {"articleId": article_id, "layAbstract": "False"}
    headers = {"Referer": referer_url, "X-Requested-With": "XMLHttpRequest"}
    result = AipAbstractResult(
        outcome=FETCH_FAILED,
        url=url,
        article_id=article_id,
        message="AIP abstract request did not complete",
    )
    for attempt_idx in range(AIP_MAX_ATTEMPTS):
        retry_after = None
        try:
            response = _aip_get(url, params=params, headers=headers)
            retry_after = _retry_after(response)
            status = int(getattr(response, "status_code", 0) or 0) or None
            text = str(getattr(response, "text", "") or "")
            if status == 403 or _is_blocked(text):
                result = AipAbstractResult(
                    outcome=BLOCKED,
                    status_code=status,
                    message="AIP abstract request was blocked",
                    url=url,
                    article_id=article_id,
                )
                break
            if status == 429:
                result = AipAbstractResult(
                    outcome=RATE_LIMITED,
                    status_code=status,
                    message="AIP abstract request was rate limited",
                    url=url,
                    article_id=article_id,
                )
            elif status == 200:
                try:
                    payload = response.json()
                except Exception as exc:
                    result = AipAbstractResult(
                        outcome=FETCH_FAILED,
                        status_code=status,
                        message=f"invalid AIP abstract JSON: {exc!r}",
                        url=url,
                        article_id=article_id,
                    )
                    break
                abstract = _clean_abstract_html(payload.get("Html", "") or "")
                if payload.get("Success") and abstract:
                    result = AipAbstractResult(
                        abstract=abstract,
                        outcome=SUCCESS,
                        status_code=status,
                        url=url,
                        article_id=article_id,
                    )
                else:
                    result = AipAbstractResult(
                        outcome="official_response_empty",
                        status_code=status,
                        message="AIP abstract response contained no abstract",
                        url=url,
                        article_id=article_id,
                    )
                break
            elif status not in {408, 425} and not (status and 500 <= status <= 599):
                result = AipAbstractResult(
                    outcome=FETCH_FAILED,
                    status_code=status,
                    message=f"AIP abstract request returned HTTP {status}",
                    url=url,
                    article_id=article_id,
                )
                break
        except Exception as exc:
            result = AipAbstractResult(
                outcome=FETCH_FAILED,
                message=repr(exc),
                url=url,
                article_id=article_id,
            )
        if attempt_idx + 1 < AIP_MAX_ATTEMPTS:
            time.sleep(
                retry_after if retry_after is not None else _retry_delay(attempt_idx)
            )
    _abstract_cache[cache_key] = result
    return result


def _parse_aip_toc(
    html: str, journal_name: str
) -> tuple[dict[str, JournalEntry], dict[str, str]]:
    """Parse an AIP issue page into entries and official abstract controls."""
    soup = BeautifulSoup(html, "lxml")
    entries: dict[str, JournalEntry] = {}
    abstract_ids: dict[str, str] = {}
    for item in soup.select(".al-article-items"):
        title_el = item.select_one("h3.item-title a, .item-title a")
        if not title_el:
            continue
        title = title_el.get_text(" ", strip=True)
        article_href = title_el.get("href", "")
        doi = ""
        doi_el = item.select_one('.citation-label a[href*="doi.org/10."]')
        if doi_el:
            doi = re.sub(
                r"^https?://(?:dx\.)?doi\.org/",
                "",
                doi_el.get("href", ""),
            ).strip()
        if not doi:
            doi_element = item.select_one("[data-doi]")
            doi = (doi_element.get("data-doi", "") if doi_element else "").strip()
        if not doi or JournalEntry.is_non_article(doi, title):
            continue
        authors = [
            author.get_text(" ", strip=True)
            for author in item.select(".al-authors-list .wi-fullname")
            if author.get_text(" ", strip=True)
        ]
        toc_image_url = ""
        image = item.select_one(".issue-featured-image img")
        if image:
            toc_image_url = image.get("src", "") or image.get("data-src", "")
        citation_element = item.select_one(".ww-citation-primary")
        citation = citation_element.get_text(" ", strip=True) if citation_element else ""
        volume_value = issue_value = pages = publication_date = ""
        citation_match = re.search(r"\b(\d+)\s*,\s*([\w.-]+)\s*\((\d{4})\)", citation)
        if citation_match:
            volume_value, pages, publication_date = citation_match.groups()
        url_match = re.search(r"/article/(\d+)/(\d+)/([^/]+)/", article_href)
        if url_match:
            volume_value = volume_value or url_match.group(1)
            issue_value = url_match.group(2)
            pages = pages or url_match.group(3)
        entries[doi] = JournalEntry(
            doi=doi,
            title=title,
            authors=authors,
            journal_name=journal_name,
            volume=volume_value,
            issue=issue_value,
            pages=pages,
            date=publication_date,
            toc_image_url=toc_image_url,
        )
        abstract_link = item.select_one(".js-show-abstract[data-articleid]")
        if abstract_link:
            article_id = abstract_link.get("data-articleid", "").strip()
            if article_id:
                abstract_ids[doi] = article_id
    return entries, abstract_ids


def _fetch_aip_browser_bundle(url: str, journal_slug: str) -> AipBrowserBundle:
    """Run one headful Playwright issue session under Xvfb."""
    command = [
        "xvfb-run",
        "-a",
        sys.executable,
        "-m",
        "papertrack.aip_browser",
        url,
        journal_slug,
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except Exception as exc:
        return AipBrowserBundle(outcome=FETCH_FAILED, message=repr(exc))
    if completed.returncode != 0:
        return AipBrowserBundle(
            outcome=FETCH_FAILED,
            message=completed.stderr.strip() or "AIP browser helper failed",
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        return AipBrowserBundle(outcome=FETCH_FAILED, message=repr(exc))
    abstracts = {
        article_id: AipAbstractResult(**value)
        for article_id, value in payload.get("abstracts", {}).items()
    }
    return AipBrowserBundle(
        html=payload.get("html", ""),
        abstracts=abstracts,
        outcome=payload.get("outcome", ""),
        status_code=payload.get("status_code"),
        message=payload.get("message", ""),
    )


def _record_abstract_outcome(
    *,
    journal_slug: str,
    volume: str,
    issue: str,
    doi: str,
    article_id: str,
    issue_url: str,
    result: AipAbstractResult | None,
) -> None:
    outcome = result.outcome if result else "official_control_absent"
    url = result.url if result and result.url else issue_url
    record_fetch_attempt(
        {
            "source_role": "authoritative",
            "source": "journal",
            "provider": "aip",
            "journal": journal_slug,
            "volume": volume,
            "issue": issue,
            "doi": doi,
            "article_id": article_id,
            "field": "abstract",
            "url": url,
            "stage": "abstract_fetch" if result else "issue_page_parse",
            "outcome": outcome,
            "http_status": result.status_code if result else None,
            "message": result.message if result else "no official abstract control",
        }
    )


def query_aip_issue_result(
    journal_slug: str,
    volume: str,
    issue: str,
    journal_name: str = "",
) -> AdapterResult[dict[str, JournalEntry]]:
    """Scrape one AIP issue without requesting any article detail page."""
    cache_key = f"{journal_slug}:{volume}:{issue}"
    if cache_key in _cache:
        return _cache[cache_key]

    canonical_url = AIP_ISSUE_URL.format(
        journal_slug=journal_slug,
        volume=volume,
        issue=issue,
    )
    final_url = canonical_url
    issue_result = _fetch_issue_http(canonical_url)
    if issue_result.status_code == 404:
        final_url = AIP_LEGACY_ISSUE_URL.format(
            journal_slug=journal_slug,
            volume=volume,
            issue=issue,
        )
        issue_result = _fetch_issue_http(final_url)

    browser_bundle = AipBrowserBundle()
    html = issue_result.html if issue_result.ok else ""
    if issue_result.outcome == BLOCKED:
        browser_bundle = _fetch_aip_browser_bundle(canonical_url, journal_slug)
        if browser_bundle.html:
            html = browser_bundle.html
            final_url = canonical_url

    if not html:
        outcome = browser_bundle.outcome or issue_result.outcome or FETCH_FAILED
        record_fetch_attempt(
            {
                "source_role": "authoritative",
                "source": "journal",
                "provider": "aip",
                "journal": journal_slug,
                "volume": volume,
                "issue": issue,
                "url": final_url,
                "stage": "issue_page_fetch",
                "outcome": outcome,
                "http_status": browser_bundle.status_code or issue_result.status_code,
                "attempt_count": len(issue_result.history),
                "fetch_history": issue_result.history,
                "message": browser_bundle.message or "AIP issue page could not be fetched",
            },
            html=issue_result.html or None,
        )
        return AdapterResult(
            entries={},
            outcome=outcome,
            provider="aip",
            source="journal",
            message="AIP issue page could not be fetched",
            metadata={"url": final_url, "fetch_history": issue_result.history},
        )

    entries, abstract_ids = _parse_aip_toc(html, journal_name)
    if not entries:
        selector_counts = {
            ".al-article-items": len(BeautifulSoup(html, "lxml").select(".al-article-items")),
            ".citation-label": len(BeautifulSoup(html, "lxml").select(".citation-label")),
            ".js-show-abstract": len(BeautifulSoup(html, "lxml").select(".js-show-abstract")),
        }
        record_fetch_attempt(
            {
                "source_role": "authoritative",
                "source": "journal",
                "provider": "aip",
                "journal": journal_slug,
                "volume": volume,
                "issue": issue,
                "url": final_url,
                "stage": "issue_page_parse",
                "outcome": OFFICIAL_EMPTY_SUSPICIOUS,
                "article_count": 0,
                "selector_counts": selector_counts,
            },
            html=html,
        )
        return AdapterResult(
            entries={},
            outcome=OFFICIAL_EMPTY_SUSPICIOUS,
            provider="aip",
            source="journal",
            message="AIP issue page parsed to zero official articles",
            metadata={"url": final_url, "selector_counts": selector_counts},
        )

    results_by_id: dict[str, AipAbstractResult] = dict(browser_bundle.abstracts)
    missing_fields = []
    browser_used = bool(browser_bundle.html)
    for doi, entry in entries.items():
        article_id = abstract_ids.get(doi, "")
        if not article_id:
            missing_fields.append({"doi": doi, "field": "abstract", "outcome": "official_control_absent"})
            _record_abstract_outcome(
                journal_slug=journal_slug,
                volume=volume,
                issue=issue,
                doi=doi,
                article_id="",
                issue_url=final_url,
                result=None,
            )
            continue
        if article_id not in results_by_id:
            if browser_used:
                results_by_id[article_id] = AipAbstractResult(
                    outcome="official_response_empty",
                    message="browser session returned no abstract result",
                    url=aip_abstract_url(journal_slug),
                    article_id=article_id,
                )
            else:
                results_by_id[article_id] = _fetch_aip_abstract(
                    article_id,
                    final_url,
                    journal_slug,
                )
        abstract_result = results_by_id[article_id]
        if abstract_result.abstract:
            entry.abstract = abstract_result.abstract
        else:
            missing_fields.append(
                {"doi": doi, "field": "abstract", "outcome": abstract_result.outcome}
            )
            _record_abstract_outcome(
                journal_slug=journal_slug,
                volume=volume,
                issue=issue,
                doi=doi,
                article_id=article_id,
                issue_url=final_url,
                result=abstract_result,
            )

    metadata = {
        "url": final_url,
        "article_count": len(entries),
        "missing_fields": missing_fields,
        "fetch_history": issue_result.history,
        "browser_fallback_used": browser_used,
    }
    result = AdapterResult(
        entries=entries,
        outcome=SUCCESS,
        provider="aip",
        source="journal",
        metadata=metadata,
    )
    _cache[cache_key] = result
    logger.info(
        "AIP %s v%s i%s: %d articles scraped",
        journal_slug,
        volume,
        issue,
        len(entries),
    )
    return result


def query_aip_issue(
    journal_slug: str,
    volume: str,
    issue: str,
    journal_name: str = "",
) -> dict[str, JournalEntry]:
    return query_aip_issue_result(journal_slug, volume, issue, journal_name).entries

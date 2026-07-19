"""Authoritative AIP issue scraper with bounded official abstract requests."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
from html import unescape
import json
import logging
import re
import time
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup
from curl_cffi import requests

from .adapter_result import (
    AdapterResult,
    BLOCKED,
    FETCH_FAILED,
    OFFICIAL_EMPTY_SUSPICIOUS,
    PARSE_FAILED,
    RATE_LIMITED,
    SUCCESS,
)
from .diagnostics import record_fetch_attempt
from .http_fetch import HttpFetchResult, configured_interval, is_blocked_response
from .journal_entry import JournalEntry
from .official_snapshot import (
    SnapshotAttachment,
    SnapshotKey,
    SnapshotValidation,
    html_snapshot_provenance_rejection,
)
from .provider_runtime import CapturedResponse, IssueRequest

logger = logging.getLogger(__name__)

AIP_BASE = "https://pubs.aip.org"
AIP_ISSUE_URL = AIP_BASE + "/aip/{journal_slug}/issue/{volume}/{issue}"
AIP_LEGACY_ISSUE_URL = AIP_BASE + "/{journal_slug}/issue/{volume}/{issue}"
AIP_MAX_ATTEMPTS = 3
AIP_BACKOFF = (2.0, 5.0)
AIP_ISSUE_INTERVAL = configured_interval("PAPERTRACK_AIP_ISSUE_INTERVAL", 30)
AIP_ABSTRACT_INTERVAL = configured_interval("PAPERTRACK_AIP_ABSTRACT_INTERVAL", 10)
AIP_IMPERSONATE = "chrome136"
AIP_ISSUE_SELECTOR = ".al-article-items"
AIP_PARSER_VERSION = "aip-issue-v1"
AIP_ABSTRACT_ENDPOINT = AIP_BASE + "/Handlers/ArticleAbstractHandler.ashx"
_SAFE_ARTICLE_ID = re.compile(r"[A-Za-z0-9_.-]+")


@dataclass(frozen=True)
class AipAbstractResult:
    abstract: str = ""
    outcome: str = ""
    status_code: int | None = None
    message: str = ""
    url: str = ""
    article_id: str = ""


@dataclass
class AipParseResult:
    entries: dict[str, JournalEntry]
    abstract_ids: dict[str, str]
    card_count: int
    excluded_cards: list[dict]
    critical_field_failures: list[dict]

    def __iter__(self):
        """Preserve the staged internal tuple API for legacy parser callers."""
        yield self.entries
        yield self.abstract_ids


_cache: dict[str, AdapterResult[dict[str, JournalEntry]]] = {}
_abstract_cache: dict[str, AipAbstractResult] = {}
_session = None
_last_request_at: dict[str, float] = {}


def clear_aip_caches() -> None:
    global _session
    _cache.clear()
    _abstract_cache.clear()
    _session = None
    _last_request_at.clear()


def aip_abstract_url(journal_slug: str) -> str:
    del journal_slug
    return AIP_ABSTRACT_ENDPOINT


def _is_blocked(html: str) -> bool:
    return is_blocked_response(html) or "Cloudflare" in html[:5000]


def _get_session():
    global _session
    if _session is None:
        _session = requests.Session()
    return _session


def _wait_for_aip_interval(role: str) -> float:
    intervals = {
        "issue": AIP_ISSUE_INTERVAL,
        "abstract": AIP_ABSTRACT_INTERVAL,
    }
    interval = intervals[role]
    now = time.monotonic()
    previous = _last_request_at.get(role)
    delay = 0.0
    if previous is not None:
        delay = max(0.0, previous + interval - now)
        if delay:
            time.sleep(delay)
            now = time.monotonic()
    _last_request_at[role] = now
    return delay


def _aip_get(url: str, *, role: str, **kwargs):
    _wait_for_aip_interval(role)
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
            response = _aip_get(url, role="issue")
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
            response = _aip_get(
                url,
                role="abstract",
                params=params,
                headers=headers,
            )
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
) -> AipParseResult:
    """Parse an AIP issue page into entries and official abstract controls."""
    soup = BeautifulSoup(html, "lxml")
    entries: dict[str, JournalEntry] = {}
    abstract_ids: dict[str, str] = {}
    excluded_cards = []
    critical_field_failures = []
    cards = soup.select(AIP_ISSUE_SELECTOR)
    for card_number, item in enumerate(cards, 1):
        title_el = item.select_one("h3.item-title a, .item-title a")
        if not title_el:
            critical_field_failures.append(
                {"card": card_number, "field": "title"}
            )
            continue
        title = title_el.get_text(" ", strip=True)
        if not title:
            critical_field_failures.append(
                {"card": card_number, "field": "title"}
            )
            continue
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
        if JournalEntry.is_non_article(doi, title):
            excluded_cards.append(
                {"card": card_number, "reason": "known_non_article"}
            )
            continue
        if not doi:
            critical_field_failures.append(
                {"card": card_number, "field": "doi"}
            )
            continue
        if doi in entries:
            critical_field_failures.append(
                {"card": card_number, "field": "duplicate_doi", "doi": doi}
            )
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
    dois_by_article_id: dict[str, list[str]] = {}
    for doi, article_id in abstract_ids.items():
        dois_by_article_id.setdefault(article_id, []).append(doi)
    critical_field_failures.extend(
        {
            "field": "duplicate_article_id",
            "article_id": article_id,
            "dois": sorted(dois),
        }
        for article_id, dois in dois_by_article_id.items()
        if len(dois) > 1
    )
    return AipParseResult(
        entries=entries,
        abstract_ids=abstract_ids,
        card_count=len(cards),
        excluded_cards=excluded_cards,
        critical_field_failures=critical_field_failures,
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


def _membership_dois(entries: dict[str, JournalEntry]) -> tuple[str, ...]:
    return tuple(sorted(doi.strip().lower() for doi in entries))


def _membership_hash(dois: tuple[str, ...]) -> str:
    return hashlib.sha256("\n".join(sorted(dois)).encode("utf-8")).hexdigest()


def _valid_aip_membership(dois: tuple[str, ...]) -> bool:
    return bool(dois) and all(
        re.fullmatch(r"10\.1063/[^\s]+", doi, re.IGNORECASE) for doi in dois
    )


def _diagnostic_context(
    journal_slug: str,
    volume: str,
    issue: str,
    url: str,
    stage: str,
    outcome: str,
    **extra,
) -> dict:
    event = {
        "source_role": "authoritative",
        "source": "journal",
        "provider": "aip",
        "journal": journal_slug,
        "volume": volume,
        "issue": issue,
        "url": url,
        "stage": stage,
        "outcome": outcome,
    }
    event.update(extra)
    return event


def _record_store_load_rejection(
    journal_slug: str, volume: str, issue: str, url: str, load_result
) -> None:
    record_fetch_attempt(
        _diagnostic_context(
            journal_slug,
            volume,
            issue,
            url,
            "snapshot_validation",
            PARSE_FAILED,
            cache_validation="rejected",
            cache_load_status=load_result.status,
            rejection_reason=load_result.reason,
            message=load_result.message,
            cache_snapshot_path=str(load_result.cache_snapshot_path),
        )
    )


def _record_snapshot_rejection(
    journal_slug: str,
    volume: str,
    issue: str,
    url: str,
    snapshot,
    reason: str,
    message: str,
    html: str | None = None,
) -> None:
    record_fetch_attempt(
        _diagnostic_context(
            journal_slug,
            volume,
            issue,
            url,
            "snapshot_validation",
            PARSE_FAILED,
            cache_validation="rejected",
            rejection_reason=reason,
            message=message,
            cache_snapshot_path=str(snapshot.body_path),
            snapshot_canonical_url=snapshot.canonical_url,
            snapshot_final_url=snapshot.final_url,
            snapshot_media_type=snapshot.media_type,
            membership_hash=snapshot.membership_hash,
        ),
        html=html,
    )


def _abstract_result_from_response(
    response: CapturedResponse, article_id: str
) -> AipAbstractResult:
    url = response.url or AIP_ABSTRACT_ENDPOINT
    if response.status_code == 403:
        return AipAbstractResult(
            outcome=BLOCKED,
            status_code=response.status_code,
            message="AIP abstract request was blocked",
            url=url,
            article_id=article_id,
        )
    if response.status_code == 429:
        return AipAbstractResult(
            outcome=RATE_LIMITED,
            status_code=response.status_code,
            message="AIP abstract request was rate limited",
            url=url,
            article_id=article_id,
        )
    if response.status_code != 200:
        return AipAbstractResult(
            outcome=FETCH_FAILED,
            status_code=response.status_code,
            message=f"AIP abstract request returned HTTP {response.status_code}",
            url=url,
            article_id=article_id,
        )
    try:
        payload = json.loads(response.body)
    except (UnicodeError, json.JSONDecodeError, TypeError) as exc:
        return AipAbstractResult(
            outcome=FETCH_FAILED,
            status_code=response.status_code,
            message=f"invalid AIP abstract JSON: {exc!r}",
            url=url,
            article_id=article_id,
        )
    if not isinstance(payload, dict):
        return AipAbstractResult(
            outcome=FETCH_FAILED,
            status_code=response.status_code,
            message="invalid AIP abstract JSON object",
            url=url,
            article_id=article_id,
        )
    abstract = _clean_abstract_html(payload.get("Html", "") or "")
    if payload.get("Success") and abstract:
        return AipAbstractResult(
            abstract=abstract,
            outcome=SUCCESS,
            status_code=response.status_code,
            url=url,
            article_id=article_id,
        )
    return AipAbstractResult(
        outcome="official_response_empty",
        status_code=response.status_code,
        message="AIP abstract response contained no abstract",
        url=url,
        article_id=article_id,
    )


def _captured_article_id(response: CapturedResponse) -> str:
    parsed = urlsplit(response.url)
    if parsed.path != urlsplit(AIP_ABSTRACT_ENDPOINT).path:
        return ""
    values = parse_qs(parsed.query).get("articleId", [])
    if values and _SAFE_ARTICLE_ID.fullmatch(values[0]):
        return values[0]
    try:
        payload = json.loads(response.body)
    except (UnicodeError, json.JSONDecodeError, TypeError):
        return ""
    if isinstance(payload, dict):
        value = str(payload.get("articleId") or payload.get("ArticleId") or "")
        if _SAFE_ARTICLE_ID.fullmatch(value):
            return value
    return ""


def _attachment_name(article_id: str) -> str:
    if not _SAFE_ARTICLE_ID.fullmatch(article_id):
        raise ValueError("AIP article ID requires a safe component")
    return f"abstract-{article_id}.json"


def _snapshot_attachment(
    article_id: str, response: CapturedResponse
) -> SnapshotAttachment:
    body = json.dumps(
        {
            "schema": "papertrack-aip-abstract-response-v1",
            "url": response.url,
            "status_code": response.status_code,
            "media_type": response.media_type,
            "body_base64": base64.b64encode(response.body).decode("ascii"),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    _snapshot_response(body)
    return SnapshotAttachment(
        _attachment_name(article_id), "application/json", body
    )


def _snapshot_response(body: bytes) -> CapturedResponse:
    try:
        payload = json.loads(body)
    except (UnicodeError, json.JSONDecodeError, TypeError) as exc:
        raise ValueError("AIP abstract attachment is not a versioned envelope") from exc
    expected_keys = {
        "schema", "url", "status_code", "media_type", "body_base64"
    }
    if (
        not isinstance(payload, dict)
        or set(payload) != expected_keys
        or payload.get("schema") != "papertrack-aip-abstract-response-v1"
    ):
        raise ValueError("AIP abstract attachment is not a versioned envelope")
    try:
        response_body = base64.b64decode(payload["body_base64"], validate=True)
        status_code = int(payload["status_code"])
        response_url = str(payload["url"])
        media_type = str(payload["media_type"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid AIP abstract attachment envelope") from exc
    parsed_url = urlsplit(response_url)
    if (
        parsed_url.scheme != "https"
        or parsed_url.netloc != "pubs.aip.org"
        or parsed_url.path != urlsplit(AIP_ABSTRACT_ENDPOINT).path
        or parsed_url.fragment
    ):
        raise ValueError("AIP abstract attachment URL is not official")
    if status_code != 200 and not 400 <= status_code <= 599:
        raise ValueError("AIP abstract attachment status is not approved")
    approved_media_types = (
        {"application/json", "text/html"}
        if status_code == 200
        else None
    )
    normalized_media_type = media_type.partition(";")[0].strip().lower()
    if (
        approved_media_types is not None
        and normalized_media_type not in approved_media_types
    ):
        raise ValueError("AIP abstract attachment media type is not approved")
    return CapturedResponse(
        url=response_url,
        status_code=status_code,
        media_type=media_type,
        body=response_body,
    )


def _result_from_official_material(
    html: str,
    journal_name: str,
    *,
    journal_slug: str,
    volume: str,
    issue: str,
    url: str,
    transport: str,
    abstract_results: dict[str, AipAbstractResult],
    snapshot=None,
    record_missing: bool = True,
):
    parsed = _parse_aip_toc(html, journal_name)
    entries = parsed.entries
    abstract_ids = parsed.abstract_ids
    selector_counts = {
        AIP_ISSUE_SELECTOR: len(BeautifulSoup(html, "lxml").select(AIP_ISSUE_SELECTOR))
    }
    membership_dois = _membership_dois(entries)
    critical_field_failures = list(parsed.critical_field_failures)
    critical_field_failures.extend(
        {"field": "doi_membership", "doi": doi}
        for doi in membership_dois
        if not re.fullmatch(r"10\.1063/[^\s]+", doi, re.IGNORECASE)
    )
    metadata = {
        "url": url,
        "article_count": len(entries),
        "missing_fields": [],
        "official_transport": transport,
        "selector_counts": selector_counts,
        "card_count": parsed.card_count,
        "excluded_card_count": len(parsed.excluded_cards),
        "excluded_cards": parsed.excluded_cards,
        "critical_field_failures": critical_field_failures,
    }
    if snapshot is not None:
        metadata.update(
            snapshot_path=str(snapshot.body_path),
            membership_hash=snapshot.membership_hash,
        )
    if selector_counts[AIP_ISSUE_SELECTOR] < 1:
        record_fetch_attempt(
            _diagnostic_context(
                journal_slug,
                volume,
                issue,
                url,
                "issue_page_parse",
                OFFICIAL_EMPTY_SUSPICIOUS,
                selector_counts=selector_counts,
                card_count=parsed.card_count,
                excluded_card_count=len(parsed.excluded_cards),
                critical_field_failures=critical_field_failures,
            ),
            html=html,
        )
        return AdapterResult(
            entries={},
            outcome=OFFICIAL_EMPTY_SUSPICIOUS,
            provider="aip",
            source="journal",
            message="AIP issue page parsed to zero official articles",
            metadata=metadata,
        ), membership_dois, abstract_ids
    if critical_field_failures or (
        entries and not _valid_aip_membership(membership_dois)
    ):
        record_fetch_attempt(
            _diagnostic_context(
                journal_slug,
                volume,
                issue,
                url,
                "issue_page_parse",
                PARSE_FAILED,
                selector_counts=selector_counts,
                card_count=parsed.card_count,
                excluded_card_count=len(parsed.excluded_cards),
                critical_field_failures=critical_field_failures,
            ),
            html=html,
        )
        return AdapterResult(
            entries={},
            outcome=PARSE_FAILED,
            provider="aip",
            source="journal",
            message="AIP article card is missing a required membership field",
            metadata=metadata,
        ), membership_dois, abstract_ids
    if not entries:
        record_fetch_attempt(
            _diagnostic_context(
                journal_slug,
                volume,
                issue,
                url,
                "issue_page_parse",
                OFFICIAL_EMPTY_SUSPICIOUS,
                selector_counts=selector_counts,
                card_count=parsed.card_count,
                excluded_card_count=len(parsed.excluded_cards),
                critical_field_failures=[],
            ),
            html=html,
        )
        return AdapterResult(
            entries={},
            outcome=OFFICIAL_EMPTY_SUSPICIOUS,
            provider="aip",
            source="journal",
            message="AIP issue page contains no official article cards",
            metadata=metadata,
        ), membership_dois, abstract_ids
    if any(
        article_id and not _SAFE_ARTICLE_ID.fullmatch(article_id)
        for article_id in abstract_ids.values()
    ):
        unsafe_ids = [
            {"doi": doi, "article_id": article_id}
            for doi, article_id in abstract_ids.items()
            if article_id and not _SAFE_ARTICLE_ID.fullmatch(article_id)
        ]
        record_fetch_attempt(
            _diagnostic_context(
                journal_slug,
                volume,
                issue,
                url,
                "issue_page_parse",
                PARSE_FAILED,
                selector_counts=selector_counts,
                card_count=parsed.card_count,
                critical_field_failures=[
                    {"field": "article_id", **item} for item in unsafe_ids
                ],
            ),
            html=html,
        )
        return AdapterResult(
            entries={},
            outcome=PARSE_FAILED,
            provider="aip",
            source="journal",
            message="AIP issue page contains an unsafe article ID",
            metadata=metadata,
        ), membership_dois, abstract_ids

    for doi, entry in entries.items():
        article_id = abstract_ids.get(doi, "")
        abstract_result = abstract_results.get(article_id) if article_id else None
        if abstract_result and abstract_result.abstract:
            entry.abstract = abstract_result.abstract
            continue
        outcome = (
            abstract_result.outcome
            if abstract_result
            else "official_response_missing"
            if article_id
            else "official_control_absent"
        )
        metadata["missing_fields"].append(
            {"doi": doi, "field": "abstract", "outcome": outcome}
        )
        if record_missing:
            _record_abstract_outcome(
                journal_slug=journal_slug,
                volume=volume,
                issue=issue,
                doi=doi,
                article_id=article_id,
                issue_url=url,
                result=abstract_result,
            )
    return AdapterResult(
        entries=entries,
        outcome=SUCCESS,
        provider="aip",
        source="journal",
        metadata=metadata,
    ), membership_dois, abstract_ids


def _load_snapshot_abstracts(snapshot) -> dict[str, AipAbstractResult]:
    results = {}
    for name, path in snapshot.attachment_paths.items():
        match = re.fullmatch(r"abstract-([A-Za-z0-9_.-]+)\.json", name)
        if match is None:
            raise ValueError("unexpected AIP snapshot attachment name")
        article_id = match.group(1)
        response = _snapshot_response(path.read_bytes())
        results[article_id] = _abstract_result_from_response(response, article_id)
    return results


def _query_aip_with_runtime(
    journal_slug: str,
    volume: str,
    issue: str,
    journal_name: str,
    provider_runtime,
) -> AdapterResult[dict[str, JournalEntry]]:
    url = AIP_ISSUE_URL.format(
        journal_slug=journal_slug, volume=volume, issue=issue
    )
    key = SnapshotKey("aip", journal_slug, volume, issue)
    load_result = provider_runtime.snapshot_store.load_result(key)
    snapshot = load_result.snapshot
    if load_result.status == "rejected":
        _record_store_load_rejection(
            journal_slug, volume, issue, url, load_result
        )
    if snapshot is not None:
        rejection = html_snapshot_provenance_rejection(snapshot, url)
        if rejection is not None:
            reason, message = rejection
            _record_snapshot_rejection(
                journal_slug, volume, issue, url, snapshot, reason, message
            )
        else:
            try:
                html = snapshot.body_path.read_text(encoding="utf-8")
                abstracts = _load_snapshot_abstracts(snapshot)
                snapshot_parse = _parse_aip_toc(html, journal_name)
                expected = {
                    value
                    for value in snapshot_parse.abstract_ids.values()
                    if value
                }
                if expected != set(abstracts):
                    raise ValueError(
                        "AIP snapshot attachments do not exactly match controls"
                    )
                cached, membership_dois, _ = _result_from_official_material(
                    html,
                    journal_name,
                    journal_slug=journal_slug,
                    volume=volume,
                    issue=issue,
                    url=url,
                    transport="snapshot",
                    abstract_results=abstracts,
                    snapshot=snapshot,
                )
            except (OSError, UnicodeError, ValueError) as exc:
                _record_snapshot_rejection(
                    journal_slug,
                    volume,
                    issue,
                    url,
                    snapshot,
                    "attachment_parse",
                    str(exc),
                )
            else:
                if cached.outcome != SUCCESS:
                    _record_snapshot_rejection(
                        journal_slug,
                        volume,
                        issue,
                        url,
                        snapshot,
                        "parse",
                        cached.message or "snapshot failed current parser",
                        html,
                    )
                elif _membership_hash(membership_dois) != snapshot.membership_hash:
                    _record_snapshot_rejection(
                        journal_slug,
                        volume,
                        issue,
                        url,
                        snapshot,
                        "membership_hash",
                        "reparsed membership hash does not match snapshot",
                        html,
                    )
                else:
                    if hasattr(provider_runtime, "stats"):
                        provider_runtime.stats.snapshot_hit += 1
                    return cached

    request = IssueRequest(
        canonical_url=url,
        required_selector=AIP_ISSUE_SELECTOR,
        challenge_markers=(
            "Just a moment",
            "cf-chl",
            "Access denied",
            "Cloudflare",
        ),
        capture_url_patterns=(
            re.compile(r"/Handlers/ArticleAbstractHandler\.ashx"),
        ),
    )
    fetched = provider_runtime.fetch_issue(request)
    if fetched.outcome != SUCCESS:
        record_fetch_attempt(
            _diagnostic_context(
                journal_slug,
                volume,
                issue,
                url,
                "issue_page_fetch",
                fetched.outcome,
                http_status=fetched.status_code,
                attempt_count=fetched.attempt_count,
                message=fetched.message,
            ),
            html=fetched.html or None,
        )
        return AdapterResult(
            entries={},
            outcome=fetched.outcome,
            provider="aip",
            source="journal",
            message=fetched.message or "AIP issue page could not be fetched",
            metadata={"url": url, "official_transport": "live"},
        )

    preflight, _, abstract_ids = _result_from_official_material(
        fetched.html,
        journal_name,
        journal_slug=journal_slug,
        volume=volume,
        issue=issue,
        url=url,
        transport="live",
        abstract_results={},
        record_missing=False,
    )
    if preflight.outcome != SUCCESS:
        return preflight

    responses_by_id = {}
    attachments_by_id = {}
    for captured in fetched.captured:
        article_id = _captured_article_id(captured)
        if article_id and article_id in abstract_ids.values():
            try:
                attachment = _snapshot_attachment(article_id, captured)
            except ValueError as exc:
                record_fetch_attempt(
                    _diagnostic_context(
                        journal_slug,
                        volume,
                        issue,
                        url,
                        "abstract_capture_validation",
                        FETCH_FAILED,
                        article_id=article_id,
                        abstract_url=captured.url,
                        http_status=captured.status_code,
                        media_type=captured.media_type,
                        message=str(exc),
                    )
                )
                continue
            responses_by_id[article_id] = _abstract_result_from_response(
                captured, article_id
            )
            attachments_by_id[article_id] = attachment
    response_missing = False
    for article_id in dict.fromkeys(abstract_ids.values()):
        if not article_id or article_id in responses_by_id:
            continue
        try:
            response = provider_runtime.request_same_origin(
                aip_abstract_url(journal_slug),
                "POST",
                {"articleId": article_id, "layAbstract": "False"},
                max(10.0, AIP_ABSTRACT_INTERVAL),
            )
        except Exception as exc:
            response_missing = True
            responses_by_id[article_id] = AipAbstractResult(
                outcome=FETCH_FAILED,
                message=str(exc),
                url=aip_abstract_url(journal_slug),
                article_id=article_id,
            )
            continue
        responses_by_id[article_id] = _abstract_result_from_response(
            response, article_id
        )
        try:
            attachments_by_id[article_id] = _snapshot_attachment(
                article_id, response
            )
        except ValueError:
            response_missing = True

    result, membership_dois, _ = _result_from_official_material(
        fetched.html,
        journal_name,
        journal_slug=journal_slug,
        volume=volume,
        issue=issue,
        url=url,
        transport="live",
        abstract_results=responses_by_id,
    )
    if result.outcome != SUCCESS:
        return result
    expected_attachment_ids = {
        article_id for article_id in abstract_ids.values() if article_id
    }
    if response_missing or set(attachments_by_id) != expected_attachment_ids:
        result.outcome = FETCH_FAILED
        result.message = (
            "AIP issue membership succeeded but an official abstract response "
            "was not captured"
        )
        result.metadata["snapshot_committed"] = False
        return result
    validation = SnapshotValidation(
        selector_counts=result.metadata["selector_counts"],
        membership_dois=membership_dois,
        parser_version=AIP_PARSER_VERSION,
    )
    try:
        stored = provider_runtime.snapshot_store.commit(
            key=key,
            canonical_url=request.canonical_url,
            final_url=fetched.final_url,
            media_type="text/html",
            body=fetched.html.encode("utf-8"),
            validation=validation,
            attachments=tuple(attachments_by_id.values()),
        )
    except Exception as exc:
        record_fetch_attempt(
            _diagnostic_context(
                journal_slug,
                volume,
                issue,
                url,
                "snapshot_commit",
                FETCH_FAILED,
                message=str(exc),
            )
        )
        return AdapterResult(
            entries={},
            outcome=FETCH_FAILED,
            provider="aip",
            source="journal",
            message=f"AIP snapshot commit failed: {exc}",
            metadata=result.metadata,
        )
    result.metadata.update(
        snapshot_path=str(stored.body_path),
        membership_hash=stored.membership_hash,
    )
    if hasattr(provider_runtime, "stats"):
        provider_runtime.stats.live_success += 1
    return result


def query_aip_issue_result(
    journal_slug: str,
    volume: str,
    issue: str,
    journal_name: str = "",
    provider_runtime=None,
) -> AdapterResult[dict[str, JournalEntry]]:
    """Scrape one AIP issue without requesting any article detail page.

    Production ``run_j.sh`` workers always supply the one publisher-scoped
    runtime opened by ``run_provider_rebuild``. The no-runtime branch remains
    staged legacy compatibility, matching the ACS/APS adapter boundary; it is
    not used by the unattended official-issue workflow.
    """
    if provider_runtime is not None:
        return _query_aip_with_runtime(
            journal_slug, volume, issue, journal_name, provider_runtime
        )
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

    html = issue_result.html if issue_result.ok else ""

    if not html:
        outcome = issue_result.outcome or FETCH_FAILED
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
                "http_status": issue_result.status_code,
                "attempt_count": len(issue_result.history),
                "fetch_history": issue_result.history,
                "message": "AIP issue page could not be fetched",
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

    results_by_id: dict[str, AipAbstractResult] = {}
    missing_fields = []
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
        "browser_fallback_used": False,
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

"""Direct APS issue scraper for Physical Review journals."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import logging
import re

from bs4 import BeautifulSoup

from .adapter_result import (
    AdapterResult,
    FETCH_FAILED,
    OFFICIAL_EMPTY_SUSPICIOUS,
    PARSE_FAILED,
    SUCCESS,
)
from .diagnostics import record_fetch_attempt
from .http_fetch import (
    HttpRequestPolicy,
    configured_interval,
    fetch_html_resilient,
    is_blocked_response,
)
from .identity import normalize_doi
from .journal_entry import JournalEntry
from .official_snapshot import (
    SnapshotKey,
    SnapshotValidation,
    html_snapshot_provenance_rejection,
)
from .provider_runtime import IssueRequest

logger = logging.getLogger(__name__)

APS_BASE = "https://journals.aps.org"
APS_ISSUE_URL = APS_BASE + "/{journal_slug}/issues/{volume}/{issue}"
APS_ISSUE_SELECTOR = ".browse-result-card.issue"
APS_PARSER_VERSION = "aps-issue-v1"
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


@dataclass
class ApsParseResult:
    entries: dict[str, JournalEntry]
    issue_card_count: int
    critical_field_failures: list[dict]


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
    return _parse_aps_issue_result(
        html, journal_slug, journal_name, fallback_volume, fallback_issue
    ).entries


def _parse_aps_issue_result(
    html: str,
    journal_slug: str,
    journal_name: str,
    fallback_volume: str = "",
    fallback_issue: str = "",
) -> ApsParseResult:
    """Parse APS issue cards while retaining commit-validation evidence."""
    soup = BeautifulSoup(html, "lxml")
    entries: dict[str, JournalEntry] = {}
    critical_field_failures: list[dict] = []
    cards = soup.select(APS_ISSUE_SELECTOR)

    for card_number, card in enumerate(cards, 1):
        title_el = card.select_one(".title a")
        if not title_el:
            critical_field_failures.append(
                {"card": card_number, "field": "title"}
            )
            continue
        href = title_el.get("href", "")
        doi_match = re.search(r"10\.1103/[^/?#\s]+", href, re.IGNORECASE)
        if not doi_match:
            critical_field_failures.append(
                {"card": card_number, "field": "doi"}
            )
            continue
        doi = doi_match.group(0)
        title = title_el.get_text(" ", strip=True)
        if not title:
            critical_field_failures.append(
                {"card": card_number, "field": "title"}
            )
            continue
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
    return ApsParseResult(
        entries=entries,
        issue_card_count=len(cards),
        critical_field_failures=critical_field_failures,
    )


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


def _membership_dois(entries: dict[str, JournalEntry]) -> tuple[str, ...]:
    return tuple(sorted(entries))


def _membership_hash(dois: tuple[str, ...]) -> str:
    normalized = sorted(normalize_doi(doi) for doi in dois)
    return hashlib.sha256("\n".join(normalized).encode("utf-8")).hexdigest()


def _valid_aps_membership(dois: tuple[str, ...]) -> bool:
    return bool(dois) and all(
        re.fullmatch(r"10\.1103/[^\s]+", doi, re.IGNORECASE) for doi in dois
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
        "provider": "aps",
        "journal": journal_slug,
        "volume": volume,
        "issue": issue,
        "url": url,
        "stage": stage,
        "outcome": outcome,
    }
    event.update(extra)
    return event


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


def _record_store_load_rejection(
    journal_slug: str,
    volume: str,
    issue: str,
    url: str,
    load_result,
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


def _result_from_html(
    html: str,
    journal_name: str,
    *,
    url: str,
    journal_slug: str,
    volume: str,
    issue: str,
    transport: str,
    snapshot=None,
):
    try:
        parsed = _parse_aps_issue_result(
            html, journal_slug, journal_name, volume, issue
        )
    except Exception as exc:
        record_fetch_attempt(
            _diagnostic_context(
                journal_slug,
                volume,
                issue,
                url,
                "issue_page_parse",
                PARSE_FAILED,
                message=str(exc),
            ),
            html=html,
        )
        return (
            AdapterResult(
                entries={},
                outcome=PARSE_FAILED,
                provider="aps",
                source="journal",
                message=f"APS issue page parser failed: {exc}",
                metadata={"url": url, "official_transport": transport},
            ),
            None,
            None,
        )

    selector_counts = {APS_ISSUE_SELECTOR: parsed.issue_card_count}
    membership_dois = _membership_dois(parsed.entries)
    metadata = {
        "url": url,
        "article_count": len(parsed.entries),
        "issue_card_count": parsed.issue_card_count,
        "critical_field_failures": parsed.critical_field_failures,
        "official_transport": transport,
        "selector_counts": selector_counts,
    }
    if snapshot is not None:
        metadata.update(
            snapshot_path=str(snapshot.body_path),
            membership_hash=snapshot.membership_hash,
        )

    if parsed.issue_card_count < 1:
        record_fetch_attempt(
            _diagnostic_context(
                journal_slug,
                volume,
                issue,
                url,
                "issue_page_parse",
                OFFICIAL_EMPTY_SUSPICIOUS,
                article_count=0,
                selector_counts=selector_counts,
            ),
            html=html,
        )
        return (
            AdapterResult(
                entries={},
                outcome=OFFICIAL_EMPTY_SUSPICIOUS,
                provider="aps",
                source="journal",
                message="APS issue page contains no official issue cards",
                metadata=metadata,
            ),
            parsed,
            membership_dois,
        )

    if parsed.critical_field_failures or not _valid_aps_membership(
        membership_dois
    ):
        record_fetch_attempt(
            _diagnostic_context(
                journal_slug,
                volume,
                issue,
                url,
                "issue_page_parse",
                PARSE_FAILED,
                article_count=len(parsed.entries),
                selector_counts=selector_counts,
                critical_field_failures=parsed.critical_field_failures,
            ),
            html=html,
        )
        return (
            AdapterResult(
                entries={},
                outcome=PARSE_FAILED,
                provider="aps",
                source="journal",
                message="APS issue card is missing a valid DOI or title",
                metadata=metadata,
            ),
            parsed,
            membership_dois,
        )

    return (
        AdapterResult(
            entries=parsed.entries,
            outcome=SUCCESS,
            provider="aps",
            source="journal",
            metadata=metadata,
        ),
        parsed,
        membership_dois,
    )


def _query_aps_with_runtime(
    journal_slug: str,
    volume: str,
    issue: str,
    journal_name: str,
    provider_runtime,
    *,
    force_live: bool = False,
):
    url = APS_ISSUE_URL.format(
        journal_slug=journal_slug, volume=volume, issue=issue
    )
    key = SnapshotKey("aps", journal_slug, volume, issue)
    load_result = None
    snapshot = None
    if not force_live:
        load_result = provider_runtime.snapshot_store.load_result(key)
        snapshot = load_result.snapshot
    if load_result is not None and load_result.status == "rejected":
        _record_store_load_rejection(
            journal_slug, volume, issue, url, load_result
        )
    if snapshot is not None:
        provenance_rejection = html_snapshot_provenance_rejection(
            snapshot, url
        )
        if provenance_rejection is not None:
            reason, message = provenance_rejection
            _record_snapshot_rejection(
                journal_slug, volume, issue, url, snapshot, reason, message
            )
        else:
            try:
                html = snapshot.body_path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                _record_snapshot_rejection(
                    journal_slug,
                    volume,
                    issue,
                    url,
                    snapshot,
                    "body_read",
                    str(exc),
                )
            else:
                cached, _, membership_dois = _result_from_html(
                    html,
                    journal_name,
                    url=url,
                    journal_slug=journal_slug,
                    volume=volume,
                    issue=issue,
                    transport="snapshot",
                    snapshot=snapshot,
                )
                if cached.outcome != SUCCESS or membership_dois is None:
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
                elif (
                    _membership_hash(membership_dois)
                    != snapshot.membership_hash
                ):
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
        canonical_url=APS_ISSUE_URL.format(
            journal_slug=journal_slug,
            volume=volume,
            issue=issue,
        ),
        required_selector=APS_ISSUE_SELECTOR,
        challenge_markers=(
            "Just a moment",
            "cf-chl",
            "Access denied",
            "Cloudflare",
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
            provider="aps",
            source="journal",
            message=fetched.message or "APS issue page could not be fetched",
            metadata={
                "url": url,
                "official_transport": "live",
                "canonical_url": fetched.canonical_url,
                "final_url": fetched.final_url,
                "http_status": fetched.status_code,
                "attempt_count": fetched.attempt_count,
            },
        )

    result, _, membership_dois = _result_from_html(
        fetched.html,
        journal_name,
        url=url,
        journal_slug=journal_slug,
        volume=volume,
        issue=issue,
        transport="live",
    )
    if result.outcome != SUCCESS:
        return result

    validation = SnapshotValidation(
        selector_counts=result.metadata["selector_counts"],
        membership_dois=membership_dois,
        parser_version=APS_PARSER_VERSION,
    )
    try:
        stored = provider_runtime.snapshot_store.commit(
            key=key,
            canonical_url=request.canonical_url,
            final_url=fetched.final_url,
            media_type="text/html",
            body=fetched.html.encode("utf-8"),
            validation=validation,
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
            provider="aps",
            source="journal",
            message=f"APS snapshot commit failed: {exc}",
            metadata=result.metadata,
        )
    result.metadata.update(
        snapshot_path=str(stored.body_path),
        membership_hash=stored.membership_hash,
    )
    if hasattr(provider_runtime, "stats"):
        provider_runtime.stats.live_success += 1
    return result


def query_aps_issue_result(journal_slug: str, volume: str, issue: str,
                           journal_name: str = "", provider_runtime=None,
                           *, force_live: bool = False,
                           ) -> AdapterResult[dict[str, JournalEntry]]:
    """Scrape a specific APS issue page without visiting article pages."""
    if provider_runtime is not None:
        return _query_aps_with_runtime(
            journal_slug, volume, issue, journal_name, provider_runtime,
            force_live=force_live,
        )

    cache_key = f"{journal_slug}:{volume}:{issue}"
    if cache_key in _cache:
        return AdapterResult(
            entries=_cache[cache_key],
            outcome=SUCCESS,
            provider="aps",
            source="journal",
        )

    url = APS_ISSUE_URL.format(journal_slug=journal_slug, volume=volume, issue=issue)
    html, failure_outcome = _fetch_html(url, APS_ISSUE_SELECTOR)
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

    result, _, _ = _result_from_html(
        html,
        journal_name,
        url=url,
        journal_slug=journal_slug,
        volume=volume,
        issue=issue,
        transport="http",
    )
    if result.outcome != SUCCESS:
        return result

    _cache[cache_key] = result.entries
    logger.info("APS %s v%s i%s: %d articles scraped",
                journal_slug, volume, issue, len(result.entries))
    return result


def query_aps_issue(journal_slug: str, volume: str, issue: str,
                    journal_name: str = "") -> dict[str, JournalEntry]:
    return query_aps_issue_result(journal_slug, volume, issue, journal_name).entries

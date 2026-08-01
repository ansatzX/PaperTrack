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
from .provider_runtime import IssueRequest, RecentRequest

logger = logging.getLogger(__name__)

APS_BASE = "https://journals.aps.org"
APS_ISSUE_URL = APS_BASE + "/{journal_slug}/issues/{volume}/{issue}"
APS_RECENT_URL = APS_BASE + "/{journal_slug}/recent"
APS_ISSUE_SELECTOR = ".browse-result-card.issue"
APS_RECENT_SELECTOR = ".browse-result-card"
APS_PARSER_VERSION = "aps-issue-v1"
APS_RECENT_PARSER_VERSION = "aps-recent-v1"
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


@dataclass
class ApsRecentParseResult:
    abstracts: dict[str, str]
    has_next_page: bool
    oldest_published_date: str = ""


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
        abstract = _parse_issue_abstract(card)

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


def _parse_issue_abstract(card) -> str:
    """Extract only a full APS abstract embedded in an issue card.

    ``summary-container`` on historical APS issue pages contains a short
    editorial teaser, not the article abstract.  It must not be rendered as an
    abstract or it prevents the DOI-matched recent-page completion path from
    running.  Current APS cards, when they expose a real abstract inline, use
    the same hidden ``.summary`` element as the official recent pages.
    """
    summary = card.select_one(".summary")
    return summary.get_text(" ", strip=True) if summary is not None else ""


def _recent_url(journal_slug: str, page: int) -> str:
    base = APS_RECENT_URL.format(journal_slug=journal_slug)
    return base if page == 1 else f"{base}?page={page}"


def _parse_aps_recent(html: str, page: int) -> ApsRecentParseResult:
    """Return DOI-keyed abstracts from one official APS recent page.

    Recent content is metadata-only: it has no authority over an issue's
    membership, title, authors, or formal volume/issue placement.
    """
    soup = BeautifulSoup(html, "lxml")
    abstracts: dict[str, str] = {}
    published_dates: list[str] = []
    for card in soup.select(APS_RECENT_SELECTOR):
        # The current APS React layout leaves ``h4.title`` empty and puts the
        # article link directly below ``.headline``.  Older APS markup used
        # ``.title a``.  Support both without accepting PDF/navigation links.
        title_el = card.select_one(
            '.title a[href*="/abstract/"], .headline a[href*="/abstract/"]'
        )
        if title_el is None:
            continue
        match = re.search(
            r"10\.1103/[^/?#\s]+", title_el.get("href", ""), re.IGNORECASE
        )
        if match is None:
            continue
        summary = card.select_one(".summary")
        abstract = (
            summary.get_text(" ", strip=True) if summary is not None
            else _parse_summary(card)
        )
        if abstract:
            abstracts[normalize_doi(match.group(0))] = abstract
        published = card.select_one("[data-published-date]")
        if published is not None:
            raw_date = published.get("data-published-date", "")
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_date):
                published_dates.append(raw_date)

    has_next_page = any(
        link.get("href", "").endswith(f"?page={page + 1}")
        for link in soup.select("a[href]")
    )
    # APS has used both a numbered paginator and a rel=next link.
    has_next_page = has_next_page or bool(
        soup.select_one('a[rel="next"], a.next, li.next a')
    )
    return ApsRecentParseResult(
        abstracts=abstracts,
        has_next_page=has_next_page,
        oldest_published_date=min(published_dates, default=""),
    )


def _recent_snapshot_is_valid(snapshot, url: str) -> bool:
    return (
        snapshot is not None
        and snapshot.canonical_url == url
        and snapshot.final_url == url
        and snapshot.media_type.partition(";")[0].strip().lower()
        == "text/html"
        and snapshot.validation.parser_version == APS_RECENT_PARSER_VERSION
    )


def _recent_page(
    journal_slug: str,
    page: int,
    provider_runtime,
) -> tuple[ApsRecentParseResult | None, str]:
    """Load one cached APS recent page, otherwise fetch and cache it."""
    url = _recent_url(journal_slug, page)
    key = SnapshotKey("aps-recent", journal_slug, "recent", str(page))
    load_result = provider_runtime.snapshot_store.load_result(key)
    snapshot = load_result.snapshot
    if _recent_snapshot_is_valid(snapshot, url):
        try:
            parsed = _parse_aps_recent(
                snapshot.body_path.read_text(encoding="utf-8"), page
            )
        except (OSError, UnicodeError, ValueError) as exc:
            record_fetch_attempt(_diagnostic_context(
                journal_slug, "", "", url, "recent_cache_parse", PARSE_FAILED,
                message=str(exc), cache_snapshot_path=str(snapshot.body_path),
            ))
        else:
            return parsed, "snapshot"

    fetched = provider_runtime.fetch_recent(RecentRequest(
        canonical_url=url,
        required_selector=APS_RECENT_SELECTOR,
        challenge_markers=("Just a moment", "cf-chl", "Access denied", "Cloudflare"),
    ))
    if fetched.outcome != SUCCESS:
        record_fetch_attempt(_diagnostic_context(
            journal_slug, "", "", url, "recent_page_fetch", fetched.outcome,
            http_status=fetched.status_code, attempt_count=fetched.attempt_count,
            message=fetched.message,
        ))
        return None, fetched.outcome
    try:
        parsed = _parse_aps_recent(fetched.html, page)
        provider_runtime.snapshot_store.commit(
            key=key,
            canonical_url=url,
            final_url=fetched.final_url,
            media_type="text/html",
            body=fetched.html.encode("utf-8"),
            validation=SnapshotValidation(
                selector_counts={APS_RECENT_SELECTOR: len(parsed.abstracts)},
                membership_dois=tuple(sorted(parsed.abstracts)),
                parser_version=APS_RECENT_PARSER_VERSION,
            ),
        )
    except Exception as exc:
        record_fetch_attempt(_diagnostic_context(
            journal_slug, "", "", url, "recent_cache_commit", FETCH_FAILED,
            message=str(exc),
        ))
        return None, FETCH_FAILED
    return parsed, "live"


def _fill_missing_abstracts_from_recent(
    entries: dict[str, JournalEntry], journal_slug: str, provider_runtime,
) -> dict[str, int | str]:
    """Fill only existing issue members from cached official APS recent pages."""
    missing = {normalize_doi(doi) for doi, entry in entries.items() if not entry.abstract}
    if not missing or provider_runtime is None:
        return {"filled": 0, "remaining": len(missing), "pages": 0}
    by_normalized_doi = {normalize_doi(doi): entry for doi, entry in entries.items()}
    target_dates = sorted(
        entry.date for entry in entries.values()
        if not entry.abstract and re.fullmatch(r"\d{4}-\d{2}-\d{2}", entry.date)
    )
    earliest_target_date = target_dates[0] if target_dates else ""
    filled = pages = 0
    outcome = "success"
    page = 1
    last_page_date = ""
    while True:
        parsed, source = _recent_page(journal_slug, page, provider_runtime)
        if parsed is None:
            outcome = source
            break
        pages += 1
        last_page_date = parsed.oldest_published_date
        for doi, abstract in parsed.abstracts.items():
            if doi in missing:
                by_normalized_doi[doi].abstract = abstract
                missing.remove(doi)
                filled += 1
        # Recent pages are newest first.  Once a complete page reaches the
        # earliest dated article in this formal issue, all later pages are too
        # old to contain the remaining issue DOI(s).  With no reliable issue
        # date, retain the conservative behavior and continue to the end.
        passed_issue_window = (
            bool(earliest_target_date)
            and bool(parsed.oldest_published_date)
            and parsed.oldest_published_date <= earliest_target_date
        )
        if not missing or not parsed.has_next_page or passed_issue_window:
            break
        page += 1
    record_fetch_attempt(_diagnostic_context(
        journal_slug, "", "", APS_RECENT_URL.format(journal_slug=journal_slug),
        "recent_abstract_fill", outcome, filled_abstracts=filled,
        remaining_abstracts=len(missing), pages_consulted=pages,
        issue_search_from_date=earliest_target_date,
        recent_last_page_date=last_page_date,
    ))
    return {
        "filled": filled,
        "remaining": len(missing),
        "pages": pages,
        "outcome": outcome,
        "issue_search_from_date": earliest_target_date,
        "recent_last_page_date": last_page_date,
    }


def enrich_aps_entries_from_recent(
    entries: dict[str, JournalEntry],
    journal_slug: str,
    provider_runtime,
) -> dict[str, int | str]:
    """Second-stage APS abstract completion for already-authoritative entries.

    Call this only after the formal issue stage has committed its snapshot.
    It never performs an issue navigation and returns coverage evidence for the
    caller to attach to its report-generation result.
    """
    coverage = _fill_missing_abstracts_from_recent(
        entries, journal_slug, provider_runtime
    )
    return coverage


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


def _observe_cache_rejection(provider_runtime) -> None:
    if hasattr(provider_runtime, "stats"):
        provider_runtime.stats.cache_rejected += 1


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
    baseline_membership = None
    load_result = provider_runtime.snapshot_store.load_result(key)
    snapshot = load_result.snapshot
    if load_result.status == "rejected":
        _record_store_load_rejection(
            journal_slug, volume, issue, url, load_result
        )
        _observe_cache_rejection(provider_runtime)
    if snapshot is not None:
        provenance_rejection = html_snapshot_provenance_rejection(
            snapshot, url
        )
        if provenance_rejection is not None:
            reason, message = provenance_rejection
            _record_snapshot_rejection(
                journal_slug, volume, issue, url, snapshot, reason, message
            )
            _observe_cache_rejection(provider_runtime)
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
                _observe_cache_rejection(provider_runtime)
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
                    _observe_cache_rejection(provider_runtime)
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
                    _observe_cache_rejection(provider_runtime)
                else:
                    baseline_membership = {
                        normalize_doi(doi) for doi in membership_dois
                    }
                    if not force_live:
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

    live_membership = {normalize_doi(doi) for doi in membership_dois}
    if (
        baseline_membership is not None
        and not baseline_membership.issubset(live_membership)
    ):
        missing = sorted(baseline_membership - live_membership)
        record_fetch_attempt(
            _diagnostic_context(
                journal_slug, volume, issue, url,
                "snapshot_non_regression", FETCH_FAILED,
                missing_membership_dois=missing,
                message="official issue membership regressed from trusted snapshot",
            ),
            html=fetched.html,
        )
        return AdapterResult(
            entries={}, outcome=FETCH_FAILED, provider="aps", source="journal",
            message="APS official issue membership regressed; retry later",
            metadata=result.metadata,
        )

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

"""Direct ACS journal TOC scraper with resilient official-page fetching."""

from dataclasses import dataclass
import hashlib
import logging
import re

from bs4 import BeautifulSoup

from .adapter_result import (
    AdapterResult,
    BLOCKED,
    FETCH_FAILED,
    OFFICIAL_FIELD_ABSENT,
    OFFICIAL_EMPTY_SUSPICIOUS,
    PARSE_FAILED,
    SUCCESS,
)
from .diagnostics import record_fetch_attempt
from .http_fetch import (
    HttpFetchResult,
    HttpRequestPolicy,
    configured_interval,
    fetch_html_resilient,
    is_blocked_response,
)
from .journal_entry import JournalEntry
from .official_snapshot import (
    SnapshotKey,
    SnapshotValidation,
    html_snapshot_provenance_rejection,
)
from .provider_runtime import IssueRequest

logger = logging.getLogger(__name__)

ACS_TOC_URL = "https://pubs.acs.org/toc/{journal_code}/{volume}/{issue}"
ACS_ISSUE_URL = ACS_TOC_URL
ACS_ISSUE_SELECTOR = "div.issue-item.clearfix"
ACS_PARSER_VERSION = "acs-toc-v1"
ACS_FETCH_STRATEGIES = (
    ("curl_cffi", "chrome136"),
    ("requests", "default"),
    ("requests", "default"),
)
ACS_REQUEST_POLICY = HttpRequestPolicy(
    min_interval=configured_interval("PAPERTRACK_ACS_ISSUE_INTERVAL", 30),
    max_attempts=3,
    backoff=(2.0, 5.0),
)

# Per-issue cache to avoid re-scraping on repeated runs.
_cache: dict[str, AdapterResult[dict[str, JournalEntry]]] = {}
_last_fetch_result: HttpFetchResult | None = None

@dataclass
class AcsParseResult:
    entries: dict[str, JournalEntry]
    issue_item_count: int
    missing_fields: list[dict]
    critical_field_failures: list[dict]


def _is_blocked(html: str) -> bool:
    return is_blocked_response(html)


def _fetch_html(url: str) -> tuple[str, str, int | None]:
    """Fetch ACS HTML through the shared HTTP-only strategy chain."""
    global _last_fetch_result
    _last_fetch_result = fetch_html_resilient(
        url,
        strategies=ACS_FETCH_STRATEGIES,
        request_policy=ACS_REQUEST_POLICY,
        provider="acs",
        log_label="ACS",
    )
    return _last_fetch_result.html, _last_fetch_result.outcome, _last_fetch_result.status_code


def _parse_acs_toc_result(html: str, journal_name: str) -> AcsParseResult:
    """Parse ACS TOC page HTML into JournalEntry dict keyed by DOI."""
    soup = BeautifulSoup(html, "lxml")
    result: dict[str, JournalEntry] = {}
    missing_fields: list[dict] = []
    critical_field_failures: list[dict] = []
    issue_item_count = 0

    for card_number, item in enumerate(soup.select("div.issue-item.clearfix"), 1):
        # Skip non-article items
        text_preview = item.get_text(" ", strip=True)
        if any(skip in text_preview for skip in
               ("Issue Publication Information", "Editorial Masthead", "Issue Editorial")):
            continue
        issue_item_count += 1

        # --- DOI & Title ---
        title_a = item.select_one("h3.issue-item_title a, h4.issue-item_title a")
        if not title_a:
            critical_field_failures.append({"card": card_number, "field": "title"})
            continue
        href = title_a.get("href", "")
        doi_match = re.search(r"10\.1021/[^?#\s]+", href)
        if not doi_match:
            critical_field_failures.append({"card": card_number, "field": "doi"})
            continue
        doi = doi_match.group(0)
        title = title_a.get_text(" ", strip=True)
        if not title:
            critical_field_failures.append({"card": card_number, "field": "title"})
            continue

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

        entry = JournalEntry(
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
        result[doi] = entry
        for field_name, value in (
            ("authors", authors),
            ("abstract", abstract),
            ("volume", volume),
            ("issue", issue),
            ("pages", pages),
            ("date", date),
        ):
            if not value:
                missing_fields.append({"doi": doi, "field": field_name})

    return AcsParseResult(
        entries=result,
        issue_item_count=issue_item_count,
        missing_fields=missing_fields,
        critical_field_failures=critical_field_failures,
    )


def _parse_acs_toc(html: str, journal_name: str) -> dict[str, JournalEntry]:
    """Backward-compatible entry-only ACS parser."""
    return _parse_acs_toc_result(html, journal_name).entries


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


def _membership_dois(entries: dict[str, JournalEntry]) -> tuple[str, ...]:
    return tuple(sorted(doi.strip().lower() for doi in entries))


def _membership_hash(dois: tuple[str, ...]) -> str:
    return hashlib.sha256("\n".join(sorted(dois)).encode("utf-8")).hexdigest()


def _selector_counts(html: str) -> dict[str, int]:
    soup = BeautifulSoup(html, "lxml")
    return {ACS_ISSUE_SELECTOR: len(soup.select(ACS_ISSUE_SELECTOR))}


def _valid_acs_membership(dois: tuple[str, ...]) -> bool:
    return bool(dois) and all(
        re.fullmatch(r"10\.1021/[^\s]+", doi, re.IGNORECASE) for doi in dois
    )


def _diagnostic_context(journal_code: str, volume: str, issue: str,
                        url: str, stage: str, outcome: str, **extra) -> dict:
    event = {
        "source_role": "authoritative",
        "source": "journal",
        "provider": "acs",
        "journal": journal_code,
        "volume": volume,
        "issue": issue,
        "url": url,
        "stage": stage,
        "outcome": outcome,
    }
    event.update(extra)
    return event


def _record_snapshot_rejection(
    journal_code: str,
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
            journal_code,
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
    journal_code: str,
    volume: str,
    issue: str,
    url: str,
    load_result,
) -> None:
    record_fetch_attempt(
        _diagnostic_context(
            journal_code,
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


def _result_from_html(html: str, journal_name: str, *, url: str,
                      journal_code: str, volume: str, issue: str,
                      transport: str, snapshot=None):
    try:
        parsed = _parse_acs_toc_result(html, journal_name)
        selector_counts = _selector_counts(html)
    except Exception as exc:
        record_fetch_attempt(
            _diagnostic_context(
                journal_code, volume, issue, url, "issue_page_parse",
                PARSE_FAILED, message=str(exc),
            ),
            html=html,
        )
        return AdapterResult(
            entries={}, outcome=PARSE_FAILED, provider="acs", source="journal",
            message=f"ACS issue page parser failed: {exc}", metadata={"url": url},
        ), None, None

    metadata = {
        "url": url,
        "article_count": len(parsed.entries),
        "issue_item_count": parsed.issue_item_count,
        "missing_fields": parsed.missing_fields,
        "critical_field_failures": parsed.critical_field_failures,
        "official_transport": transport,
        "selector_counts": selector_counts,
    }
    membership_dois = _membership_dois(parsed.entries)
    if snapshot is not None:
        metadata.update(
            snapshot_path=str(snapshot.body_path),
            membership_hash=snapshot.membership_hash,
        )

    if selector_counts[ACS_ISSUE_SELECTOR] < 1 or parsed.issue_item_count < 1:
        record_fetch_attempt(
            _diagnostic_context(
                journal_code, volume, issue, url, "issue_page_parse",
                OFFICIAL_EMPTY_SUSPICIOUS, article_count=0,
            ),
            html=html,
        )
        return AdapterResult(
            entries={}, outcome=OFFICIAL_EMPTY_SUSPICIOUS, provider="acs",
            source="journal",
            message="ACS issue page parsed to zero official articles",
            metadata=metadata,
        ), parsed, membership_dois

    if parsed.critical_field_failures or not _valid_acs_membership(membership_dois):
        record_fetch_attempt(
            _diagnostic_context(
                journal_code, volume, issue, url, "issue_page_parse",
                PARSE_FAILED, article_count=len(parsed.entries),
                critical_field_failures=parsed.critical_field_failures,
            ),
            html=html,
        )
        return AdapterResult(
            entries={}, outcome=PARSE_FAILED, provider="acs", source="journal",
            message="ACS article card is missing a valid DOI or title",
            metadata=metadata,
        ), parsed, membership_dois

    return AdapterResult(
        entries=parsed.entries, outcome=SUCCESS, provider="acs", source="journal",
        metadata=metadata,
    ), parsed, membership_dois


def _query_acs_with_runtime(journal_code: str, volume: str, issue: str,
                            journal_name: str, provider_runtime,
                            *, force_live: bool = False):
    url = ACS_ISSUE_URL.format(
        journal_code=journal_code, volume=volume, issue=issue
    )
    key = SnapshotKey("acs", journal_code, volume, issue)
    load_result = None
    snapshot = None
    if not force_live:
        load_result = provider_runtime.snapshot_store.load_result(key)
        snapshot = load_result.snapshot
    if load_result is not None and load_result.status == "rejected":
        _record_store_load_rejection(
            journal_code, volume, issue, url, load_result
        )
    if snapshot is not None:
        provenance_rejection = html_snapshot_provenance_rejection(
            snapshot, url
        )
        if provenance_rejection is not None:
            reason, message = provenance_rejection
            _record_snapshot_rejection(
                journal_code, volume, issue, url, snapshot, reason, message
            )
        else:
            try:
                html = snapshot.body_path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                _record_snapshot_rejection(
                    journal_code,
                    volume,
                    issue,
                    url,
                    snapshot,
                    "body_read",
                    str(exc),
                )
            else:
                cached, _, membership_dois = _result_from_html(
                    html, journal_name, url=url, journal_code=journal_code,
                    volume=volume, issue=issue, transport="snapshot",
                    snapshot=snapshot,
                )
                if cached.outcome != SUCCESS or membership_dois is None:
                    _record_snapshot_rejection(
                        journal_code,
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
                        journal_code,
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
        canonical_url=ACS_ISSUE_URL.format(
            journal_code=journal_code,
            volume=volume,
            issue=issue,
        ),
        required_selector=ACS_ISSUE_SELECTOR,
        challenge_markers=("Just a moment", "cf-chl", "Access denied"),
    )
    fetched = provider_runtime.fetch_issue(request)
    if fetched.outcome != SUCCESS:
        record_fetch_attempt(
            _diagnostic_context(
                journal_code, volume, issue, url, "issue_page_fetch",
                fetched.outcome, http_status=fetched.status_code,
                attempt_count=fetched.attempt_count, message=fetched.message,
            ),
            html=fetched.html or None,
        )
        return AdapterResult(
            entries={}, outcome=fetched.outcome, provider="acs", source="journal",
            message=fetched.message or "ACS issue page could not be fetched",
            metadata={"url": url, "official_transport": "live"},
        )

    result, parsed, membership_dois = _result_from_html(
        fetched.html, journal_name, url=url, journal_code=journal_code,
        volume=volume, issue=issue, transport="live",
    )
    if result.outcome != SUCCESS:
        return result

    validation = SnapshotValidation(
        selector_counts=result.metadata["selector_counts"],
        membership_dois=membership_dois,
        parser_version=ACS_PARSER_VERSION,
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
                journal_code, volume, issue, url, "snapshot_commit",
                FETCH_FAILED, message=str(exc),
            )
        )
        return AdapterResult(
            entries={}, outcome=FETCH_FAILED, provider="acs", source="journal",
            message=f"ACS snapshot commit failed: {exc}", metadata=result.metadata,
        )
    result.metadata.update(
        snapshot_path=str(stored.body_path),
        membership_hash=stored.membership_hash,
    )
    if hasattr(provider_runtime, "stats"):
        provider_runtime.stats.live_success += 1
    for missing in parsed.missing_fields:
        record_fetch_attempt(
            _diagnostic_context(
                journal_code, volume, issue, url, "issue_page_parse",
                OFFICIAL_FIELD_ABSENT, doi=missing["doi"],
                field=missing["field"],
            )
        )
    return result


def query_acs_issue_result(journal_code: str, volume: str, issue: str,
                           journal_name: str = "", provider_runtime=None,
                           *, force_live: bool = False,
                           ) -> AdapterResult[dict[str, JournalEntry]]:
    """Scrape ACS TOC page for a specific volume/issue.

    Args:
        journal_code: ACS journal code (e.g. 'jctcce' for JCTC).
        volume: Volume number.
        issue: Issue number.
        journal_name: Display name for the journal.

    Returns:
        AdapterResult containing DOI -> JournalEntry mappings.
    """
    if provider_runtime is not None:
        return _query_acs_with_runtime(
            journal_code, volume, issue, journal_name, provider_runtime,
            force_live=force_live,
        )

    cache_key = f"{journal_code}:{volume}:{issue}"
    if cache_key in _cache:
        return _cache[cache_key]

    url = ACS_TOC_URL.format(journal_code=journal_code, volume=volume, issue=issue)

    logger.info("Scraping ACS TOC: %s", url)
    global _last_fetch_result
    _last_fetch_result = None
    html, failure_outcome, status_code = _fetch_html(url)
    if not html or failure_outcome:
        outcome = failure_outcome or FETCH_FAILED
        if not html or failure_outcome:
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

    parsed = _parse_acs_toc_result(html, journal_name)
    metadata = {
        "url": url,
        "article_count": len(parsed.entries),
        "issue_item_count": parsed.issue_item_count,
        "missing_fields": parsed.missing_fields,
        "critical_field_failures": parsed.critical_field_failures,
        "fetch_history": (
            _last_fetch_result.history if _last_fetch_result else []
        ),
    }
    if parsed.critical_field_failures:
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
                "outcome": PARSE_FAILED,
                "article_count": len(parsed.entries),
                "critical_field_failures": parsed.critical_field_failures,
            },
            html=html,
        )
        return AdapterResult(
            entries={},
            outcome=PARSE_FAILED,
            provider="acs",
            source="journal",
            message="ACS article card is missing DOI or title",
            metadata=metadata,
        )

    if not parsed.entries:
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
            metadata=metadata,
        )

    for missing in parsed.missing_fields:
        record_fetch_attempt(
            {
                "source_role": "authoritative",
                "source": "journal",
                "provider": "acs",
                "journal": journal_code,
                "volume": volume,
                "issue": issue,
                "doi": missing["doi"],
                "field": missing["field"],
                "url": url,
                "stage": "issue_page_parse",
                "outcome": OFFICIAL_FIELD_ABSENT,
            }
        )

    logger.info("ACS %s v%s i%s: %d articles scraped",
                journal_code, volume, issue, len(parsed.entries))
    result = AdapterResult(
        entries=parsed.entries,
        outcome=SUCCESS,
        provider="acs",
        source="journal",
        metadata=metadata,
    )
    _cache[cache_key] = result
    return result


def query_acs_issue(journal_code: str, volume: str, issue: str,
                    journal_name: str = "", provider_runtime=None,
                    ) -> dict[str, JournalEntry]:
    return query_acs_issue_result(
        journal_code, volume, issue, journal_name, provider_runtime
    ).entries

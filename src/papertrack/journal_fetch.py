import logging
from copy import deepcopy
import time
from urllib.request import Request, urlopen
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.parse import quote
from http.client import IncompleteRead
import json

from .adapter_result import AdapterResult, FETCH_FAILED, SUCCESS
from .diagnostics import record_fetch_attempt
from .journal_entry import JournalEntry
from .source_policy import SourcePolicy

logger = logging.getLogger(__name__)

CROSSREF_JOURNAL_WORKS = "https://api.crossref.org/journals/{issn}/works"
MAX_RETRIES = 3
RETRY_BACKOFF = (1, 2, 4)

# Per-year cache to avoid re-fetching CrossRef when querying multiple issues
# from the same journal/year.
_cache: dict[str, list[dict]] = {}

_OFFICIAL_ISSUE_URL_TEMPLATES = {
    "acs": "https://pubs.acs.org/toc/{journal}/{volume}/{issue}",
    "aip": "https://pubs.aip.org/aip/{journal}/issue/{volume}/{issue}",
    "aps": "https://journals.aps.org/{journal}/issues/{volume}/{issue}",
}


def _crossref_title(item: dict) -> str:
    titles = item.get("title", [])
    if not titles:
        return ""
    return str(titles[0])


def _crossref_get(url: str) -> dict:
    """GET a CrossRef API URL with retries, return parsed JSON.

    Large per-year responses can drop mid-stream (``IncompleteRead``); those
    transient failures are retried so a partial year is not silently treated
    as a complete one.
    """
    last_exc = None
    for attempt in range(MAX_RETRIES):
        try:
            req = Request(url, headers={"User-Agent": "PaperTrack/0.1 (mailto:ansatzMe@outlook.com)"})
            with urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode())
        except (URLError, json.JSONDecodeError, IncompleteRead, OSError, TimeoutError) as e:
            last_exc = e
            if attempt < MAX_RETRIES - 1:
                delay = RETRY_BACKOFF[attempt]
                logger.warning("CrossRef fetch attempt %d/3 failed: %s. Retrying in %ds...", attempt + 1, e, delay)
                time.sleep(delay)
    logger.error("CrossRef fetch failed after %d attempts: %s", MAX_RETRIES, last_exc)
    return {}


def _fetch_year(issn: str, year: int) -> list[dict]:
    """Fetch all journal articles for a given ISSN and year from CrossRef.

    Results are cached in memory so that querying multiple issues from the
    same journal/year only hits the API once.
    """
    cache_key = f"{issn}:{year}"
    if cache_key in _cache:
        return _cache[cache_key]

    logger.info("Fetching CrossRef data for %s %d...", issn, year)

    items: list[dict] = []
    cursor = "*"
    seen_cursors: set[str] = set()
    complete = True

    while cursor and cursor not in seen_cursors:
        seen_cursors.add(cursor)
        params = {
            "filter": (
                "type:journal-article"
                f",from-pub-date:{year}-01-01,until-pub-date:{year}-12-31"
            ),
            "rows": "1000",
            "cursor": cursor,
            "select": "DOI,title,volume,issue,page,author,abstract,published-print,published-online",
        }
        url = f"{CROSSREF_JOURNAL_WORKS.format(issn=issn)}?{urlencode(params)}"
        data = _crossref_get(url)
        if not isinstance(data.get("message"), dict):
            complete = False
            break
        message = data["message"]
        page_items = message.get("items", [])
        if not page_items:
            break
        items.extend(page_items)
        cursor = message.get("next-cursor", "")

    # Filter out non-articles (issue info, masthead)
    articles = [
        i for i in items
        if not JournalEntry.is_non_article(i.get("DOI", ""), _crossref_title(i))
    ]
    if complete:
        _cache[cache_key] = articles
    logger.info("CrossRef %s %d: %d articles cached", issn, year, len(articles))
    return articles


def _fill_missing_abstracts_from_crossref(entries: dict[str, JournalEntry],
                                          issn: str,
                                          year: int | None = None) -> int:
    """Fill missing abstracts by DOI without changing official issue membership."""
    if year is None:
        for entry in entries.values():
            if entry.date:
                try:
                    year = int(entry.date.split("-", 1)[0])
                    break
                except ValueError:
                    pass
    if year is None:
        year = time.localtime().tm_year

    missing = {doi for doi, entry in entries.items() if not entry.abstract}
    if not missing:
        return 0

    filled = 0
    for item in _fetch_year(issn, year):
        doi = item.get("DOI", "")
        if doi not in missing:
            continue
        abstract = JournalEntry._clean_crossref_abstract(item.get("abstract", "") or "")
        if abstract:
            entries[doi].abstract = abstract
            missing.remove(doi)
            filled += 1
            if not missing:
                return filled
    return filled


def _record_crossref_abstract_fill(
    official: AdapterResult[dict[str, JournalEntry]],
    issn: str,
    year: int | None,
    journal_slug: str,
    volume: str,
    issue: str,
) -> None:
    filled = _fill_missing_abstracts_from_crossref(official.entries, issn, year)
    if not filled:
        return
    record_fetch_attempt({
        "source_role": "third_party_metadata",
        "source": "journal",
        "provider": "crossref",
        "journal": journal_slug,
        "volume": volume,
        "issue": issue,
        "stage": "abstract_fill",
        "outcome": "success",
        "fallback_used": ["crossref_abstract_fill"],
        "filled_abstracts": filled,
    })
    official.metadata["crossref_filled_abstracts"] = filled


def _official_issue_url(
    provider: str,
    journal: str,
    volume: str,
    issue: str,
    *,
    publisher_configured: bool = True,
) -> str:
    template = _OFFICIAL_ISSUE_URL_TEMPLATES.get(provider, "")
    if not template or not journal or not publisher_configured:
        authority = quote(provider or "journal", safe="._-")
        identifier = quote(journal or "unknown", safe="._-")
        return f"unconfigured://{authority}/{identifier}/{volume}/{issue}"
    return template.format(journal=journal, volume=volume, issue=issue)


def _official_fetch_failed(
    provider: str,
    journal: str,
    message: str,
    *,
    volume: str,
    issue: str,
    url: str,
    issn: str,
    journal_name: str,
    exception: BaseException | None = None,
    status: int | None = None,
) -> AdapterResult[dict[str, JournalEntry]]:
    failure_message = message if exception is None else f"{type(exception).__name__}: {message}"
    record_fetch_attempt({
        "source_role": "authoritative",
        "source": "journal",
        "provider": provider,
        "journal": journal,
        "volume": volume,
        "issue": issue,
        "url": url,
        "stage": "issue_fetch",
        "outcome": FETCH_FAILED,
        "status": status,
        "status_code": status,
        "exception": failure_message,
        "message": failure_message,
        "field": "issue_membership",
        "context": {
            "issn": issn,
            "journal_name": journal_name,
            "adapter": provider,
        },
    })
    return AdapterResult(
        entries={},
        outcome=FETCH_FAILED,
        provider=provider,
        source="journal",
        message=f"{journal}: {failure_message}",
    )


def _pubmed_abbrev(journal_slug: str) -> str:
    """Return the PubMed journal abbreviation for a journal slug."""
    if not journal_slug:
        return ""
    try:
        from . import codex
        for cfg in codex.get_journals().values():
            if cfg.get("slug") == journal_slug:
                return str(cfg.get("pubmed_abbrev", "") or "")
    except Exception:
        logger.exception("failed to look up PubMed abbreviation for %s", journal_slug)
    return ""


def _pubmed_issue_result(journal_slug: str, journal_name: str, volume: str, issue: str,
                         ) -> AdapterResult[dict[str, JournalEntry]]:
    """Query the PubMed issue search path for a volume/issue."""
    abbrev = _pubmed_abbrev(journal_slug)
    if not abbrev:
        return AdapterResult(
            entries={},
            outcome=FETCH_FAILED,
            provider="pubmed",
            source="journal",
            source_role="third_party_issue",
            message="pubmed: no abbreviation configured",
        )
    try:
        from .pubmed_fetch import query_pubmed_issue_result
        return query_pubmed_issue_result(abbrev, volume, issue, journal_name)
    except Exception as exc:
        logger.exception("PubMed fallback failed for %s %s/%s", journal_slug, volume, issue)
        return AdapterResult(
            entries={},
            outcome=FETCH_FAILED,
            provider="pubmed",
            source="journal",
            source_role="third_party_issue",
            message=f"pubmed: {type(exc).__name__}: {exc}",
        )


def _crossref_issue_result(issn: str, volume: str, issue: str,
                           journal_name: str = "", year: int | None = None,
                           ) -> dict[str, JournalEntry]:
    """Last-resort CrossRef membership: articles matching volume/issue for a year."""
    if not issn:
        return {}
    if year is None:
        year = time.localtime().tm_year
    try:
        articles = _fetch_year(issn, year)
    except Exception:
        logger.exception("CrossRef fallback fetch failed for %s %s/%s", issn, volume, issue)
        return {}
    entries: dict[str, JournalEntry] = {}
    for item in articles:
        if str(item.get("volume", "")) == str(volume) and str(item.get("issue", "")) == str(issue):
            entry = JournalEntry.from_crossref(item, journal_name)
            if entry.doi:
                entries[entry.doi] = entry
    return entries


def _issue_fallback(official: AdapterResult[dict[str, JournalEntry]], *,
                    provider: str, journal_slug: str, journal_name: str,
                    issn: str, volume: str, issue: str, year: int | None,
                    run_fallback: bool,
                    ) -> AdapterResult[dict[str, JournalEntry]]:
    """Combine third-party records and enrich official entries by exact DOI.

    The chain applies to the live HTTP path only (``run_fallback`` true, i.e.
    no caller-supplied provider_runtime).  The snapshot-based rebuild path
    leaves the official failure untouched so it can preserve prior valid
    reports instead of pulling third-party fallback data.
    """
    if not run_fallback:
        return official
    from .metadata_sources import europe_pmc_issue, merge_entries, stamp, enrich_abstracts

    authoritative = bool(official.entries)
    entries = deepcopy(official.entries)
    for entry in entries.values():
        stamp(entry, "official")
    # A complete official result needs no third-party calls.
    unavailable = set(official.metadata.get("abstract_unavailable_dois", []))
    if authoritative and all((e.abstract or e.doi in unavailable) and e.authors for e in entries.values()):
        result = deepcopy(official)
        result.entries = entries
        return result

    if year is None and authoritative:
        for entry in entries.values():
            try:
                year = int(entry.date.split("-", 1)[0])
                break
            except ValueError:
                continue
    conflicts = []
    sources = []
    # Fail each independent source softly; a malformed response must never
    # discard an already fetched official inventory.
    loaders = (
        ("europe_pmc", lambda: europe_pmc_issue(issn, volume, issue, journal_name)),
        ("crossref", lambda: _crossref_issue_result(issn, volume, issue, journal_name, year)),
        ("pubmed", lambda: _pubmed_issue_result(journal_slug, journal_name, volume, issue).entries),
    )
    for source, loader in loaders:
        try:
            candidates = loader()
        except Exception as exc:
            candidates = {}
            record_fetch_attempt({"source": "journal", "provider": source, "stage": "metadata_parse",
                                  "outcome": FETCH_FAILED, "exception": type(exc).__name__,
                                  "journal": journal_slug, "volume": volume, "issue": issue})
        before = len(entries)
        conflicts.extend(merge_entries(entries, candidates, source, allow_new=not authoritative,
                                       volume=volume, issue=issue))
        if any(source in e.membership_sources for e in entries.values()):
            sources.append(source)
        record_fetch_attempt({"source": "journal", "source_role": "third_party_metadata" if authoritative else "third_party_issue",
                              "provider": source, "journal": journal_slug, "volume": volume, "issue": issue,
                              "stage": "issue_merge", "outcome": SUCCESS if candidates else FETCH_FAILED,
                              "articles": len(candidates), "added": len(entries) - before})
    if not entries:
        return official
    try:
        enrich_abstracts(entries)
    except Exception as exc:
        record_fetch_attempt({"source": "journal", "provider": "abstract_enrichment",
                              "stage": "metadata_parse", "outcome": FETCH_FAILED,
                              "exception": type(exc).__name__})
    record_fetch_attempt({"source": "journal", "provider": "metadata_merge", "source_role": "third_party_metadata",
                          "journal": journal_slug, "volume": volume, "issue": issue, "stage": "merge_summary",
                          "outcome": SUCCESS, "field_conflicts": conflicts,
                          "missing_abstract_dois": [e.doi for e in entries.values() if not e.abstract]})
    metadata = dict(official.metadata) if authoritative else {}
    metadata.update({"source": "official" if authoritative else (sources[0] if len(sources) == 1 else "third_party"),
                     "sources": sources, "membership_authoritative": authoritative,
                     "field_conflicts": conflicts})
    if not authoritative:
        metadata["fallback_for"] = provider
    return AdapterResult(entries=entries, outcome=official.outcome if authoritative else SUCCESS,
                         provider=official.provider if authoritative else metadata["source"], source="journal",
                         source_role="authoritative" if authoritative else "third_party_issue",
                         message=f"{len(entries)} articles; metadata sources: {', '.join(sources)}", metadata=metadata)


def query_journal_issue_full_result(issn: str, volume: str, issue: str,
                                    journal_name: str = "", year: int | None = None,
                                    acs_code: str = "", provider: str = "",
                                    journal_slug: str = "",
                                    source_policy: SourcePolicy | None = None,
                                    provider_runtime=None,
                                    browser_snapshot: str = "",
                                    ) -> AdapterResult[dict[str, JournalEntry]]:
    """Fetch articles for a journal volume/issue.

    Official issue membership wins. An explicit connected-browser capture
    replaces HTTP for that issue. Third-party indexes complement missing fields
    by exact DOI, or combine provisional membership when official access fails.
    Caller-supplied provider_runtime remains an offline-only replay path.
    """
    if browser_snapshot:
        if provider_runtime is not None:
            raise ValueError("Browser captures cannot be mixed with offline provider replay")
        from .browser_issue import read_browser_issue
        official = read_browser_issue(browser_snapshot, provider="acs" if acs_code else provider,
                                      code=acs_code or journal_slug, volume=volume, issue=issue,
                                      journal_name=journal_name)
        return _issue_fallback(official, provider=official.provider, journal_slug=journal_slug,
                               journal_name=journal_name, issn=issn, volume=volume, issue=issue,
                               year=year, run_fallback=True)

    if acs_code:
        try:
            from .acs_fetch import query_acs_issue_result
            official = query_acs_issue_result(
                acs_code,
                volume,
                issue,
                journal_name,
                provider_runtime=provider_runtime,
            )
            logger.info("ACS issue outcome: %s; checking metadata gaps", official.outcome)
            return _issue_fallback(
                official,
                provider="acs", journal_slug=journal_slug, journal_name=journal_name,
                issn=issn, volume=volume, issue=issue, year=year,
                run_fallback=provider_runtime is None,
            )
        except ImportError:
            logger.warning("ACS scraper unavailable; falling back to PubMed/CrossRef")
            return _issue_fallback(
                _official_fetch_failed(
                    "acs", journal_slug or journal_name or issn, "ACS scraper unavailable",
                    volume=volume, issue=issue,
                    url=_official_issue_url("acs", acs_code, volume, issue),
                    issn=issn, journal_name=journal_name,
                ),
                provider="acs", journal_slug=journal_slug, journal_name=journal_name,
                issn=issn, volume=volume, issue=issue, year=year,
                run_fallback=provider_runtime is None,
            )
        except Exception as exc:
            logger.exception("ACS scraping failed; falling back to PubMed/CrossRef")
            return _issue_fallback(
                _official_fetch_failed(
                    "acs", journal_slug or journal_name or issn, str(exc),
                    volume=volume, issue=issue,
                    url=_official_issue_url("acs", acs_code, volume, issue),
                    issn=issn, journal_name=journal_name, exception=exc,
                ),
                provider="acs", journal_slug=journal_slug, journal_name=journal_name,
                issn=issn, volume=volume, issue=issue, year=year,
                run_fallback=provider_runtime is None,
            )

    if provider == "aip" and journal_slug:
        try:
            from .aip_fetch import query_aip_issue_result
            official = query_aip_issue_result(
                journal_slug,
                volume,
                issue,
                journal_name,
                provider_runtime=provider_runtime,
            )
            logger.info("AIP issue outcome: %s; checking metadata gaps", official.outcome)
            return _issue_fallback(
                official,
                provider="aip", journal_slug=journal_slug, journal_name=journal_name,
                issn=issn, volume=volume, issue=issue, year=year,
                run_fallback=provider_runtime is None,
            )
        except ImportError:
            logger.warning("AIP scraper unavailable; falling back to PubMed/CrossRef")
            return _issue_fallback(
                _official_fetch_failed(
                    "aip", journal_slug or journal_name or issn, "AIP scraper unavailable",
                    volume=volume, issue=issue,
                    url=_official_issue_url("aip", journal_slug, volume, issue),
                    issn=issn, journal_name=journal_name,
                ),
                provider="aip", journal_slug=journal_slug, journal_name=journal_name,
                issn=issn, volume=volume, issue=issue, year=year,
                run_fallback=provider_runtime is None,
            )
        except Exception as exc:
            logger.exception("AIP scraping failed; falling back to PubMed/CrossRef")
            return _issue_fallback(
                _official_fetch_failed(
                    "aip", journal_slug or journal_name or issn, str(exc),
                    volume=volume, issue=issue,
                    url=_official_issue_url("aip", journal_slug, volume, issue),
                    issn=issn, journal_name=journal_name, exception=exc,
                ),
                provider="aip", journal_slug=journal_slug, journal_name=journal_name,
                issn=issn, volume=volume, issue=issue, year=year,
                run_fallback=provider_runtime is None,
            )

    if provider == "aps" and journal_slug:
        try:
            from .aps_fetch import query_aps_issue_result
            official = query_aps_issue_result(
                journal_slug,
                volume,
                issue,
                journal_name,
                provider_runtime=provider_runtime,
            )
            logger.info("APS issue outcome: %s; checking metadata gaps", official.outcome)
            return _issue_fallback(
                official,
                provider="aps", journal_slug=journal_slug, journal_name=journal_name,
                issn=issn, volume=volume, issue=issue, year=year,
                run_fallback=provider_runtime is None,
            )
        except ImportError:
            logger.warning("APS scraper unavailable; falling back to PubMed/CrossRef")
            return _issue_fallback(
                _official_fetch_failed(
                    "aps", journal_slug or journal_name or issn, "APS scraper unavailable",
                    volume=volume, issue=issue,
                    url=_official_issue_url("aps", journal_slug, volume, issue),
                    issn=issn, journal_name=journal_name,
                ),
                provider="aps", journal_slug=journal_slug, journal_name=journal_name,
                issn=issn, volume=volume, issue=issue, year=year,
                run_fallback=provider_runtime is None,
            )
        except Exception as exc:
            logger.exception("APS scraping failed; falling back to PubMed/CrossRef")
            return _issue_fallback(
                _official_fetch_failed(
                    "aps", journal_slug or journal_name or issn, str(exc),
                    volume=volume, issue=issue,
                    url=_official_issue_url("aps", journal_slug, volume, issue),
                    issn=issn, journal_name=journal_name, exception=exc,
                ),
                provider="aps", journal_slug=journal_slug, journal_name=journal_name,
                issn=issn, volume=volume, issue=issue, year=year,
                run_fallback=provider_runtime is None,
            )

    no_adapter = _official_fetch_failed(
        provider or "journal",
        journal_slug or journal_name or issn,
        "no authoritative issue adapter configured",
        volume=volume,
        issue=issue,
        url=_official_issue_url(
            provider,
            journal_slug or journal_name or issn,
            volume,
            issue,
            publisher_configured=bool(journal_slug),
        ),
        issn=issn,
        journal_name=journal_name,
    )
    return _issue_fallback(
        no_adapter,
        provider=provider or "journal", journal_slug=journal_slug, journal_name=journal_name,
        issn=issn, volume=volume, issue=issue, year=year,
        run_fallback=provider_runtime is None,
    )


def query_journal_issue_full(issn: str, volume: str, issue: str,
                            journal_name: str = "", year: int | None = None,
                            acs_code: str = "", provider: str = "",
                            journal_slug: str = "",
                            source_policy: SourcePolicy | None = None,
                            ) -> dict[str, JournalEntry]:
    return query_journal_issue_full_result(
        issn,
        volume,
        issue,
        journal_name,
        year,
        acs_code,
        provider,
        journal_slug,
        source_policy,
    ).entries


def clear_cache():
    """Clear the CrossRef year-level cache (useful for testing)."""
    _cache.clear()
    try:
        from .acs_fetch import _cache as _acs_cache
        _acs_cache.clear()
    except ImportError:
        pass
    try:
        from .aip_fetch import clear_aip_caches
        clear_aip_caches()
    except ImportError:
        pass
    try:
        from .aps_fetch import _cache as _aps_cache
        _aps_cache.clear()
    except ImportError:
        pass

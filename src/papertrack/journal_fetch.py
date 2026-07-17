import logging
import time
from urllib.request import Request, urlopen
from urllib.error import URLError
from urllib.parse import urlencode
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


def _crossref_title(item: dict) -> str:
    titles = item.get("title", [])
    if not titles:
        return ""
    return str(titles[0])


def _crossref_get(url: str) -> dict:
    """GET a CrossRef API URL with retries, return parsed JSON."""
    last_exc = None
    for attempt in range(MAX_RETRIES):
        try:
            req = Request(url, headers={"User-Agent": "PaperTrack/0.1 (mailto:ansatzMe@outlook.com)"})
            with urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except (URLError, json.JSONDecodeError) as e:
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
        message = data.get("message", {})
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
    _cache[cache_key] = articles
    logger.info("CrossRef %s %d: %d articles cached", issn, year, len(articles))
    return articles


def query_journal_issue(issn: str, volume: str, issue: str,
                        journal_name: str = "", year: int | None = None) -> dict[str, JournalEntry]:
    """Fetch articles for a specific journal volume/issue via CrossRef.

    Args:
        issn: Journal ISSN (e.g. '1549-9618' for JCTC).
        volume: Volume number as string.
        issue: Issue number as string.
        journal_name: Display name for the journal.
        year: Publication year. If None, estimated from current date
              (assumes volume → year mapping is 1:1 starting from journal inception).

    Returns:
        Dict mapping DOI → JournalEntry, sorted by page number.
    """
    if year is None:
        year = time.localtime().tm_year

    all_articles = _fetch_year(issn, year)

    # Filter by volume and issue
    matches: list[JournalEntry] = []
    for item in all_articles:
        item_vol = item.get("volume", "") or ""
        item_iss = item.get("issue", "") or ""
        if str(item_vol) == str(volume) and str(item_iss) == str(issue):
            entry = JournalEntry.from_crossref(item, journal_name)
            matches.append(entry)

    # Sort by page number if available
    def _page_key(entry: JournalEntry) -> int:
        try:
            return int(entry.pages.split("-")[0])
        except (ValueError, AttributeError):
            return 0

    matches.sort(key=_page_key)

    result: dict[str, JournalEntry] = {}
    for entry in matches:
        result[entry.doi] = entry

    logger.info("JOURNAL %s Vol.%s Iss.%s: %d articles matched",
                journal_name or issn, volume, issue, len(result))
    return result


def query_journal_issue_result(issn: str, volume: str, issue: str,
                               journal_name: str = "",
                               year: int | None = None) -> AdapterResult[dict[str, JournalEntry]]:
    entries = query_journal_issue(issn, volume, issue, journal_name, year)
    return AdapterResult(
        entries=entries,
        outcome=SUCCESS,
        provider="crossref",
        source="journal",
        source_role="fallback",
    )


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


def _official_fetch_failed(provider: str, journal: str, message: str) -> AdapterResult[dict[str, JournalEntry]]:
    return AdapterResult(
        entries={},
        outcome=FETCH_FAILED,
        provider=provider,
        source="journal",
        message=f"{journal}: {message}",
    )


def query_journal_issue_full_result(issn: str, volume: str, issue: str,
                                    journal_name: str = "", year: int | None = None,
                                    acs_code: str = "", provider: str = "",
                                    journal_slug: str = "",
                                    source_policy: SourcePolicy | None = None,
                                    ) -> AdapterResult[dict[str, JournalEntry]]:
    """Fetch articles for a journal volume/issue.

    Official journal providers define issue membership. CrossRef may fill
    missing abstracts by DOI, but only after an official issue fetch succeeds.
    """
    policy = source_policy or SourcePolicy.official_only()

    if acs_code:
        try:
            from .acs_fetch import query_acs_issue_result
            official = query_acs_issue_result(acs_code, volume, issue, journal_name)
            if official.entries and policy.crossref_metadata_enabled:
                _record_crossref_abstract_fill(
                    official, issn, year, journal_slug or journal_name or issn, volume, issue
                )
            if official.entries:
                return official
            logger.warning("ACS scraping returned %s; not using CrossRef for issue membership", official.outcome)
            return official
        except ImportError:
            logger.warning("ACS scraper unavailable; not using CrossRef for issue membership")
            return _official_fetch_failed("acs", journal_slug or journal_name or issn, "ACS scraper unavailable")
        except Exception as exc:
            logger.exception("ACS scraping failed; not using CrossRef for issue membership")
            return _official_fetch_failed("acs", journal_slug or journal_name or issn, repr(exc))

    if provider == "aip" and journal_slug:
        try:
            from .aip_fetch import query_aip_issue_result
            official = query_aip_issue_result(journal_slug, volume, issue, journal_name)
            if official.entries and policy.crossref_metadata_enabled:
                _record_crossref_abstract_fill(official, issn, year, journal_slug, volume, issue)
            if official.entries:
                return official
            logger.warning("AIP scraping returned %s; not using CrossRef for issue membership", official.outcome)
            return official
        except ImportError:
            logger.warning("AIP scraper unavailable; not using CrossRef for issue membership")
            return _official_fetch_failed("aip", journal_slug or journal_name or issn, "AIP scraper unavailable")
        except Exception as exc:
            logger.exception("AIP scraping failed; not using CrossRef for issue membership")
            return _official_fetch_failed("aip", journal_slug or journal_name or issn, repr(exc))

    if provider == "aps" and journal_slug:
        try:
            from .aps_fetch import query_aps_issue_result
            official = query_aps_issue_result(journal_slug, volume, issue, journal_name)
            if official.entries and policy.crossref_metadata_enabled:
                _record_crossref_abstract_fill(official, issn, year, journal_slug, volume, issue)
            if official.entries:
                return official
            logger.warning("APS scraping returned %s; not using CrossRef for issue membership", official.outcome)
            return official
        except ImportError:
            logger.warning("APS scraper unavailable; not using CrossRef for issue membership")
            return _official_fetch_failed("aps", journal_slug or journal_name or issn, "APS scraper unavailable")
        except Exception as exc:
            logger.exception("APS scraping failed; not using CrossRef for issue membership")
            return _official_fetch_failed("aps", journal_slug or journal_name or issn, repr(exc))

    return _official_fetch_failed(
        provider or "journal",
        journal_slug or journal_name or issn,
        "no authoritative issue adapter configured",
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
        from .aip_fetch import _cache as _aip_cache
        _aip_cache.clear()
    except ImportError:
        pass
    try:
        from .aps_fetch import _cache as _aps_cache
        _aps_cache.clear()
    except ImportError:
        pass

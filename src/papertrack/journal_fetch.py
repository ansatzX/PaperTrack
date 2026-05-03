import logging
import time
from urllib.request import Request, urlopen
from urllib.error import URLError
from urllib.parse import urlencode
import json

from .journal_entry import JournalEntry

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


def _fill_missing_abstracts_from_crossref(entries: dict[str, JournalEntry],
                                          issn: str,
                                          year: int | None = None) -> None:
    """Fill missing abstracts by DOI without changing AIP issue membership."""
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
        return

    for item in _fetch_year(issn, year):
        doi = item.get("DOI", "")
        if doi not in missing:
            continue
        abstract = JournalEntry._clean_crossref_abstract(item.get("abstract", "") or "")
        if abstract:
            entries[doi].abstract = abstract
            missing.remove(doi)
            if not missing:
                return


def query_journal_issue_full(issn: str, volume: str, issue: str,
                            journal_name: str = "", year: int | None = None,
                            acs_code: str = "", provider: str = "",
                            journal_slug: str = "") -> dict[str, JournalEntry]:
    """Fetch articles for a journal volume/issue, preferring ACS direct scraping.

    Falls back to CrossRef when ACS scraping fails or acs_code is not provided.
    """
    if acs_code:
        try:
            from .acs_fetch import query_acs_issue
            entries = query_acs_issue(acs_code, volume, issue, journal_name)
            if entries:
                return entries
            logger.info("ACS scraping returned empty, falling back to CrossRef")
        except ImportError:
            logger.warning("curl_cffi not available for ACS scraping, falling back to CrossRef")
        except Exception:
            logger.exception("ACS scraping failed, falling back to CrossRef")

    if provider == "aip" and journal_slug:
        try:
            from .aip_fetch import query_aip_issue
            entries = query_aip_issue(journal_slug, volume, issue, journal_name)
            if entries:
                _fill_missing_abstracts_from_crossref(entries, issn, year)
                return entries
            logger.info("AIP scraping returned empty, falling back to CrossRef")
        except ImportError:
            logger.warning("curl_cffi not available, falling back to CrossRef")
        except Exception:
            logger.exception("AIP scraping failed, falling back to CrossRef")

    return query_journal_issue(issn, volume, issue, journal_name, year)


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

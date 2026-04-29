import logging
import time
from urllib.request import Request, urlopen
from urllib.error import URLError
import json

from .journal_entry import JournalEntry

logger = logging.getLogger(__name__)

CROSSREF_WORKS = "https://api.crossref.org/works"
MAX_RETRIES = 3
RETRY_BACKOFF = (1, 2, 4)

# Per-year cache to avoid re-fetching CrossRef when querying multiple issues
# from the same journal/year.
_cache: dict[str, list[dict]] = {}


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

    params = (
        f"filter=issn:{issn},type:journal-article"
        f",from-pub-date:{year}-01-01,until-pub-date:{year+1}-01-01"
        f"&rows=1000&select=DOI,title,volume,issue,page,author,published-print,published-online"
    )
    url = f"{CROSSREF_WORKS}?{params}"
    logger.info("Fetching CrossRef data for %s %d...", issn, year)

    data = _crossref_get(url)
    items = data.get("message", {}).get("items", [])

    # Filter out non-articles (issue info, masthead)
    articles = [i for i in items if not JournalEntry.is_non_article(i.get("DOI", ""))]
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


def query_journal_issue_full(issn: str, volume: str, issue: str,
                            journal_name: str = "", year: int | None = None,
                            acs_code: str = "") -> dict[str, JournalEntry]:
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
            logger.warning("cloudscraper not available, falling back to CrossRef")
        except Exception:
            logger.exception("ACS scraping failed, falling back to CrossRef")

    return query_journal_issue(issn, volume, issue, journal_name, year)


def clear_cache():
    """Clear the CrossRef year-level cache (useful for testing)."""
    _cache.clear()
    from .acs_fetch import _cache as _acs_cache
    _acs_cache.clear()

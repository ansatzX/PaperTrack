"""PubMed/NCBI issue search fallback for journal reports.

Second tier in the journal source chain defined in AGENTS.md:

    official issue page (plain HTTP) -> PubMed issue search -> CrossRef

The PubMed query must request abstracts (``format=abstract``) so the report can
expose them.  The query selects the formal issue via the ``[jour]``,
``[volume]`` and ``[issue]`` fields, e.g.::

    https://pubmed.ncbi.nlm.nih.gov/?term=%22J+Chem+Theory+Comput%22%5Bjour%5D+AND+22%5Bvolume%5D+AND+14%5Bissue%5D&format=abstract&size=200

Coverage is a third-party index: it may be partial for a given issue (notably
APS titles).  That under-reporting is accepted by the project policy; callers
must not treat a fallback result as authoritative completeness.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import quote_plus

from bs4 import BeautifulSoup

from .adapter_result import AdapterResult, BLOCKED, FETCH_FAILED, SUCCESS
from .diagnostics import record_fetch_attempt
from .http_fetch import fetch_html_resilient
from .identity import normalize_doi
from .journal_entry import JournalEntry

logger = logging.getLogger(__name__)

PUBMED_SEARCH_URL = (
    "https://pubmed.ncbi.nlm.nih.gov/?term={term}&format=abstract&size=200"
)

# "2026 Jul 28;22(14):7025-7042."  ->  date/volume/issue/pages
_CIT_RE = re.compile(
    r"(?P<year>\d{4})\s+(?P<month>[A-Z][a-z]{2})\s+(?P<day>\d{1,2})"
    r";\s*(?P<vol>\d+)(?:\((?P<issue>\d+)\))?:?\s*(?P<pages>[0-9-]*)"
)

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def build_search_url(abbrev: str, volume: str, issue: str) -> str:
    """Build the PubMed abstract-exposing search URL for a formal issue."""
    term = quote_plus(f'"{abbrev}"[jour] AND {volume}[volume] AND {issue}[issue]')
    return PUBMED_SEARCH_URL.format(term=term)


def _text_of(element) -> str:
    if element is None:
        return ""
    return element.get_text(" ", strip=True)


def _doi_from_text(text: str) -> str:
    """Extract a DOI from PubMed citation text like 'doi: 10.1021/acs.jctc.6c00510.'"""
    if not text:
        return ""
    match = re.search(r"10\.\d{4,9}/[^\s]+", text)
    if not match:
        return ""
    return match.group(0).rstrip(".,;)")


def _clean_author(author: str) -> str:
    """Strip PubMed trailing affiliation numbers and inline MathML from a name.

    PubMed renders Greek-letter author names (e.g. ``epsilon Collaboration``)
    as inline ``<math>...</math>`` markup; that markup must be reduced to its
    text content before it can reach the ledger.
    """
    author = re.sub(r"<[^>]+>", "", author)
    author = re.sub(r"\s+(?:\d+\s*)+", "", author.strip())
    author = re.sub(r"\s+", " ", author).strip()
    return author


def _parse_cit(text: str) -> dict:
    """Parse a PubMed journal citation into date/volume/issue/pages."""
    out = {"date": "", "volume": "", "issue": "", "pages": ""}
    match = _CIT_RE.search(text or "")
    if not match:
        return out
    year = int(match.group("year"))
    month = _MONTHS.get((match.group("month") or "").lower())
    day = int(match.group("day") or 0)
    if month and day:
        out["date"] = f"{year:04d}-{month:02d}-{day:02d}"
    out["volume"] = match.group("vol") or ""
    out["issue"] = match.group("issue") or ""
    out["pages"] = match.group("pages") or ""
    return out


def parse_pubmed_issue_html(html: str, *, journal_name: str = "") -> tuple[dict[str, JournalEntry], int]:
    """Parse PubMed search result HTML into DOI-keyed JournalEntries.

    Returns ``(entries, skipped_no_doi)``.  Records without a DOI are skipped
    because the report keys papers by DOI; their count is returned for
    diagnostics so a later extractor upgrade can recover them.
    """
    soup = BeautifulSoup(html, "lxml")
    entries: dict[str, JournalEntry] = {}
    skipped_no_doi = 0
    for art in soup.find_all("article", class_="article-overview"):
        title_a = art.select_one("h1.heading-title a[data-article-id]")
        title = _text_of(title_a)
        if not title:
            continue
        doi = _doi_from_text(_text_of(art.select_one(".citation-doi")))
        if not doi:
            skipped_no_doi += 1
            continue
        cit = _parse_cit(_text_of(art.select_one(".cit")))
        authors_raw = _text_of(art.select_one(".authors-list"))
        authors = [_clean_author(a) for a in authors_raw.split(",")]
        authors = [a for a in authors if a]
        abstract = _text_of(art.select_one(".abstract-content"))
        entry = JournalEntry(
            doi=normalize_doi(doi),
            title=title,
            authors=authors,
            abstract=abstract,
            journal_name=journal_name,
            volume=cit["volume"],
            issue=cit["issue"],
            pages=cit["pages"],
            date=cit["date"],
        )
        entries[entry.doi] = entry
    return entries, skipped_no_doi


def query_pubmed_issue_result(
    abbrev: str,
    volume: str,
    issue: str,
    journal_name: str = "",
) -> AdapterResult[dict[str, JournalEntry]]:
    """Fetch a formal issue from the PubMed search path.

    Returns an AdapterResult keyed by DOI.  A zero-article PubMed response is
    reported as a failure, never as a successful empty issue, so the caller can
    continue down the chain to CrossRef.
    """
    if not abbrev:
        return AdapterResult(
            entries={},
            outcome=FETCH_FAILED,
            provider="pubmed",
            source="journal",
            source_role="third_party_issue",
            message="pubmed: no journal abbreviation configured",
        )
    url = build_search_url(abbrev, volume, issue)
    logger.info("Fetching PubMed issue search: %s", url)
    fetched = fetch_html_resilient(url, provider="pubmed", required_selector="article")
    html = fetched.html
    if not html or fetched.outcome == BLOCKED:
        record_fetch_attempt({
            "source_role": "third_party_issue",
            "source": "journal",
            "provider": "pubmed",
            "journal": abbrev,
            "volume": volume,
            "issue": issue,
            "url": url,
            "stage": "issue_search",
            "outcome": fetched.outcome or FETCH_FAILED,
            "status_code": fetched.status_code,
            "message": "PubMed issue search could not be fetched",
        }, html=html or None)
        return AdapterResult(
            entries={},
            outcome=fetched.outcome or FETCH_FAILED,
            provider="pubmed",
            source="journal",
            source_role="third_party_issue",
            message=f"pubmed: {fetched.outcome or 'fetch_failed'}",
        )

    entries, skipped_no_doi = parse_pubmed_issue_html(html, journal_name=journal_name)
    if not entries:
        record_fetch_attempt({
            "source_role": "third_party_issue",
            "source": "journal",
            "provider": "pubmed",
            "journal": abbrev,
            "volume": volume,
            "issue": issue,
            "url": url,
            "stage": "issue_search",
            "outcome": FETCH_FAILED,
            "status_code": fetched.status_code,
            "message": "PubMed returned no DOI-bearing articles for this issue",
            "skipped_no_doi": skipped_no_doi,
        }, html=html or None)
        return AdapterResult(
            entries={},
            outcome=FETCH_FAILED,
            provider="pubmed",
            source="journal",
            source_role="third_party_issue",
            message="pubmed: no articles found for issue",
            metadata={"skipped_no_doi": skipped_no_doi},
        )

    record_fetch_attempt({
        "source_role": "third_party_issue",
        "source": "journal",
        "provider": "pubmed",
        "journal": abbrev,
        "volume": volume,
        "issue": issue,
        "url": url,
        "stage": "issue_search",
        "outcome": SUCCESS,
        "status_code": fetched.status_code,
        "articles": len(entries),
        "skipped_no_doi": skipped_no_doi,
        "fallback_used": ["pubmed_issue"],
    })
    return AdapterResult(
        entries=entries,
        outcome=SUCCESS,
        provider="pubmed",
        source="journal",
        source_role="third_party_issue",
        message=f"pubmed: {len(entries)} articles for issue",
        metadata={"skipped_no_doi": skipped_no_doi, "url": url},
    )

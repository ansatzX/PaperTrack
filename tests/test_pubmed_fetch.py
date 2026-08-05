"""Unit tests for the PubMed issue-search fallback source."""

import pytest

from papertrack import pubmed_fetch
from papertrack.adapter_result import BLOCKED, FETCH_FAILED, SUCCESS
from papertrack.http_fetch import HttpFetchResult


# ---------------------------------------------------------------------------
# URL construction
# ---------------------------------------------------------------------------

def test_build_search_url_requests_abstract_format():
    url = pubmed_fetch.build_search_url("J Chem Theory Comput", "22", "14")
    assert "format=abstract" in url
    assert "size=200" in url
    assert "%22J+Chem+Theory+Comput%22%5Bjour%5D" in url
    assert "22%5Bvolume%5D" in url
    assert "14%5Bissue%5D" in url
    assert url.startswith("https://pubmed.ncbi.nlm.nih.gov/?term=")


# ---------------------------------------------------------------------------
# HTML parsing
# ---------------------------------------------------------------------------

def _article_html(title, doi="", cit="2026 Jul 28;22(14):7025-7042.",
                  authors="Alice Smith 1, Bob Jones 2 3", abstract="Abstract body."):
    doi_div = f'<div class="citation-doi">doi: {doi}</div>' if doi else ""
    return f"""
    <article class="article-overview">
      <h1 class="heading-title"><a data-article-id="123" href="/123/">{title}</a></h1>
      {doi_div}
      <div class="cit">{cit}</div>
      <div class="authors-list">{authors}</div>
      <div class="abstract-content">{abstract}</div>
    </article>
    """


def test_parse_pubmed_issue_html_full_entry():
    html = _article_html(
        "Some Title",
        doi="10.1021/acs.jctc.6c00510",
        authors="Mario Villada-Balbuena 1 2, John Doe",
        abstract="We show that the method works.",
    )
    entries, skipped = pubmed_fetch.parse_pubmed_issue_html(
        html, journal_name="Journal of Chemical Theory and Computation"
    )
    assert skipped == 0
    assert set(entries) == {"10.1021/acs.jctc.6c00510"}
    entry = entries["10.1021/acs.jctc.6c00510"]
    assert entry.title == "Some Title"
    assert entry.authors == ["Mario Villada-Balbuena", "John Doe"]
    assert entry.abstract == "We show that the method works."
    assert entry.volume == "22"
    assert entry.issue == "14"
    assert entry.pages == "7025-7042"
    assert entry.date == "2026-07-28"
    assert entry.journal_name == "Journal of Chemical Theory and Computation"


def test_parse_pubmed_issue_html_skips_articles_without_doi():
    with_doi = _article_html("With DOI", doi="10.1021/acs.jctc.6c00510")
    no_doi = _article_html("No DOI", doi="")
    entries, skipped = pubmed_fetch.parse_pubmed_issue_html(
        with_doi + no_doi, journal_name="J"
    )
    assert set(entries) == {"10.1021/acs.jctc.6c00510"}
    assert skipped == 1


def test_clean_author_strips_multiple_trailing_affiliation_numbers():
    assert pubmed_fetch._clean_author("Mario Villada-Balbuena 1 2") == "Mario Villada-Balbuena"
    assert pubmed_fetch._clean_author("Alice Smith 1") == "Alice Smith"
    assert pubmed_fetch._clean_author("Bob") == "Bob"


def test_parse_cit_handles_issue_and_pages():
    parsed = pubmed_fetch._parse_cit("2026 Jul 28;22(14):7025-7042.")
    assert parsed["date"] == "2026-07-28"
    assert parsed["volume"] == "22"
    assert parsed["issue"] == "14"
    assert parsed["pages"] == "7025-7042"


def test_doi_extraction_strips_punctuation():
    assert pubmed_fetch._doi_from_text("doi: 10.1021/acs.jctc.6c00510.") == "10.1021/acs.jctc.6c00510"
    assert pubmed_fetch._doi_from_text("doi: 10.1063/5.0179137, accessed today") == "10.1063/5.0179137"


# ---------------------------------------------------------------------------
# query_pubmed_issue_result
# ---------------------------------------------------------------------------

def test_query_empty_abbrev_fails_without_network(monkeypatch):
    called = []

    def no_fetch(*args, **kwargs):
        called.append(True)
        raise AssertionError("must not fetch without an abbreviation")

    monkeypatch.setattr(pubmed_fetch, "fetch_html_resilient", no_fetch)
    result = pubmed_fetch.query_pubmed_issue_result("", "22", "14", "J")
    assert not called
    assert result.outcome == FETCH_FAILED
    assert result.entries == {}
    assert result.provider == "pubmed"


def test_query_success_records_attempt_and_returns_entries(monkeypatch):
    html = _article_html("Title", doi="10.1021/acs.jctc.6c00510")
    monkeypatch.setattr(
        pubmed_fetch,
        "fetch_html_resilient",
        lambda *a, **k: HttpFetchResult(html=html, outcome="", status_code=200),
    )
    events = []
    monkeypatch.setattr(
        pubmed_fetch,
        "record_fetch_attempt",
        lambda event, html=None: events.append(event),
    )

    result = pubmed_fetch.query_pubmed_issue_result(
        "J Chem Theory Comput", "22", "14", "Journal of Chemical Theory and Computation"
    )

    assert result.outcome == SUCCESS
    assert result.provider == "pubmed"
    assert result.source_role == "third_party_issue"
    assert set(result.entries) == {"10.1021/acs.jctc.6c00510"}
    assert len(events) == 1
    event = events[0]
    assert event["provider"] == "pubmed"
    assert event["outcome"] == SUCCESS
    assert event["articles"] == 1
    assert event["fallback_used"] == ["pubmed_issue"]
    assert "format=abstract" in event["url"]


def test_query_zero_entries_is_failure_not_empty_success(monkeypatch):
    # A PubMed page with no article.article-overview nodes is a failure, so the
    # caller can continue down the chain to CrossRef instead of treating the
    # issue as successfully processed with zero articles.
    monkeypatch.setattr(
        pubmed_fetch,
        "fetch_html_resilient",
        lambda *a, **k: HttpFetchResult(
            html="<html><body>No results found.</body></html>", outcome="", status_code=200
        ),
    )
    events = []
    monkeypatch.setattr(
        pubmed_fetch,
        "record_fetch_attempt",
        lambda event, html=None: events.append(event),
    )

    result = pubmed_fetch.query_pubmed_issue_result("J Chem Phys", "164", "1", "JCP")

    assert result.outcome == FETCH_FAILED
    assert result.entries == {}
    assert len(events) == 1
    assert events[0]["outcome"] == FETCH_FAILED
    assert events[0]["message"] == "PubMed returned no DOI-bearing articles for this issue"


def test_query_blocked_is_failure(monkeypatch):
    monkeypatch.setattr(
        pubmed_fetch,
        "fetch_html_resilient",
        lambda *a, **k: HttpFetchResult(html="", outcome=BLOCKED, status_code=403),
    )
    events = []
    monkeypatch.setattr(
        pubmed_fetch,
        "record_fetch_attempt",
        lambda event, html=None: events.append(event),
    )

    result = pubmed_fetch.query_pubmed_issue_result("J Chem Phys", "164", "1", "JCP")

    assert result.outcome == BLOCKED
    assert result.entries == {}
    assert len(events) == 1
    assert events[0]["status_code"] == 403

from papertrack.adapter_result import (
    FETCH_FAILED,
    OFFICIAL_EMPTY_SUSPICIOUS,
    PARSE_FAILED,
)


def test_aip_issue_result_distinguishes_fetch_failure(monkeypatch):
    from papertrack import aip_fetch

    aip_fetch._cache.clear()
    monkeypatch.setattr(aip_fetch, "_fetch_html", lambda *args, **kwargs: "")

    result = aip_fetch.query_aip_issue_result("jcp", "164", "16", "Journal of Chemical Physics")

    assert result.outcome == FETCH_FAILED
    assert result.entries == {}
    assert result.provider == "aip"


def test_acs_issue_result_distinguishes_suspicious_empty_parse(monkeypatch):
    from papertrack import acs_fetch

    acs_fetch._cache.clear()

    class Response:
        text = "<html><main>No issue items</main></html>"
        status_code = 200

    monkeypatch.setattr(acs_fetch.requests, "get", lambda *args, **kwargs: Response())

    result = acs_fetch.query_acs_issue_result("jctcce", "22", "8", "JCTC")

    assert result.outcome == OFFICIAL_EMPTY_SUSPICIOUS
    assert result.entries == {}
    assert result.provider == "acs"


def test_arxiv_result_distinguishes_parse_failure(monkeypatch):
    from papertrack import arxiv_index_fetch

    monkeypatch.setattr(arxiv_index_fetch.feedparser, "parse", lambda url: {})

    result = arxiv_index_fetch.query_arxiv_result(
        "2026-06-01",
        "2026-06-02",
        query_args={},
    )

    assert result.outcome == PARSE_FAILED
    assert result.entries == {}
    assert result.provider == "arxiv"

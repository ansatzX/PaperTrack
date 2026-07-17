from papertrack.adapter_result import (
    BLOCKED,
    FETCH_FAILED,
    OFFICIAL_EMPTY_SUSPICIOUS,
    PARSE_FAILED,
    RATE_LIMITED,
    SUCCESS,
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

    monkeypatch.setattr(
        acs_fetch,
        "_fetch_html",
        lambda *args, **kwargs: ("<html><main>No issue items</main></html>", "", 200),
    )

    result = acs_fetch.query_acs_issue_result("jctcce", "22", "8", "JCTC")

    assert result.outcome == OFFICIAL_EMPTY_SUSPICIOUS
    assert result.entries == {}
    assert result.provider == "acs"


def test_acs_issue_result_distinguishes_cloudflare_block(monkeypatch):
    from papertrack import acs_fetch

    acs_fetch._cache.clear()

    monkeypatch.setattr(
        acs_fetch,
        "_fetch_html",
        lambda *args, **kwargs: (
            "<html><head><title>Just a moment...</title></head></html>",
            BLOCKED,
            403,
        ),
    )

    result = acs_fetch.query_acs_issue_result("jcisd8", "66", "11", "JCIM")

    assert result.outcome == BLOCKED
    assert result.entries == {}
    assert result.provider == "acs"


def test_acs_issue_result_rejects_article_card_without_doi(monkeypatch):
    from papertrack import acs_fetch

    acs_fetch._cache.clear()
    monkeypatch.setattr(
        acs_fetch,
        "_fetch_html",
        lambda *args, **kwargs: (
            """
            <div class="issue-item clearfix">
              <h3 class="issue-item_title"><a href="/article/no-doi">Article title</a></h3>
            </div>
            """,
            "",
            200,
        ),
    )

    result = acs_fetch.query_acs_issue_result("jcisd8", "66", "12", "JCIM")

    assert result.outcome == PARSE_FAILED
    assert result.entries == {}
    assert result.metadata["critical_field_failures"] == [
        {"card": 1, "field": "doi"}
    ]


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


def test_arxiv_result_paginates_when_first_page_is_full(monkeypatch):
    from papertrack import arxiv_index_fetch

    seen_urls = []

    def fake_parse(url):
        seen_urls.append(url)
        if "start=200" in url:
            return {"feed": {"summary": _arxiv_summary(200, 1)}}
        return {"feed": {"summary": _arxiv_summary(0, 200)}}

    monkeypatch.setattr(arxiv_index_fetch.feedparser, "parse", fake_parse)

    result = arxiv_index_fetch.query_arxiv_result(
        "2026-06-01",
        "2026-06-02",
        query_args={"size": "200"},
    )

    assert result.outcome == SUCCESS
    assert len(result.entries) == 201
    assert len(seen_urls) == 2
    assert "start=" not in seen_urls[0]
    assert "start=200" in seen_urls[1]


def test_arxiv_result_parse_failure_on_second_page_is_not_partial_success(monkeypatch):
    from papertrack import arxiv_index_fetch

    def fake_parse(url):
        if "start=200" in url:
            return {}
        return {"feed": {"summary": _arxiv_summary(0, 200)}}

    monkeypatch.setattr(arxiv_index_fetch.feedparser, "parse", fake_parse)

    result = arxiv_index_fetch.query_arxiv_result(
        "2026-06-01",
        "2026-06-02",
        query_args={"size": "200"},
    )

    assert result.outcome == PARSE_FAILED
    assert result.entries == {}


def test_arxiv_result_distinguishes_rate_limit(monkeypatch):
    from papertrack import arxiv_index_fetch

    class RateLimitedResult(dict):
        status = 429
        feed = {}
        headers = {}

        def __init__(self):
            super().__init__(feed={})

    monkeypatch.setattr(arxiv_index_fetch.feedparser, "parse", lambda url: RateLimitedResult())
    monkeypatch.setattr(arxiv_index_fetch.time, "sleep", lambda seconds: None)

    result = arxiv_index_fetch.query_arxiv_result(
        "2026-06-01",
        "2026-06-02",
        query_args={"size": "200"},
    )

    assert result.outcome == RATE_LIMITED
    assert result.entries == {}
    assert result.metadata["http_status"] == 429


def _arxiv_summary(start: int, count: int) -> str:
    items = []
    for i in range(start, start + count):
        items.append(
            f"""
            <li class="arxiv-result">
              <p class="list-title is-inline-block">
                <a href="https://arxiv.org/abs/2606.{i:05d}">arXiv:2606.{i:05d}</a>
              </p>
              <p class="title is-5 mathjax">Paper {i}</p>
              <p class="authors"><a>Author {i}</a></p>
              <p class="abstract mathjax"><span class="abstract-full">Abstract {i}</span></p>
              <span class="tag is-small is-link tooltip is-tooltip-top">cs.AI</span>
            </li>
            """
        )
    return "<ol>" + "\n".join(items) + "</ol>"

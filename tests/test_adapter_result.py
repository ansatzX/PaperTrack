from papertrack.adapter_result import (
    PARSE_FAILED,
    RATE_LIMITED,
    SUCCESS,
)


def test_arxiv_result_distinguishes_parse_failure(monkeypatch):
    from papertrack.arxiv import fetch as arxiv_index_fetch

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
    from papertrack.arxiv import fetch as arxiv_index_fetch

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
    from papertrack.arxiv import fetch as arxiv_index_fetch

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


def test_arxiv_result_keeps_exact_primary_and_cross_list_members(monkeypatch):
    from papertrack.arxiv import fetch as arxiv_index_fetch

    summary = "<ol>" + "".join([
        _arxiv_item(1, ["cs.AI", "cs.LG"]),
        _arxiv_item(2, ["cs.LG", "cs.AI"]),
        _arxiv_item(3, ["cs.LG"]),
    ]) + "</ol>"
    monkeypatch.setattr(
        arxiv_index_fetch.feedparser,
        "parse",
        lambda url: {"feed": {"summary": summary}},
    )

    result = arxiv_index_fetch.query_arxiv_result(
        "2026-06-01",
        "2026-06-02",
        query_args={"size": "200"},
        required_category="cs.AI",
    )

    assert result.outcome == SUCCESS
    assert set(result.entries) == {"arXiv:2606.00001", "arXiv:2606.00002"}
    assert result.metadata["candidate_count"] == 3
    assert result.metadata["accepted_count"] == 2
    assert result.metadata["rejected_count"] == 1


def test_arxiv_category_filter_paginates_by_raw_candidate_count(monkeypatch):
    from papertrack.arxiv import fetch as arxiv_index_fetch

    seen_urls = []

    def fake_parse(url):
        seen_urls.append(url)
        if "start=2" in url:
            summary = _arxiv_empty_summary()
        elif "start=1" in url:
            summary = "<ol>" + _arxiv_item(2, ["cs.AI"]) + "</ol>"
        else:
            summary = "<ol>" + _arxiv_item(1, ["cs.LG"]) + "</ol>"
        return {"feed": {"summary": summary}}

    monkeypatch.setattr(arxiv_index_fetch.feedparser, "parse", fake_parse)

    result = arxiv_index_fetch.query_arxiv_result(
        "2026-06-01",
        "2026-06-02",
        query_args={"size": "1"},
        required_category="cs.AI",
    )

    assert result.outcome == SUCCESS
    assert set(result.entries) == {"arXiv:2606.00002"}
    assert any("start=1" in url for url in seen_urls)
    assert result.metadata["candidate_count"] == 2
    assert result.metadata["accepted_count"] == 1
    assert result.metadata["rejected_count"] == 1


def test_arxiv_result_rejects_unrecognized_successful_empty_page(monkeypatch):
    from papertrack.arxiv import fetch as arxiv_index_fetch

    monkeypatch.setattr(
        arxiv_index_fetch.feedparser,
        "parse",
        lambda url: {"feed": {"summary": "<html><main>unexpected page</main></html>"}},
    )

    result = arxiv_index_fetch.query_arxiv_result(
        "2026-06-01",
        "2026-06-02",
        query_args={"size": "200"},
    )

    assert result.outcome == PARSE_FAILED
    assert result.entries == {}


def test_arxiv_result_accepts_verified_no_results_page(monkeypatch):
    from papertrack.arxiv import fetch as arxiv_index_fetch

    monkeypatch.setattr(
        arxiv_index_fetch.feedparser,
        "parse",
        lambda url: {"feed": {"summary": _arxiv_empty_summary()}},
    )

    result = arxiv_index_fetch.query_arxiv_result(
        "2026-06-01",
        "2026-06-02",
        query_args={"size": "200"},
    )

    assert result.outcome == SUCCESS
    assert result.entries == {}


def test_arxiv_request_delay_applies_between_paginated_http_requests(monkeypatch):
    from papertrack.arxiv import fetch as arxiv_index_fetch

    summaries = iter([_arxiv_summary(0, 1), _arxiv_empty_summary()])
    monotonic_values = iter([100.0, 100.0, 102.0, 107.0])
    sleeps = []
    arxiv_index_fetch._last_request_at = 0.0
    monkeypatch.setattr(
        arxiv_index_fetch.feedparser,
        "parse",
        lambda url: {"feed": {"summary": next(summaries)}},
    )
    monkeypatch.setattr(arxiv_index_fetch.time, "monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(arxiv_index_fetch.time, "sleep", sleeps.append)

    result = arxiv_index_fetch.query_arxiv_result(
        "2026-06-01",
        "2026-06-02",
        query_args={"size": "1"},
        request_delay=7,
    )

    assert result.outcome == SUCCESS
    assert sleeps == [5.0]


def test_arxiv_request_delay_applies_before_retry_attempt(monkeypatch):
    from papertrack.arxiv import fetch as arxiv_index_fetch

    attempts = iter([OSError("temporary failure"), {"feed": {"summary": "unused"}}])
    monotonic_values = iter([100.0, 100.0, 102.0, 107.0])
    sleeps = []
    arxiv_index_fetch._last_request_at = 0.0

    def fake_parse(url):
        result = next(attempts)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(arxiv_index_fetch.feedparser, "parse", fake_parse)
    monkeypatch.setattr(arxiv_index_fetch.time, "monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(arxiv_index_fetch.time, "sleep", sleeps.append)

    result = arxiv_index_fetch._fetch_arxiv_page("https://arxiv.org/search", request_delay=7)

    assert result.results is not None
    assert sleeps == [1, 5.0]


def test_arxiv_diagnostic_counts_deduplicate_repeated_page_candidates(monkeypatch):
    from papertrack.arxiv import fetch as arxiv_index_fetch

    summaries = iter([_arxiv_summary(1, 1), _arxiv_summary(1, 1)])
    monkeypatch.setattr(
        arxiv_index_fetch.feedparser,
        "parse",
        lambda url: {"feed": {"summary": next(summaries)}},
    )

    result = arxiv_index_fetch.query_arxiv_result(
        "2026-06-01",
        "2026-06-02",
        query_args={"size": "1"},
        required_category="cs.AI",
    )

    assert result.outcome == SUCCESS
    assert result.metadata["candidate_count"] == 1
    assert result.metadata["raw_candidate_count"] == 2
    assert result.metadata["duplicate_count"] == 1
    assert result.metadata["accepted_count"] == 1
    assert result.metadata["rejected_count"] == 0


def test_arxiv_category_filter_fails_closed_when_subjects_are_missing(monkeypatch):
    from papertrack.arxiv import fetch as arxiv_index_fetch

    summary = "<ol>" + _arxiv_item(1, []) + "</ol>"
    monkeypatch.setattr(
        arxiv_index_fetch.feedparser,
        "parse",
        lambda url: {"feed": {"summary": summary}},
    )

    result = arxiv_index_fetch.query_arxiv_result(
        "2026-06-01",
        "2026-06-02",
        query_args={"size": "200"},
        required_category="cs.AI",
    )

    assert result.outcome == PARSE_FAILED
    assert result.entries == {}
    assert result.metadata["field"] == "subjects"


def test_arxiv_result_distinguishes_rate_limit(monkeypatch):
    from papertrack.arxiv import fetch as arxiv_index_fetch

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
    items = [_arxiv_item(i, ["cs.AI"]) for i in range(start, start + count)]
    return "<ol>" + "\n".join(items) + "</ol>"


def _arxiv_item(index: int, subjects: list[str]) -> str:
    tags = "".join(
        f'<span class="tag is-small {"is-link" if position == 0 else "is-grey"}">'
        f"{subject}</span>"
        for position, subject in enumerate(subjects)
    )
    return f"""
        <li class="arxiv-result">
          <p class="list-title is-inline-block">
            <a href="https://arxiv.org/abs/2606.{index:05d}">arXiv:2606.{index:05d}</a>
          </p>
          <p class="title is-5 mathjax">Paper {index}</p>
          <p class="authors"><a>Author {index}</a></p>
          <p class="abstract mathjax"><span class="abstract-full">Abstract {index}</span></p>
          {tags}
        </li>
        """


def _arxiv_empty_summary() -> str:
    return '<h1 class="title is-clearfix">Sorry, your query returned no results</h1>'

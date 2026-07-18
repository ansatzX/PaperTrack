import pytest

from papertrack.adapter_result import BLOCKED, FETCH_FAILED, SUCCESS
from papertrack.http_fetch import HttpFetchResult


def _issue_html(cards: str) -> str:
    return f'<html><div class="issue-list">{cards}</div></html>'


def _card(doi: str, article_id: str = "") -> str:
    control = (
        f'<a class="js-show-abstract" data-articleid="{article_id}">Abstract</a>'
        if article_id
        else ""
    )
    suffix = doi.rsplit("/", 1)[-1]
    return f"""
    <div class="al-article-items">
      <h3 class="item-title">
        <a href="/aip/jcp/article/164/16/{suffix}/123/Title">Title {doi}</a>
      </h3>
      <div class="ww-citation-primary">
        164, {suffix} (2026)
        <span class="citation-label"><a href="https://doi.org/{doi}">{doi}</a></span>
      </div>
      {control}
    </div>
    """


class Response:
    def __init__(self, status_code=200, text="", payload=None):
        self.status_code = status_code
        self.text = text
        self._payload = payload
        self.headers = {}

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def test_aip_abstract_success_uses_journal_scoped_endpoint(monkeypatch):
    from papertrack import aip_fetch

    seen = []
    monkeypatch.setattr(
        aip_fetch,
        "_aip_get",
        lambda url, **kwargs: seen.append((url, kwargs)) or Response(
            payload={"Success": True, "Html": "<h2>Abstract</h2><p>Official text.</p>"}
        ),
    )
    aip_fetch.clear_aip_caches()

    result = aip_fetch._fetch_aip_abstract(
        "3387571",
        "https://pubs.aip.org/aip/jcp/issue/164/16",
        "jcp",
    )

    assert result.abstract == "Official text."
    assert result.outcome == SUCCESS
    assert result.status_code == 200
    assert result.url == "https://pubs.aip.org/aip/jcp/PlatformArticle/ArticleAbstractAjax"
    assert seen[0][0] == result.url
    assert seen[0][1]["params"] == {"articleId": "3387571", "layAbstract": "False"}


def test_aip_abstract_distinguishes_empty_blocked_and_transport(monkeypatch):
    from papertrack import aip_fetch

    aip_fetch.clear_aip_caches()
    monkeypatch.setattr(aip_fetch.time, "sleep", lambda delay: None)
    monkeypatch.setattr(
        aip_fetch,
        "_aip_get",
        lambda *args, **kwargs: Response(payload={"Success": True, "Html": ""}),
    )
    empty = aip_fetch._fetch_aip_abstract("empty", "https://pubs.aip.org/aip/jcp/issue/1/1", "jcp")
    assert empty.outcome == "official_response_empty"
    assert empty.status_code == 200

    monkeypatch.setattr(
        aip_fetch,
        "_aip_get",
        lambda *args, **kwargs: Response(403, "Just a moment"),
    )
    blocked = aip_fetch._fetch_aip_abstract("blocked", "https://pubs.aip.org/aip/jcp/issue/1/1", "jcp")
    assert blocked.outcome == BLOCKED
    assert blocked.status_code == 403

    monkeypatch.setattr(
        aip_fetch,
        "_aip_get",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("offline")),
    )
    failed = aip_fetch._fetch_aip_abstract("failed", "https://pubs.aip.org/aip/jcp/issue/1/1", "jcp")
    assert failed.outcome == FETCH_FAILED
    assert "offline" in failed.message


def test_aip_duplicate_article_id_uses_one_ajax_request(monkeypatch):
    from papertrack import aip_fetch

    aip_fetch.clear_aip_caches()
    html = _issue_html(
        _card("10.1063/one", "same-id") + _card("10.1063/two", "same-id")
    )
    monkeypatch.setattr(
        aip_fetch,
        "_fetch_issue_http",
        lambda *args, **kwargs: HttpFetchResult(html=html, status_code=200),
    )
    calls = []

    def fake_abstract(article_id, referer_url, journal_slug):
        calls.append((article_id, referer_url, journal_slug))
        return aip_fetch.AipAbstractResult(
            abstract="Shared official abstract.",
            outcome=SUCCESS,
            status_code=200,
            url=aip_fetch.aip_abstract_url(journal_slug),
            article_id=article_id,
        )

    monkeypatch.setattr(aip_fetch, "_fetch_aip_abstract", fake_abstract)
    result = aip_fetch.query_aip_issue_result("jcp", "164", "16", "JCP")

    assert len(result.entries) == 2
    assert calls == [
        (
            "same-id",
            "https://pubs.aip.org/aip/jcp/issue/164/16",
            "jcp",
        )
    ]
    assert all(entry.abstract == "Shared official abstract." for entry in result.entries.values())


def test_aip_challenge_uses_one_browser_bundle_and_no_article_page(monkeypatch):
    from papertrack import aip_fetch

    aip_fetch.clear_aip_caches()
    html = _issue_html(_card("10.1063/browser", "browser-id"))
    issue_calls = []
    browser_calls = []
    monkeypatch.setattr(
        aip_fetch,
        "_fetch_issue_http",
        lambda url: issue_calls.append(url) or HttpFetchResult(
            html="Just a moment",
            outcome=BLOCKED,
            status_code=403,
        ),
    )
    monkeypatch.setattr(
        aip_fetch,
        "_fetch_aip_browser_bundle",
        lambda url, slug: browser_calls.append((url, slug)) or aip_fetch.AipBrowserBundle(
            html=html,
            abstracts={
                "browser-id": aip_fetch.AipAbstractResult(
                    abstract="Browser official abstract.",
                    outcome=SUCCESS,
                    status_code=200,
                    url=aip_fetch.aip_abstract_url("jcp"),
                    article_id="browser-id",
                )
            },
        ),
    )

    result = aip_fetch.query_aip_issue_result("jcp", "164", "16", "JCP")

    assert result.entries["10.1063/browser"].abstract == "Browser official abstract."
    assert issue_calls == ["https://pubs.aip.org/aip/jcp/issue/164/16"]
    assert browser_calls == [
        ("https://pubs.aip.org/aip/jcp/issue/164/16", "jcp")
    ]


def test_aip_browser_bundle_runs_once_under_xvfb(monkeypatch):
    from papertrack import aip_fetch

    captured = {}
    wait_roles = []

    class Completed:
        returncode = 0
        stderr = ""
        stdout = '{"html":"<div class=\\"al-article-items\\"></div>","abstracts":{}}'

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return Completed()

    monkeypatch.setattr(aip_fetch.subprocess, "run", fake_run)
    monkeypatch.setattr(
        aip_fetch,
        "_wait_for_aip_interval",
        lambda role: wait_roles.append(role),
    )

    bundle = aip_fetch._fetch_aip_browser_bundle(
        "https://pubs.aip.org/aip/jcp/issue/164/16",
        "jcp",
    )

    assert bundle.html.startswith('<div class="al-article-items"')
    assert captured["command"][:2] == ["xvfb-run", "-a"]
    assert captured["command"][-4:] == [
        "papertrack.aip_browser",
        "https://pubs.aip.org/aip/jcp/issue/164/16",
        "jcp",
        "10.0",
    ]
    timeout = captured["kwargs"]["timeout"]
    assert timeout == aip_fetch._aip_browser_timeout(
        aip_fetch.AIP_BROWSER_MAX_ABSTRACTS,
        aip_fetch.AIP_ABSTRACT_INTERVAL,
    )
    minimum_pacing_duration = (
        aip_fetch.AIP_BROWSER_MAX_ABSTRACTS - 1
    ) * aip_fetch.AIP_ABSTRACT_INTERVAL
    assert timeout > minimum_pacing_duration
    assert wait_roles == ["issue"]


def test_aip_browser_completion_paces_next_direct_issue_request(monkeypatch):
    from papertrack import aip_fetch

    clock = [100.0]
    sleeps = []

    class Completed:
        returncode = 0
        stderr = ""
        stdout = '{"html":"<div class=\\"al-article-items\\"></div>","abstracts":{}}'

    class Session:
        def get(self, url, **kwargs):
            return Response()

    def fake_run(command, **kwargs):
        clock[0] += 5
        return Completed()

    def fake_sleep(delay):
        sleeps.append(delay)
        clock[0] += delay

    monkeypatch.setattr(aip_fetch.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(aip_fetch.time, "sleep", fake_sleep)
    monkeypatch.setattr(aip_fetch.subprocess, "run", fake_run)
    monkeypatch.setattr(aip_fetch, "_get_session", lambda: Session())
    aip_fetch.clear_aip_caches()

    aip_fetch._fetch_aip_browser_bundle("https://example.test/issue", "jcp")
    clock[0] += 1
    aip_fetch._aip_get("https://example.test/direct-issue", role="issue")

    assert sleeps == [29.0]


def test_aip_browser_timeout_paces_next_direct_abstract_request(monkeypatch):
    from papertrack import aip_fetch

    clock = [200.0]
    sleeps = []

    class Session:
        def get(self, url, **kwargs):
            return Response()

    def fake_run(command, **kwargs):
        clock[0] += 5
        raise aip_fetch.subprocess.TimeoutExpired(command, kwargs["timeout"])

    def fake_sleep(delay):
        sleeps.append(delay)
        clock[0] += delay

    monkeypatch.setattr(aip_fetch.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(aip_fetch.time, "sleep", fake_sleep)
    monkeypatch.setattr(aip_fetch.subprocess, "run", fake_run)
    monkeypatch.setattr(aip_fetch, "_get_session", lambda: Session())
    aip_fetch.clear_aip_caches()

    bundle = aip_fetch._fetch_aip_browser_bundle(
        "https://example.test/issue",
        "jcp",
    )
    clock[0] += 1
    aip_fetch._aip_get("https://example.test/direct-abstract", role="abstract")

    assert bundle.outcome == FETCH_FAILED
    assert sleeps == [9.0]


def test_aip_browser_timeout_rejects_non_finite_result_before_launch(monkeypatch):
    from papertrack import aip_fetch

    subprocess_calls = []
    monkeypatch.setattr(aip_fetch, "AIP_ABSTRACT_INTERVAL", 1e308)
    monkeypatch.setattr(
        aip_fetch.subprocess,
        "run",
        lambda *args, **kwargs: subprocess_calls.append((args, kwargs)),
    )

    with pytest.raises(RuntimeError, match="browser timeout must be finite"):
        aip_fetch._fetch_aip_browser_bundle(
            "https://example.test/issue",
            "jcp",
        )
    assert subprocess_calls == []


def test_aip_request_clocks_are_role_aware(monkeypatch):
    from papertrack import aip_fetch

    clock = [100.0]
    sleeps = []
    starts = []

    class Session:
        def get(self, url, **kwargs):
            starts.append((url, clock[0]))
            return Response()

    def fake_sleep(delay):
        sleeps.append(delay)
        clock[0] += delay

    monkeypatch.setattr(aip_fetch.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(aip_fetch.time, "sleep", fake_sleep)
    monkeypatch.setattr(aip_fetch, "_get_session", lambda: Session())
    aip_fetch.clear_aip_caches()

    aip_fetch._aip_get("issue-1", role="issue")
    clock[0] += 1
    aip_fetch._aip_get("issue-2", role="issue")

    aip_fetch._aip_get("abstract-1", role="abstract")
    clock[0] += 1
    aip_fetch._aip_get("abstract-2", role="abstract")

    assert sleeps == [29.0, 9.0]
    assert starts[1][1] - starts[0][1] >= 30
    assert starts[3][1] - starts[2][1] >= 10


def test_aip_browser_abstract_wait_uses_request_start_clock(monkeypatch):
    from papertrack import aip_browser

    clock = [200.0]
    sleeps = []

    def fake_sleep(delay):
        sleeps.append(delay)
        clock[0] += delay

    monkeypatch.setattr(aip_browser.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(aip_browser.time, "sleep", fake_sleep)

    first_start = aip_browser._wait_for_abstract_interval(None, 5)
    clock[0] += 2
    second_start = aip_browser._wait_for_abstract_interval(first_start, 5)

    assert sleeps == [8.0]
    assert second_start - first_start >= 10


def test_aip_browser_abstract_interval_enforces_floor():
    from papertrack import aip_browser

    assert aip_browser._browser_abstract_interval("5") == 10
    assert aip_browser._browser_abstract_interval("15") == 15


@pytest.mark.parametrize("raw", ["-1", "nan", "inf", "-inf", "fast"])
def test_aip_browser_abstract_interval_rejects_invalid_values(raw):
    from papertrack import aip_browser

    with pytest.raises(RuntimeError, match="abstract interval"):
        aip_browser._browser_abstract_interval(raw)


def test_aip_browser_rejects_abstract_count_above_timeout_bound():
    from papertrack import aip_browser
    from papertrack.aip_fetch import AIP_BROWSER_MAX_ABSTRACTS

    assert aip_browser._validate_abstract_count(AIP_BROWSER_MAX_ABSTRACTS) is None
    with pytest.raises(RuntimeError, match="exceeds browser abstract request limit"):
        aip_browser._validate_abstract_count(AIP_BROWSER_MAX_ABSTRACTS + 1)


def test_aip_browser_allows_challenge_query_but_blocks_article_navigation():
    from papertrack.aip_browser import _browser_request_allowed

    issue_url = "https://pubs.aip.org/aip/jcp/issue/164/16"
    endpoint = "https://pubs.aip.org/aip/jcp/PlatformArticle/ArticleAbstractAjax"

    assert _browser_request_allowed(
        issue_url + "?__cf_chl_tk=token",
        "document",
        issue_url,
        endpoint,
    )
    assert not _browser_request_allowed(
        "https://pubs.aip.org/aip/jcp/article/164/16/164101/123/Title",
        "document",
        issue_url,
        endpoint,
    )
    assert _browser_request_allowed(endpoint + "?articleId=123", "xhr", issue_url, endpoint)
    assert _browser_request_allowed(
        "https://pubs.aip.org/cdn-cgi/challenge-platform/script",
        "fetch",
        issue_url,
        endpoint,
    )
    assert not _browser_request_allowed(
        "https://pubs.aip.org/aip/jcp/other-api",
        "fetch",
        issue_url,
        endpoint,
    )


def test_aip_records_no_official_abstract_control(monkeypatch):
    from papertrack import aip_fetch

    aip_fetch.clear_aip_caches()
    html = _issue_html(_card("10.1063/no-control"))
    events = []
    monkeypatch.setattr(
        aip_fetch,
        "_fetch_issue_http",
        lambda *args, **kwargs: HttpFetchResult(html=html, status_code=200),
    )
    monkeypatch.setattr(
        aip_fetch,
        "record_fetch_attempt",
        lambda event, html=None: events.append(event),
    )

    result = aip_fetch.query_aip_issue_result("jcp", "164", "16", "JCP")

    assert result.entries["10.1063/no-control"].abstract == ""
    event = next(event for event in events if event.get("field") == "abstract")
    assert event["doi"] == "10.1063/no-control"
    assert event["article_id"] == ""
    assert event["url"] == "https://pubs.aip.org/aip/jcp/issue/164/16"
    assert event["outcome"] == "official_control_absent"

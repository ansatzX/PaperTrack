from papertrack.adapter_result import BLOCKED, FETCH_FAILED
from papertrack.http_fetch import (
    HttpRequestPolicy,
    fetch_html_resilient,
    is_blocked_response,
)


class Response:
    def __init__(self, text: str, status_code: int = 200, headers=None):
        self.text = text
        self.status_code = status_code
        self.headers = headers or {}


def test_fetch_html_resilient_retries_transient_status_with_next_strategy(monkeypatch):
    from papertrack import http_fetch

    calls = []

    def fake_fetch_once(client, variant, url, headers, timeout):
        calls.append((client, variant))
        if client == "curl_cffi":
            return Response("temporarily unavailable", 503)
        return Response('<html><div class="issue-item clearfix">Article</div></html>')

    monkeypatch.setattr(http_fetch, "_fetch_once", fake_fetch_once)

    result = fetch_html_resilient(
        "https://example.test/issue",
        strategies=(("curl_cffi", "chrome136"), ("requests", "default")),
        request_policy=HttpRequestPolicy(min_interval=0, max_attempts=2, backoff=(0,)),
        required_selector=".issue-item",
    )

    assert result.ok
    assert result.client == "requests"
    assert calls == [("curl_cffi", "chrome136"), ("requests", "default")]


def test_fetch_html_resilient_stops_immediately_on_challenge(monkeypatch):
    from papertrack import http_fetch

    calls = []

    def fake_fetch(*args, **kwargs):
        calls.append(args[:2])
        return Response("<html>Attention Required! cf-chl</html>", 403)

    monkeypatch.setattr(
        http_fetch,
        "_fetch_once",
        fake_fetch,
    )

    result = fetch_html_resilient(
        "https://example.test/issue",
        strategies=(("curl_cffi", "chrome136"), ("requests", "default")),
        request_policy=HttpRequestPolicy(min_interval=0, max_attempts=2),
    )

    assert result.outcome == BLOCKED
    assert not result.ok
    assert calls == [("curl_cffi", "chrome136")]


def test_fetch_html_resilient_never_exceeds_max_attempts(monkeypatch):
    from papertrack import http_fetch

    calls = []

    def fake_fetch(*args, **kwargs):
        calls.append(args[:2])
        return Response("temporarily unavailable", 503)

    monkeypatch.setattr(http_fetch, "_fetch_once", fake_fetch)
    result = fetch_html_resilient(
        "https://example.test/issue",
        strategies=(
            ("curl_cffi", "chrome136"),
            ("requests", "default"),
            ("curl_cffi", "firefox"),
        ),
        request_policy=HttpRequestPolicy(
            min_interval=0,
            max_attempts=2,
            backoff=(0,),
        ),
    )

    assert result.outcome == FETCH_FAILED
    assert calls == [
        ("curl_cffi", "chrome136"),
        ("requests", "default"),
    ]


def test_retry_after_overrides_policy_backoff(monkeypatch):
    from papertrack import http_fetch

    sleeps = []
    responses = iter([
        Response("rate limited", 429, {"Retry-After": "7"}),
        Response("<html>ok</html>"),
    ])
    monkeypatch.setattr(http_fetch, "_fetch_once", lambda *args, **kwargs: next(responses))
    monkeypatch.setattr(http_fetch.time, "sleep", sleeps.append)

    result = fetch_html_resilient(
        "https://example.test/issue",
        strategies=(("requests", "default"), ("requests", "default")),
        request_policy=HttpRequestPolicy(
            min_interval=0,
            max_attempts=2,
            backoff=(2,),
        ),
    )

    assert result.ok
    assert sleeps == [7.0]
    assert result.history[0]["retry_after"] == 7.0
    assert result.history[0]["retry_delay"] == 7.0


def test_provider_throttle_is_shared_across_fetch_calls(monkeypatch):
    from papertrack import http_fetch

    clock = [100.0]
    sleeps = []

    def fake_sleep(delay):
        sleeps.append(delay)
        clock[0] += delay

    monkeypatch.setattr(http_fetch.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(http_fetch.time, "sleep", fake_sleep)
    monkeypatch.setattr(
        http_fetch,
        "_fetch_once",
        lambda *args, **kwargs: Response("<html>ok</html>"),
    )
    http_fetch.clear_provider_throttles()
    policy = HttpRequestPolicy(min_interval=10, max_attempts=1)

    first = fetch_html_resilient(
        "https://example.test/issue/1",
        provider="aps",
        strategies=(("requests", "default"),),
        request_policy=policy,
    )
    second = fetch_html_resilient(
        "https://example.test/issue/2",
        provider="aps",
        strategies=(("requests", "default"),),
        request_policy=policy,
    )

    assert first.ok and second.ok
    assert sleeps == [10.0]
    assert second.history[0]["throttle_delay"] == 10.0


def test_block_detector_does_not_treat_plain_cloudflare_word_as_block():
    assert is_blocked_response("<html>Cloudflare CDN status page text</html>") is False


def test_configured_interval_enforces_floor():
    from papertrack import http_fetch

    assert http_fetch.configured_interval("PAPERTRACK_TEST_INTERVAL", 30, {}) == 30
    assert http_fetch.configured_interval(
        "PAPERTRACK_TEST_INTERVAL",
        30,
        {"PAPERTRACK_TEST_INTERVAL": "5"},
    ) == 30
    assert http_fetch.configured_interval(
        "PAPERTRACK_TEST_INTERVAL",
        30,
        {"PAPERTRACK_TEST_INTERVAL": "45"},
    ) == 45


def test_configured_interval_rejects_non_numeric_value():
    from papertrack import http_fetch

    try:
        http_fetch.configured_interval(
            "PAPERTRACK_TEST_INTERVAL",
            30,
            {"PAPERTRACK_TEST_INTERVAL": "fast"},
        )
    except RuntimeError as exc:
        assert str(exc) == "PAPERTRACK_TEST_INTERVAL must be a non-negative number"
    else:
        raise AssertionError("non-numeric interval was accepted")

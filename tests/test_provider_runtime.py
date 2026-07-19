import re
import stat

import pytest

from papertrack.adapter_result import BLOCKED, FETCH_FAILED, SUCCESS
from papertrack.provider_runtime import CIRCUIT_OPEN, IssueRequest, ProviderRuntime


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class FakeNavigationResponse:
    def __init__(self, status=200):
        self.status = status


class FakeApiResponse:
    def __init__(
        self,
        url,
        body=b'{"abstract": "official"}',
        media_type="application/json; charset=utf-8",
        resource_type="xhr",
        body_error=False,
    ):
        self.url = url
        self.status = 200
        self.headers = {"content-type": media_type}
        self._body = body
        self.body_calls = 0
        self.body_error = body_error
        self.request = type("Request", (), {"resource_type": resource_type})()

    def body(self):
        self.body_calls += 1
        if self.body_error:
            raise OSError("response body unavailable")
        return self._body


class FakeRequestContext:
    def __init__(self, browser):
        self.browser = browser
        self.calls = []

    def fetch(self, url, **kwargs):
        self.calls.append((url, kwargs))
        final_url = self.browser.explicit_response_url or url
        response = FakeApiResponse(
            final_url, body_error=self.browser.explicit_response_body_error
        )
        if self.browser.explicit_response_statuses:
            response.status = self.browser.explicit_response_statuses.pop(0)
        return response


class FakePage:
    def __init__(self, browser):
        self.browser = browser
        self.url = "about:blank"
        self._challenge = False
        self._response_handler = None

    def on(self, event, handler):
        assert event == "response"
        if self.browser.failure_stage == "event_registration":
            raise RuntimeError("event registration failed")
        self.browser.event_registered = True
        self._response_handler = handler

    def goto(self, url, **kwargs):
        self.browser.goto_calls.append((url, kwargs))
        self.url = self.browser.navigation_final_url or url
        self.browser.navigation_times.append(self.browser.clock.time())
        self._challenge = self.browser.challenge_pages > 0
        if self._challenge:
            self.browser.challenge_pages -= 1
        for response in self.browser.navigation_responses:
            self._response_handler(response)
        if self.browser.navigation_error_after_responses:
            raise RuntimeError("navigation failed after responses")
        return FakeNavigationResponse(self.browser.navigation_status)

    def reload(self, **kwargs):
        self.browser.reload_calls.append(kwargs)
        self.browser.reload_count += 1
        self.browser.navigation_times.append(self.browser.clock.time())
        self._challenge = self.browser.challenge_pages > 0
        if self._challenge:
            self.browser.challenge_pages -= 1
        return FakeNavigationResponse(self.browser.navigation_status)

    def wait_for_selector(self, selector):
        if self._challenge:
            raise RuntimeError("selector timed out")
        if self.browser.selector_failure:
            raise RuntimeError("selector timed out")

    def title(self):
        return "Just a moment..." if self._challenge else "Official issue"

    def content(self):
        return "<html>Cloudflare challenge</html>" if self._challenge else "<main class='issue'>papers</main>"


class FakeContext:
    def __init__(self, browser):
        self.browser = browser
        self.request = FakeRequestContext(browser)

    def new_page(self):
        if self.browser.failure_stage == "page_creation":
            raise RuntimeError("page creation failed")
        self.browser.page = FakePage(self.browser)
        self.browser.page_created = True
        return self.browser.page

    def close(self):
        self.browser.closed = True
        if self.browser.context_close_error:
            raise RuntimeError("context close failed")


class FakeChromium:
    def __init__(self, browser):
        self.browser = browser

    def launch_persistent_context(self, **kwargs):
        self.browser.persistent_context_launches += 1
        self.browser.launch_kwargs = kwargs
        if self.browser.failure_stage == "context_launch":
            raise RuntimeError("context launch failed")
        self.browser.context = FakeContext(self.browser)
        self.browser.context_created = True
        return self.browser.context


class FakePlaywright:
    def __init__(
        self,
        clock,
        challenge_pages=0,
        selector_failure=False,
        context_close_error=False,
        manager_close_error=False,
        failure_stage="",
    ):
        self.clock = clock
        self.challenge_pages = challenge_pages
        self.selector_failure = selector_failure
        self.persistent_context_launches = 0
        self.navigation_times = []
        self.goto_calls = []
        self.reload_calls = []
        self.reload_count = 0
        self.closed = False
        self.stopped = False
        self.launch_kwargs = {}
        self.navigation_final_url = ""
        self.navigation_status = 200
        self.explicit_response_url = ""
        self.explicit_response_statuses = []
        self.explicit_response_body_error = False
        self.navigation_responses = []
        self.navigation_error_after_responses = False
        self.context_close_error = context_close_error
        self.manager_close_error = manager_close_error
        self.failure_stage = failure_stage
        self.entered = False
        self.context_created = False
        self.page_created = False
        self.event_registered = False
        self.chromium = FakeChromium(self)

    def __enter__(self):
        self.entered = True
        if self.failure_stage == "playwright_enter":
            raise RuntimeError("playwright enter failed")
        return self

    def __exit__(self, *args):
        self.stopped = True
        if self.manager_close_error:
            raise RuntimeError("manager close failed")


def aps_request(index):
    return IssueRequest(
        canonical_url=f"https://journals.aps.org/pra/issues/1/{index + 1}",
        required_selector="main.issue",
        challenge_markers=("just a moment", "cloudflare"),
    )


def aps_request_with_capture(index):
    return IssueRequest(
        canonical_url=f"https://journals.aps.org/pra/issues/1/{index + 1}",
        required_selector="main.issue",
        challenge_markers=("just a moment", "cloudflare"),
        capture_url_patterns=(re.compile(r"/api/"),),
    )


def aip_request(index):
    return IssueRequest(
        canonical_url=f"https://pubs.aip.org/aip/jcp/issue/160/{index + 4}",
        required_selector="main.issue",
        challenge_markers=("just a moment", "cloudflare"),
        capture_url_patterns=(
            re.compile(r"/Handlers/ArticleAbstractHandler\.ashx"),
        ),
    )


def make_aip_runtime(tmp_path, fake, clock, **kwargs):
    options = {
        "playwright_factory": lambda: fake,
        "clock": clock.time,
        "sleep": clock.sleep,
        "issue_interval": 30.0,
        "challenge_retry_delay": 60.0,
        "max_consecutive_blocks": 3,
    }
    options.update(kwargs)
    return ProviderRuntime(
        provider="aip",
        profile_root=tmp_path / "profiles",
        snapshot_root=tmp_path / "snapshots",
        **options,
    )


def make_runtime(tmp_path, fake, clock, **kwargs):
    options = {
        "playwright_factory": lambda: fake,
        "clock": clock.time,
        "sleep": clock.sleep,
        "issue_interval": 30.0,
        "challenge_retry_delay": 60.0,
        "max_consecutive_blocks": 3,
    }
    options.update(kwargs)
    return ProviderRuntime(
        provider="aps",
        profile_root=tmp_path / "profiles",
        snapshot_root=tmp_path / "snapshots",
        **options,
    )


def test_runtime_reuses_context_and_opens_circuit_after_three_blocks(tmp_path):
    clock = FakeClock()
    fake = FakePlaywright(clock, challenge_pages=6)
    runtime = make_runtime(tmp_path, fake, clock)

    with runtime:
        outcomes = [runtime.fetch_issue(aps_request(index)).outcome for index in range(4)]

    assert outcomes == [BLOCKED, BLOCKED, BLOCKED, CIRCUIT_OPEN]
    assert fake.persistent_context_launches == 1
    assert clock.sleeps.count(60.0) == 3
    assert runtime.stats.blocked == 3
    assert runtime.stats.circuit_skipped == 1
    assert runtime.consecutive_blocks == 0
    assert fake.closed is True
    assert fake.stopped is True


def test_second_issue_navigation_obeys_provider_interval(tmp_path):
    clock = FakeClock()
    fake = FakePlaywright(clock)
    runtime = make_runtime(tmp_path, fake, clock)

    with runtime:
        assert runtime.fetch_issue(aps_request(0)).outcome == SUCCESS
        assert runtime.fetch_issue(aps_request(1)).outcome == SUCCESS

    assert fake.navigation_times == [0.0, 30.0]
    assert clock.sleeps == [30.0]


def test_issue_navigation_waits_only_for_dom_content_before_selector(tmp_path):
    clock = FakeClock()
    fake = FakePlaywright(clock)
    runtime = make_runtime(tmp_path, fake, clock)

    with runtime:
        assert runtime.fetch_issue(aps_request(0)).outcome == SUCCESS

    assert fake.goto_calls == [
        (
            "https://journals.aps.org/pra/issues/1/1",
            {"wait_until": "domcontentloaded"},
        )
    ]


def test_challenge_reload_waits_only_for_dom_content_before_selector(tmp_path):
    clock = FakeClock()
    fake = FakePlaywright(clock, challenge_pages=1)
    runtime = make_runtime(tmp_path, fake, clock)

    with runtime:
        assert runtime.fetch_issue(aps_request(0)).outcome == SUCCESS

    assert fake.reload_calls == [{"wait_until": "domcontentloaded"}]


def test_successful_issue_resets_consecutive_blocks(tmp_path):
    clock = FakeClock()
    fake = FakePlaywright(clock, challenge_pages=2)
    runtime = make_runtime(tmp_path, fake, clock)

    with runtime:
        assert runtime.fetch_issue(aps_request(0)).outcome == BLOCKED
        assert runtime.consecutive_blocks == 1
        assert runtime.fetch_issue(aps_request(1)).outcome == SUCCESS

    assert runtime.consecutive_blocks == 0


def test_profile_directory_is_private(tmp_path):
    clock = FakeClock()
    fake = FakePlaywright(clock)
    runtime = make_runtime(tmp_path, fake, clock)

    with runtime:
        profile_dir = runtime.profile_dir

    assert stat.S_IMODE(profile_dir.stat().st_mode) == 0o700


def test_profile_lock_rejects_concurrent_owner(tmp_path):
    clock = FakeClock()
    first_fake = FakePlaywright(clock)
    second_fake = FakePlaywright(clock)
    first = make_runtime(tmp_path, first_fake, clock)
    second = make_runtime(tmp_path, second_fake, clock)

    with first:
        with pytest.raises(RuntimeError, match="^provider profile is already owned$"):
            second.open()

    assert second_fake.persistent_context_launches == 0


@pytest.mark.parametrize(
    "url",
    [
        "http://journals.aps.org/pra/issues/1/1",
        "https://example.test/pra/issues/1/1",
        "https://user:secret@journals.aps.org/pra/issues/1/1",
        "https://journals.aps.org/pra/issues/1/1#papers",
        "https://journals.aps.org/pra/article/1/1",
    ],
)
def test_issue_url_policy_rejects_before_navigation(tmp_path, url):
    clock = FakeClock()
    fake = FakePlaywright(clock)
    runtime = make_runtime(tmp_path, fake, clock)
    request = IssueRequest(url, "main.issue", ("cloudflare",))

    with runtime:
        with pytest.raises(ValueError, match="official issue URL"):
            runtime.fetch_issue(request)

    assert fake.navigation_times == []


def test_issue_navigation_rejects_same_origin_non_issue_redirect(tmp_path):
    clock = FakeClock()
    fake = FakePlaywright(clock)
    fake.navigation_final_url = "https://journals.aps.org/pra/articles/1/1"
    runtime = make_runtime(tmp_path, fake, clock)

    with runtime:
        result = runtime.fetch_issue(aps_request(0))

    assert result.outcome == FETCH_FAILED
    assert result.message == "issue navigation changed the canonical issue location"


@pytest.mark.parametrize(
    "canonical_url",
    [
        "https://journals.aps.org:443/pra/issues/1/1",
        "https://journals.aps.org/pra/issues/1/1?",
    ],
)
def test_issue_request_requires_exact_canonical_representation(
    tmp_path, canonical_url
):
    clock = FakeClock()
    fake = FakePlaywright(clock)
    runtime = make_runtime(tmp_path, fake, clock)

    with runtime:
        with pytest.raises(ValueError, match="official issue URL"):
            runtime.fetch_issue(
                IssueRequest(canonical_url, "main.issue", ("cloudflare",))
            )

    assert fake.navigation_times == []


@pytest.mark.parametrize("status", [403, 500])
def test_issue_navigation_rejects_http_error_with_valid_selector(tmp_path, status):
    clock = FakeClock()
    fake = FakePlaywright(clock)
    fake.navigation_status = status
    fake.navigation_responses = [
        FakeApiResponse("https://journals.aps.org/api/http-error")
    ]
    runtime = make_runtime(tmp_path, fake, clock)

    with runtime:
        result = runtime.fetch_issue(aps_request_with_capture(0))

    assert result.outcome == FETCH_FAILED
    assert result.status_code == status
    assert result.html == ""
    assert result.captured == ()
    assert runtime.captured_responses == ()


@pytest.mark.parametrize(
    "final_url",
    [
        "http://journals.aps.org/pra/issues/1/1",
        "https://user:secret@journals.aps.org/pra/issues/1/1",
        "https://journals.aps.org:444/pra/issues/1/1",
        "https://pubs.acs.org/pra/issues/1/1",
        "https://journals.aps.org/pra/issues/1/2",
        "https://journals.aps.org/pra/issues/1/1?view=expanded",
        "https://journals.aps.org/pra/issues/1/1#papers",
        "https://journals.aps.org:443/pra/issues/1/1",
        "https://journals.aps.org/pra/issues/1/1?",
    ],
)
def test_issue_navigation_rejects_noncanonical_final_url_before_content_exposure(
    tmp_path, final_url
):
    clock = FakeClock()
    fake = FakePlaywright(clock)
    fake.navigation_final_url = final_url
    fake.navigation_responses = [
        FakeApiResponse("https://journals.aps.org/api/noncanonical")
    ]
    runtime = make_runtime(tmp_path, fake, clock)

    with runtime:
        result = runtime.fetch_issue(aps_request_with_capture(0))

    assert result.outcome == FETCH_FAILED
    assert result.html == ""
    assert result.captured == ()
    assert runtime.captured_responses == ()


def test_selector_failure_is_sanitized_but_diagnostic_keeps_raw_material(
    tmp_path, monkeypatch
):
    clock = FakeClock()
    fake = FakePlaywright(clock, selector_failure=True)
    fake.navigation_responses = [
        FakeApiResponse(
            "https://journals.aps.org/api/issue",
            body=b'{"papers": ["evidence"]}',
        )
    ]
    runtime = make_runtime(tmp_path, fake, clock)
    events = []
    monkeypatch.setattr(
        "papertrack.provider_runtime.record_fetch_attempt",
        lambda event, html=None: events.append((event, html)),
    )

    with runtime:
        result = runtime.fetch_issue(aps_request_with_capture(0))

    assert result.outcome == FETCH_FAILED
    assert result.html == ""
    assert result.captured == ()
    assert runtime.captured_responses == ()
    event, diagnostic_html = events[-1]
    assert diagnostic_html == "<main class='issue'>papers</main>"
    assert event["captured_response_evidence"][0]["url"] == (
        "https://journals.aps.org/api/issue"
    )


def test_blocked_result_is_sanitized_but_diagnostic_keeps_challenge(
    tmp_path, monkeypatch
):
    clock = FakeClock()
    fake = FakePlaywright(clock, challenge_pages=2)
    fake.navigation_responses = [
        FakeApiResponse(
            "https://journals.aps.org/api/blocked",
            body=b'{"challenge": "raw"}',
        )
    ]
    runtime = make_runtime(tmp_path, fake, clock)
    events = []
    monkeypatch.setattr(
        "papertrack.provider_runtime.record_fetch_attempt",
        lambda event, html=None: events.append((event, html)),
    )

    with runtime:
        result = runtime.fetch_issue(aps_request_with_capture(0))

    assert result.outcome == BLOCKED
    assert result.html == ""
    assert result.captured == ()
    assert runtime.captured_responses == ()
    event, diagnostic_html = events[-1]
    assert "Cloudflare challenge" in diagnostic_html
    assert event["challenge_indicators"]


def test_transport_success_does_not_claim_adapter_live_success(tmp_path):
    clock = FakeClock()
    fake = FakePlaywright(clock)
    runtime = make_runtime(tmp_path, fake, clock)

    with runtime:
        result = runtime.fetch_issue(aps_request(0))

    assert result.outcome == SUCCESS
    assert runtime.stats.live_success == 0


def test_same_origin_request_is_paced_and_returns_captured_response(tmp_path):
    clock = FakeClock()
    fake = FakePlaywright(clock)
    runtime = make_runtime(tmp_path, fake, clock)

    with runtime:
        runtime.fetch_issue(aps_request(0))
        first = runtime.request_same_origin(
            "https://journals.aps.org/api/abstract",
            method="POST",
            form={"id": "one"},
            min_interval=10.0,
        )
        second = runtime.request_same_origin(
            "https://journals.aps.org/api/abstract",
            method="POST",
            form={"id": "two"},
            min_interval=10.0,
        )

    assert first.body == b'{"abstract": "official"}'
    assert first.media_type == "application/json"
    assert second.status_code == 200
    assert clock.sleeps == [10.0]


@pytest.mark.parametrize(
    ("statuses", "blocked", "rate_limited"),
    [
        ([403, 403, 403], 3, 0),
        ([429, 429, 429], 0, 3),
        ([403, 429, 403], 2, 1),
    ],
)
def test_same_origin_denials_open_provider_circuit_after_three_contacts(
    tmp_path, statuses, blocked, rate_limited
):
    clock = FakeClock()
    fake = FakePlaywright(clock)
    fake.explicit_response_statuses = list(statuses)
    runtime = make_runtime(tmp_path, fake, clock)

    with runtime:
        assert runtime.fetch_issue(aps_request(0)).outcome == SUCCESS
        for article_id in range(3):
            runtime.request_same_origin(
                "https://journals.aps.org/api/abstract",
                "POST",
                {"id": str(article_id)},
                10,
            )
        assert runtime.circuit_open is True
        with pytest.raises(RuntimeError, match="provider circuit is open"):
            runtime.request_same_origin(
                "https://journals.aps.org/api/abstract",
                "POST",
                {"id": "must-not-contact"},
                10,
            )
        next_issue = runtime.fetch_issue(aps_request(1))

    assert next_issue.outcome == CIRCUIT_OPEN
    assert len(fake.context.request.calls) == 3
    assert len(fake.goto_calls) == 1
    assert runtime.stats.blocked == blocked
    assert runtime.stats.rate_limited == rate_limited
    assert runtime.stats.circuit_skipped == 1


def test_same_origin_http_200_resets_consecutive_endpoint_denials(tmp_path):
    clock = FakeClock()
    fake = FakePlaywright(clock)
    fake.explicit_response_statuses = [403, 429, 200, 403, 429, 429]
    runtime = make_runtime(tmp_path, fake, clock)

    with runtime:
        assert runtime.fetch_issue(aps_request(0)).outcome == SUCCESS
        observed_counts = []
        for article_id in range(6):
            runtime.request_same_origin(
                "https://journals.aps.org/api/abstract",
                "POST",
                {"id": str(article_id)},
                10,
            )
            observed_counts.append(runtime.consecutive_endpoint_denials)

    assert observed_counts == [1, 2, 0, 1, 2, 3]
    assert runtime.circuit_open is True


def test_successful_next_issue_does_not_reset_endpoint_denial_streak(tmp_path):
    clock = FakeClock()
    fake = FakePlaywright(clock)
    fake.explicit_response_statuses = [403, 429, 403]
    runtime = make_runtime(tmp_path, fake, clock)

    with runtime:
        assert runtime.fetch_issue(aps_request(0)).outcome == SUCCESS
        for article_id in range(2):
            runtime.request_same_origin(
                "https://journals.aps.org/api/abstract",
                "POST",
                {"id": str(article_id)},
                10,
            )
        assert runtime.consecutive_endpoint_denials == 2
        assert runtime.fetch_issue(aps_request(1)).outcome == SUCCESS
        assert runtime.consecutive_endpoint_denials == 2
        runtime.request_same_origin(
            "https://journals.aps.org/api/abstract",
            "POST",
            {"id": "third"},
            10,
        )

    assert runtime.consecutive_endpoint_denials == 3
    assert runtime.circuit_open is True


@pytest.mark.parametrize(
    ("status", "blocked", "rate_limited"),
    [(403, 1, 0), (429, 0, 1)],
)
def test_endpoint_denial_returns_bodyless_evidence_when_body_read_fails(
    tmp_path, status, blocked, rate_limited
):
    clock = FakeClock()
    fake = FakePlaywright(clock)
    fake.explicit_response_statuses = [status]
    fake.explicit_response_body_error = True
    runtime = make_runtime(tmp_path, fake, clock)

    with runtime:
        assert runtime.fetch_issue(aps_request(0)).outcome == SUCCESS
        response = runtime.request_same_origin(
            "https://journals.aps.org/api/abstract",
            "POST",
            {"id": "one"},
            10,
        )

    assert response.status_code == status
    assert response.body == b""
    assert runtime.consecutive_endpoint_denials == 1
    assert runtime.stats.blocked == blocked
    assert runtime.stats.rate_limited == rate_limited


def test_selected_captured_endpoint_denials_open_circuit_exactly_once(tmp_path):
    clock = FakeClock()
    fake = FakePlaywright(clock)
    statuses = [403, 429, 403]
    for index, status in enumerate(statuses):
        response = FakeApiResponse(
            "https://pubs.aip.org/Handlers/ArticleAbstractHandler.ashx"
            f"?articleId={10000 + index}",
            body=b"denied",
            media_type="text/html",
        )
        response.status = status
        fake.navigation_responses.append(response)
    runtime = make_aip_runtime(tmp_path, fake, clock)

    with runtime:
        first = runtime.fetch_issue(aip_request(0))
        second = runtime.fetch_issue(aip_request(1))

    assert first.outcome == SUCCESS
    assert [response.status_code for response in first.captured] == statuses
    assert runtime.consecutive_endpoint_denials == 3
    assert runtime.stats.blocked == 2
    assert runtime.stats.rate_limited == 1
    assert second.outcome == CIRCUIT_OPEN
    assert len(fake.goto_calls) == 1
    assert fake.context.request.calls == []


def test_selected_captured_http_200_resets_prior_explicit_denial_streak(tmp_path):
    clock = FakeClock()
    fake = FakePlaywright(clock)
    runtime = make_aip_runtime(tmp_path, fake, clock)

    with runtime:
        assert runtime.fetch_issue(aip_request(0)).outcome == SUCCESS
        fake.explicit_response_statuses = [403, 429]
        for article_id in ("one", "two"):
            runtime.request_same_origin(
                "https://pubs.aip.org/Handlers/ArticleAbstractHandler.ashx",
                "POST",
                {"articleId": article_id},
                10,
            )
        assert runtime.consecutive_endpoint_denials == 2
        captured = FakeApiResponse(
            "https://pubs.aip.org/Handlers/ArticleAbstractHandler.ashx"
            "?articleId=three"
        )
        fake.navigation_responses = [captured]
        assert runtime.fetch_issue(aip_request(1)).outcome == SUCCESS

    assert runtime.consecutive_endpoint_denials == 0
    assert runtime.circuit_open is False
    assert runtime.stats.blocked == 1
    assert runtime.stats.rate_limited == 1
    assert len(fake.context.request.calls) == 2


def test_same_origin_request_rejects_other_origin(tmp_path):
    clock = FakeClock()
    fake = FakePlaywright(clock)
    runtime = make_runtime(tmp_path, fake, clock)

    with runtime:
        runtime.fetch_issue(aps_request(0))
        with pytest.raises(ValueError, match="same canonical issue origin"):
            runtime.request_same_origin(
                "https://pubs.acs.org/api/abstract", "GET", None, 0
            )


def test_same_origin_request_requires_successful_issue_load(tmp_path):
    clock = FakeClock()
    fake = FakePlaywright(clock, selector_failure=True)
    runtime = make_runtime(tmp_path, fake, clock)

    with runtime:
        assert runtime.fetch_issue(aps_request(0)).outcome == FETCH_FAILED
        with pytest.raises(
            RuntimeError, match="canonical issue origin has not been established"
        ):
            runtime.request_same_origin(
                "https://journals.aps.org/api/abstract", "GET", None, 10
            )


@pytest.mark.parametrize(
    "final_url",
    [
        "https://pubs.acs.org/api/abstract",
        "http://journals.aps.org/api/abstract",
    ],
)
def test_same_origin_request_rejects_redirected_response(tmp_path, final_url):
    clock = FakeClock()
    fake = FakePlaywright(clock)
    fake.explicit_response_url = final_url
    runtime = make_runtime(tmp_path, fake, clock)

    with runtime:
        assert runtime.fetch_issue(aps_request(0)).outcome == SUCCESS
        with pytest.raises(ValueError, match="response must remain on the exact origin"):
            runtime.request_same_origin(
                "https://journals.aps.org/api/abstract", "GET", None, 10
            )


def test_non_challenge_selector_failure_is_fetch_failed(tmp_path):
    clock = FakeClock()
    fake = FakePlaywright(clock, selector_failure=True)
    runtime = make_runtime(tmp_path, fake, clock)

    with runtime:
        result = runtime.fetch_issue(aps_request(0))

    assert result.outcome == FETCH_FAILED
    assert runtime.stats.fetch_failed == 1
    assert result.html == ""
    assert result.captured == ()


def test_fetch_failure_resets_consecutive_blocks(tmp_path):
    clock = FakeClock()
    fake = FakePlaywright(clock, challenge_pages=2)
    runtime = make_runtime(tmp_path, fake, clock)

    with runtime:
        assert runtime.fetch_issue(aps_request(0)).outcome == BLOCKED
        fake.selector_failure = True
        assert runtime.fetch_issue(aps_request(1)).outcome == FETCH_FAILED

    assert runtime.consecutive_blocks == 0


def test_reload_becomes_pacing_origin_for_next_issue(tmp_path):
    clock = FakeClock()
    fake = FakePlaywright(clock, challenge_pages=1)
    runtime = make_runtime(tmp_path, fake, clock)

    with runtime:
        assert runtime.fetch_issue(aps_request(0)).outcome == SUCCESS
        assert runtime.fetch_issue(aps_request(1)).outcome == SUCCESS

    assert fake.navigation_times == [0.0, 60.0, 90.0]


@pytest.mark.parametrize(
    ("issue_interval", "challenge_retry_delay", "message"),
    [
        (29.9, 60.0, "issue_interval must be at least 30 seconds"),
        (30.0, 59.9, "challenge_retry_delay must be at least 60 seconds"),
    ],
)
def test_runtime_enforces_contact_interval_floors(
    tmp_path, issue_interval, challenge_retry_delay, message
):
    clock = FakeClock()
    fake = FakePlaywright(clock)

    with pytest.raises(ValueError, match=message):
        ProviderRuntime(
            provider="aps",
            profile_root=tmp_path / "profiles",
            snapshot_root=tmp_path / "snapshots",
            playwright_factory=lambda: fake,
            clock=clock.time,
            sleep=clock.sleep,
            issue_interval=issue_interval,
            challenge_retry_delay=challenge_retry_delay,
        )


def test_close_attempts_all_cleanup_after_failures(tmp_path):
    clock = FakeClock()
    fake = FakePlaywright(
        clock, context_close_error=True, manager_close_error=True
    )
    runtime = make_runtime(tmp_path, fake, clock)
    runtime.open()

    with pytest.raises(RuntimeError, match="manager close failed"):
        runtime.close()

    assert fake.closed is True
    assert fake.stopped is True
    replacement_fake = FakePlaywright(clock)
    replacement = make_runtime(tmp_path, replacement_fake, clock)
    with replacement:
        pass
    assert replacement_fake.persistent_context_launches == 1


@pytest.mark.parametrize(
    ("failure_stage", "expected_context_close"),
    [
        ("playwright_enter", False),
        ("context_launch", False),
        ("page_creation", True),
        ("event_registration", True),
    ],
)
def test_partial_open_failure_releases_all_acquired_resources(
    tmp_path, failure_stage, expected_context_close
):
    clock = FakeClock()
    fake = FakePlaywright(clock, failure_stage=failure_stage)
    runtime = make_runtime(tmp_path, fake, clock)

    with pytest.raises(RuntimeError, match=failure_stage.replace("_", " ")):
        runtime.open()

    assert fake.closed is expected_context_close
    assert fake.stopped is True
    replacement_fake = FakePlaywright(clock)
    with make_runtime(tmp_path, replacement_fake, clock):
        pass
    assert replacement_fake.persistent_context_launches == 1


def test_playwright_factory_failure_releases_profile_lock(tmp_path):
    clock = FakeClock()

    def fail_factory():
        raise RuntimeError("playwright factory failed")

    runtime = make_runtime(tmp_path, FakePlaywright(clock), clock)
    runtime._playwright_factory = fail_factory
    with pytest.raises(RuntimeError, match="playwright factory failed"):
        runtime.open()

    replacement_fake = FakePlaywright(clock)
    with make_runtime(tmp_path, replacement_fake, clock):
        pass
    assert replacement_fake.persistent_context_launches == 1


def test_capture_patterns_admit_only_selected_safe_response_bodies(tmp_path):
    clock = FakeClock()
    fake = FakePlaywright(clock)
    runtime = make_runtime(tmp_path, fake, clock)
    request = IssueRequest(
        canonical_url="https://journals.aps.org/pra/issues/1/1",
        required_selector="main.issue",
        challenge_markers=("cloudflare",),
        capture_url_patterns=(re.compile(r"/api/selected$"),),
    )
    selected = FakeApiResponse("https://journals.aps.org/api/selected")
    unselected = FakeApiResponse("https://journals.aps.org/api/other")
    fake.navigation_responses = [selected, unselected]

    with runtime:
        result = runtime.fetch_issue(request)

    assert [item.url for item in result.captured] == [selected.url]
    assert [item.url for item in runtime.captured_responses] == [selected.url]
    assert [item["url"] for item in runtime._response_evidence] == [
        selected.url,
        unselected.url,
    ]


def test_navigation_exception_clears_public_captured_response_bodies(
    tmp_path, monkeypatch
):
    clock = FakeClock()
    fake = FakePlaywright(clock)
    captured = FakeApiResponse(
        "https://journals.aps.org/api/selected",
        body=b'{"papers": ["raw"]}',
    )
    fake.navigation_responses = [captured]
    fake.navigation_error_after_responses = True
    runtime = make_runtime(tmp_path, fake, clock)
    events = []
    monkeypatch.setattr(
        "papertrack.provider_runtime.record_fetch_attempt",
        lambda event, html=None: events.append((event, html)),
    )
    request = IssueRequest(
        canonical_url="https://journals.aps.org/pra/issues/1/1",
        required_selector="main.issue",
        challenge_markers=("cloudflare",),
        capture_url_patterns=(re.compile(r"/api/selected$"),),
    )

    with runtime:
        result = runtime.fetch_issue(request)

    assert result.outcome == FETCH_FAILED
    assert result.captured == ()
    assert runtime.captured_responses == ()
    evidence = events[-1][0]["captured_response_evidence"]
    assert evidence[0]["url"] == captured.url
    assert "body" not in evidence[0]


def test_capture_filters_size_type_origin_and_resource_role(tmp_path):
    clock = FakeClock()
    fake = FakePlaywright(clock)
    accepted = FakeApiResponse("https://journals.aps.org/api/accepted")
    retained_at_limit = FakeApiResponse(
        "https://journals.aps.org/api/at-limit", body=b"x" * (10 * 1024 * 1024)
    )
    over_retention_limit = FakeApiResponse(
        "https://journals.aps.org/api/large",
        body=b"x" * (10 * 1024 * 1024 + 1),
    )
    fake.navigation_responses = [
        accepted,
        retained_at_limit,
        over_retention_limit,
        FakeApiResponse(
            "https://journals.aps.org/api/plain", media_type="text/plain"
        ),
        FakeApiResponse("https://pubs.acs.org/api/foreign"),
        FakeApiResponse(
            "https://journals.aps.org/api/document", resource_type="document"
        ),
    ]
    runtime = make_runtime(tmp_path, fake, clock)
    request = IssueRequest(
        canonical_url="https://journals.aps.org/pra/issues/1/1",
        required_selector="main.issue",
        challenge_markers=("cloudflare",),
        capture_url_patterns=(re.compile(r"/api/"),),
    )

    with runtime:
        result = runtime.fetch_issue(request)

    assert [item.url for item in result.captured] == [
        accepted.url,
        retained_at_limit.url,
    ]
    assert [item["url"] for item in runtime._response_evidence] == [
        accepted.url,
        retained_at_limit.url,
    ]
    assert over_retention_limit.body_calls == 1


def test_circuit_skip_diagnostics_do_not_reuse_previous_capture_evidence(
    tmp_path, monkeypatch
):
    from papertrack import provider_runtime

    events = []
    monkeypatch.setattr(
        provider_runtime,
        "record_fetch_attempt",
        lambda event, html=None: events.append(event),
    )
    clock = FakeClock()
    fake = FakePlaywright(clock, challenge_pages=6)
    fake.navigation_responses = [
        FakeApiResponse("https://journals.aps.org/api/evidence")
    ]
    runtime = make_runtime(tmp_path, fake, clock)

    with runtime:
        for index in range(3):
            assert (
                runtime.fetch_issue(aps_request_with_capture(index)).outcome
                == BLOCKED
            )
            assert runtime.captured_responses == ()
        assert runtime.fetch_issue(aps_request_with_capture(3)).outcome == CIRCUIT_OPEN
        assert runtime.captured_responses == ()

    assert events[-1]["captured_response_evidence"] == []


def test_failure_diagnostics_include_issue_runtime_and_circuit_context(
    tmp_path, monkeypatch
):
    from papertrack import provider_runtime

    events = []
    monkeypatch.setattr(
        provider_runtime,
        "record_fetch_attempt",
        lambda event, html=None: events.append(event),
    )
    clock = FakeClock()
    blocked_fake = FakePlaywright(clock, challenge_pages=2)
    blocked_runtime = make_runtime(
        tmp_path,
        blocked_fake,
        clock,
        max_consecutive_blocks=1,
    )
    with blocked_runtime:
        assert blocked_runtime.fetch_issue(aps_request(0)).outcome == BLOCKED
        assert blocked_runtime.fetch_issue(aps_request(1)).outcome == CIRCUIT_OPEN

    failed_fake = FakePlaywright(clock, selector_failure=True)
    with make_runtime(tmp_path, failed_fake, clock) as failed_runtime:
        assert failed_runtime.fetch_issue(aps_request(2)).outcome == FETCH_FAILED

    blocked_event, circuit_event, failed_event = events
    common = {
        "source_role": "authoritative",
        "source": "journal",
        "provider": "aps",
        "stage": "issue_page_browser",
        "client": "playwright",
        "browser_mode": "headed_chrome",
        "profile_mode": "persistent",
    }
    for event in events:
        assert {key: event[key] for key in common} == common
        assert event["journal"] == "pra"
        assert event["volume"] == "1"

    assert blocked_event["issue"] == "1"
    assert blocked_event["url"].endswith("/pra/issues/1/1")
    assert blocked_event["http_status"] == 200
    assert blocked_event["message"]
    assert blocked_event["challenge_indicators"] == [
        "just a moment",
        "cloudflare",
    ]
    assert blocked_event["attempt"] == 2
    assert blocked_event["attempt_count"] == 2
    assert blocked_event["circuit_state"] == "open"

    assert circuit_event["issue"] == "2"
    assert circuit_event["challenge_indicators"] == []
    assert circuit_event["attempt"] == 0
    assert circuit_event["circuit_state"] == "open"

    assert failed_event["issue"] == "3"
    assert failed_event["challenge_indicators"] == []
    assert failed_event["attempt"] == 1
    assert failed_event["circuit_state"] == "closed"

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
    status = 200


class FakeApiResponse:
    def __init__(
        self,
        url,
        body=b'{"abstract": "official"}',
        media_type="application/json; charset=utf-8",
        resource_type="xhr",
    ):
        self.url = url
        self.status = 200
        self.headers = {"content-type": media_type}
        self._body = body
        self.request = type("Request", (), {"resource_type": resource_type})()

    def body(self):
        return self._body


class FakeRequestContext:
    def __init__(self, browser):
        self.browser = browser
        self.calls = []

    def fetch(self, url, **kwargs):
        self.calls.append((url, kwargs))
        final_url = self.browser.explicit_response_url or url
        return FakeApiResponse(final_url)


class FakePage:
    def __init__(self, browser):
        self.browser = browser
        self.url = "about:blank"
        self._challenge = False
        self._response_handler = None

    def on(self, event, handler):
        assert event == "response"
        self._response_handler = handler

    def goto(self, url):
        self.url = self.browser.navigation_final_url or url
        self.browser.navigation_times.append(self.browser.clock.time())
        self._challenge = self.browser.challenge_pages > 0
        if self._challenge:
            self.browser.challenge_pages -= 1
        for response in self.browser.navigation_responses:
            self._response_handler(response)
        return FakeNavigationResponse()

    def reload(self):
        self.browser.reload_count += 1
        self.browser.navigation_times.append(self.browser.clock.time())
        self._challenge = self.browser.challenge_pages > 0
        if self._challenge:
            self.browser.challenge_pages -= 1
        return FakeNavigationResponse()

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
        self.browser.page = FakePage(self.browser)
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
        self.browser.context = FakeContext(self.browser)
        return self.browser.context


class FakePlaywright:
    def __init__(
        self,
        clock,
        challenge_pages=0,
        selector_failure=False,
        context_close_error=False,
        manager_close_error=False,
    ):
        self.clock = clock
        self.challenge_pages = challenge_pages
        self.selector_failure = selector_failure
        self.persistent_context_launches = 0
        self.navigation_times = []
        self.reload_count = 0
        self.closed = False
        self.stopped = False
        self.launch_kwargs = {}
        self.navigation_final_url = ""
        self.explicit_response_url = ""
        self.navigation_responses = []
        self.context_close_error = context_close_error
        self.manager_close_error = manager_close_error
        self.chromium = FakeChromium(self)

    def __enter__(self):
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


def make_runtime(tmp_path, fake, clock, **kwargs):
    return ProviderRuntime(
        provider="aps",
        profile_root=tmp_path / "profiles",
        snapshot_root=tmp_path / "snapshots",
        playwright_factory=lambda: fake,
        clock=clock.time,
        sleep=clock.sleep,
        issue_interval=30.0,
        challenge_retry_delay=60.0,
        max_consecutive_blocks=3,
        **kwargs,
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
    assert result.html == "<main class='issue'>papers</main>"


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
    assert [item["url"] for item in runtime._response_evidence] == [
        selected.url,
        unselected.url,
    ]


def test_capture_filters_size_type_origin_and_resource_role(tmp_path):
    clock = FakeClock()
    fake = FakePlaywright(clock)
    accepted = FakeApiResponse("https://journals.aps.org/api/accepted")
    fake.navigation_responses = [
        accepted,
        FakeApiResponse(
            "https://journals.aps.org/api/large", body=b"x" * (10 * 1024 * 1024 + 1)
        ),
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

    assert [item.url for item in result.captured] == [accepted.url]
    assert [item["url"] for item in runtime._response_evidence] == [accepted.url]


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
            assert runtime.fetch_issue(aps_request(index)).outcome == BLOCKED
        assert runtime.fetch_issue(aps_request(3)).outcome == CIRCUIT_OPEN

    assert events[-1]["captured_response_evidence"] == []

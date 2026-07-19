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
    def __init__(self, url, body=b'{"abstract": "official"}'):
        self.url = url
        self.status = 200
        self.headers = {"content-type": "application/json; charset=utf-8"}
        self._body = body

    def body(self):
        return self._body


class FakeRequestContext:
    def __init__(self):
        self.calls = []

    def fetch(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeApiResponse(url)


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
        self.url = url
        self.browser.navigation_times.append(self.browser.clock.time())
        self._challenge = self.browser.challenge_pages > 0
        if self._challenge:
            self.browser.challenge_pages -= 1
        return FakeNavigationResponse()

    def reload(self):
        self.browser.reload_count += 1
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
        self.request = FakeRequestContext()

    def new_page(self):
        self.browser.page = FakePage(self.browser)
        return self.browser.page

    def close(self):
        self.browser.closed = True


class FakeChromium:
    def __init__(self, browser):
        self.browser = browser

    def launch_persistent_context(self, **kwargs):
        self.browser.persistent_context_launches += 1
        self.browser.launch_kwargs = kwargs
        self.browser.context = FakeContext(self.browser)
        return self.browser.context


class FakePlaywright:
    def __init__(self, clock, challenge_pages=0, selector_failure=False):
        self.clock = clock
        self.challenge_pages = challenge_pages
        self.selector_failure = selector_failure
        self.persistent_context_launches = 0
        self.navigation_times = []
        self.reload_count = 0
        self.closed = False
        self.stopped = False
        self.launch_kwargs = {}
        self.chromium = FakeChromium(self)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.stopped = True


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


def test_non_challenge_selector_failure_is_fetch_failed(tmp_path):
    clock = FakeClock()
    fake = FakePlaywright(clock, selector_failure=True)
    runtime = make_runtime(tmp_path, fake, clock)

    with runtime:
        result = runtime.fetch_issue(aps_request(0))

    assert result.outcome == FETCH_FAILED
    assert runtime.stats.fetch_failed == 1
    assert result.html == "<main class='issue'>papers</main>"


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

    with runtime:
        result = runtime.fetch_issue(request)
        page = fake.page
        selected = FakeApiResponse("https://journals.aps.org/api/selected")
        selected.request = type("Request", (), {"resource_type": "xhr"})()
        ignored = FakeApiResponse("https://journals.aps.org/api/other")
        ignored.request = type("Request", (), {"resource_type": "fetch"})()
        page._response_handler(selected)
        page._response_handler(ignored)
        captured = runtime.captured_responses

    assert result.captured == ()
    assert [item.url for item in captured] == [selected.url]

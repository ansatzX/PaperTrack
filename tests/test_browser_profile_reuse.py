from papertrack.provider_runtime import ProviderRuntime


class _Context:
    pages = []

    def new_page(self):
        return _Page()

    def close(self):
        pass


class _Page:
    def on(self, *_args):
        pass


class _Chromium:
    def __init__(self):
        self.user_data_dir = None

    def launch_persistent_context(self, **kwargs):
        self.user_data_dir = kwargs["user_data_dir"]
        return _Context()

    def connect_over_cdp(self, url):
        self.cdp_url = url
        return type("Browser", (), {"contexts": [_Context()]})()


class _Playwright:
    def __init__(self):
        self.chromium = _Chromium()


def test_browser_user_data_dir_is_distinct_from_papertrack_lock(tmp_path):
    playwright = _Playwright()
    user_profile = tmp_path / "real-chrome-profile"
    runtime = ProviderRuntime(
        "aps", tmp_path / "locks", tmp_path / "snapshots",
        browser_user_data_dir=user_profile,
        playwright_factory=lambda: playwright,
    )

    with runtime:
        assert playwright.chromium.user_data_dir == str(user_profile)
        assert runtime.profile_dir == tmp_path / "locks" / "aps"
        assert runtime.profile_dir != user_profile


def test_cdp_attach_does_not_launch_or_close_user_context(tmp_path):
    playwright = _Playwright()
    runtime = ProviderRuntime(
        "aps", tmp_path / "locks", tmp_path / "snapshots",
        browser_cdp_url="http://127.0.0.1:9222",
        playwright_factory=lambda: playwright,
    )

    with runtime:
        assert playwright.chromium.cdp_url == "http://127.0.0.1:9222"
        assert playwright.chromium.user_data_dir is None

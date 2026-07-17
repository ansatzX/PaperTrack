from papertrack.adapter_result import BLOCKED
from papertrack.http_fetch import fetch_html_resilient, is_blocked_response


class Response:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code


def test_fetch_html_resilient_tries_next_http_client(monkeypatch):
    from papertrack import http_fetch

    calls = []

    def fake_fetch_once(client, variant, url, headers, timeout):
        calls.append((client, variant))
        if client == "curl_cffi":
            return Response("<html><title>Just a moment...</title><div>cf-chl</div></html>")
        return Response('<html><div class="issue-item clearfix">Article</div></html>')

    monkeypatch.setattr(http_fetch, "_fetch_once", fake_fetch_once)

    result = fetch_html_resilient(
        "https://example.test/issue",
        clients=("curl_cffi", "requests"),
        curl_impersonations=("chrome136",),
        rounds=1,
        required_selector=".issue-item",
    )

    assert result.ok
    assert result.client == "requests"
    assert calls == [("curl_cffi", "chrome136"), ("requests", "default")]


def test_fetch_html_resilient_reports_blocked_after_all_http_clients(monkeypatch):
    from papertrack import http_fetch

    monkeypatch.setattr(
        http_fetch,
        "_fetch_once",
        lambda *args, **kwargs: Response("<html>Attention Required! cf-chl</html>", 403),
    )

    result = fetch_html_resilient(
        "https://example.test/issue",
        clients=("curl_cffi",),
        curl_impersonations=("chrome136",),
        rounds=1,
    )

    assert result.outcome == BLOCKED
    assert not result.ok


def test_block_detector_does_not_treat_plain_cloudflare_word_as_block():
    assert is_blocked_response("<html>Cloudflare CDN status page text</html>") is False

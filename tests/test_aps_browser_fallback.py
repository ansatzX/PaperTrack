from papertrack import aps_fetch
from papertrack.adapter_result import BLOCKED, FETCH_FAILED, SUCCESS


HTML = '''<div class="browse-result-card issue"><div class="title"><a href="/doi/10.1103/PhysRevA.1.1">A title</a></div><div class="citation-container"><p>Alice, Bob</p><p>1, 1 (2024)</p></div></div>'''


def test_blocked_http_uses_browser(monkeypatch):
    monkeypatch.setattr(aps_fetch, "_cache", {})
    monkeypatch.setattr(aps_fetch, "_fetch_html", lambda *args: ("challenge", BLOCKED))
    monkeypatch.setattr(aps_fetch, "_fetch_aps_browser", lambda url: {"html": HTML, "outcome": SUCCESS})
    result = aps_fetch.query_aps_issue_result("pra", "1", "1", "Phys Rev A")
    assert result.outcome == SUCCESS
    assert len(result.entries) == 1


def test_browser_failure_preserves_blocked(monkeypatch):
    monkeypatch.setattr(aps_fetch, "_cache", {})
    monkeypatch.setattr(aps_fetch, "_fetch_html", lambda *args: ("challenge", BLOCKED))
    monkeypatch.setattr(aps_fetch, "_fetch_aps_browser", lambda url: {"html": "", "outcome": FETCH_FAILED})
    result = aps_fetch.query_aps_issue_result("pra", "1", "2")
    assert result.outcome == FETCH_FAILED
    assert result.entries == {}


def test_browser_command_is_official_and_throttled(monkeypatch):
    import papertrack.aps_fetch as mod
    seen = {}
    monkeypatch.setattr(mod, "provider_throttle", lambda provider, interval: seen.update(provider=provider, interval=interval))
    class Proc:
        returncode = 0
        stdout = '{"outcome":"success","html":""}'
        stderr = ""
    monkeypatch.setattr(mod.subprocess, "run", lambda cmd, **kwargs: seen.update(cmd=cmd) or Proc())
    mod._fetch_aps_browser("https://journals.aps.org/pra/issues/1/1")
    assert seen["provider"] == "aps"
    assert seen["cmd"][0:3] == ["xvfb-run", "-a", mod.sys.executable]
    assert seen["cmd"][-1].startswith("https://journals.aps.org/")

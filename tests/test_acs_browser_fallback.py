from papertrack import acs_fetch
from papertrack.adapter_result import BLOCKED, SUCCESS, FETCH_FAILED
from papertrack.journal_entry import JournalEntry

def test_blocked_http_browser_success(monkeypatch):
    acs_fetch._cache.clear()
    monkeypatch.setattr(acs_fetch, "_fetch_html", lambda url: ("challenge", BLOCKED, 403))
    entry = JournalEntry(doi="10.1021/x", title="X", authors=[], abstract="", journal_name="J", volume="1", issue="1")
    monkeypatch.setattr(acs_fetch, "_fetch_acs_browser", lambda url: {"html":"ok", "outcome":SUCCESS, "status_code":200})
    monkeypatch.setattr(acs_fetch, "_parse_acs_toc_result", lambda html, name: type("P", (), {"entries": {entry.doi: entry}, "issue_item_count":1, "missing_fields":[], "critical_field_failures":[]})())
    result = acs_fetch.query_acs_issue_result("jctcce", "1", "1")
    assert result.outcome == SUCCESS and entry.doi in result.entries

def test_browser_failure_preserves_blocked(monkeypatch):
    acs_fetch._cache.clear()
    monkeypatch.setattr(acs_fetch, "_fetch_html", lambda url: ("challenge", BLOCKED, 403))
    monkeypatch.setattr(acs_fetch, "_fetch_acs_browser", lambda url: {"outcome":FETCH_FAILED, "message":"timeout"})
    result = acs_fetch.query_acs_issue_result("jctcce", "2", "1")
    assert result.outcome == FETCH_FAILED

def test_browser_command_is_official_url(monkeypatch):
    captured = {}
    monkeypatch.setattr(acs_fetch, "provider_throttle", lambda *a: 0)
    class P:
        stdout='{"outcome":"success","html":""}'
    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return P()
    monkeypatch.setattr(acs_fetch.subprocess, "run", fake_run)
    acs_fetch._fetch_acs_browser("https://pubs.acs.org/toc/jctcce/1/1")
    assert captured["cmd"][-1] == "https://pubs.acs.org/toc/jctcce/1/1"
    assert not any("article" in x.lower() for x in captured["cmd"])

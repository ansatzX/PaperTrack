from papertrack import browser_session


def test_cdp_port_requires_loopback_numeric_port():
    assert browser_session._cdp_port("http://127.0.0.1:9222") == 9222


def test_cdp_port_rejects_non_loopback_endpoint():
    try:
        browser_session._cdp_port("http://example.com:9222")
    except ValueError as exc:
        assert "loopback" in str(exc)
    else:
        raise AssertionError("non-loopback CDP endpoint was accepted")


def test_launch_reuses_existing_cdp_without_spawning(monkeypatch, tmp_path):
    monkeypatch.setattr(browser_session, "cdp_is_available", lambda *_: True)
    monkeypatch.setattr(
        browser_session.subprocess, "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("spawned")),
    )

    browser_session.launch_chrome_for_cdp(
        "/chrome", tmp_path / "profile", "http://127.0.0.1:9222"
    )

from papertrack.aps_loi import discover_issues_aps


def test_discover_issues_aps_skips_partial_and_sorts_newest_first(monkeypatch):
    from papertrack import aps_loi

    def fake_scrape(url):
        if url.endswith("/prl/issues"):
            return """
            <a href="/prl/issues/136#v136">Volume 136</a>
            <a href="/prl/issues/135#v135">Volume 135</a>
            """
        if url.endswith("/prl/issues/136"):
            return """
            <a href="/prl/issues/136/1">Issue #1:  9 January 2026 (010201 — 018101)</a>
            <a href="/prl/issues/136/2">Issue #2: 16 January 2026 (020401 — 027201)</a>
            <a href="/prl/issues/136/3">Issue #3: 23 January 2026 ( partial )</a>
            """
        if url.endswith("/prl/issues/135"):
            return """
            <a href="/prl/issues/135/24">Issue #24: 19 December 2025 (240401 — 249901)</a>
            """
        return ""

    monkeypatch.setattr(aps_loi, "_scrape", fake_scrape)

    assert discover_issues_aps("prl", from_year=2026) == [
        (2026, 136, 2),
        (2026, 136, 1),
    ]


def test_discover_issues_aps_can_include_partial(monkeypatch):
    from papertrack import aps_loi

    monkeypatch.setattr(
        aps_loi,
        "_scrape",
        lambda url: (
            '<a href="/prl/issues/136#v136">Volume 136</a>'
            if url.endswith("/prl/issues")
            else '<a href="/prl/issues/136/3">Issue #3: 23 January 2026 ( partial )</a>'
        ),
    )

    assert discover_issues_aps("prl", from_year=2026, include_partial=True) == [
        (2026, 136, 3),
    ]

from papertrack.cli import _journal_output_root
from papertrack.journal_entry import JournalEntry


def test_jcp_provider_outputs_under_aip_folder(tmp_path):
    cfg = {"name": "Journal of Chemical Physics", "slug": "jcp", "provider": "aip"}

    assert _journal_output_root(str(tmp_path), cfg) == str(tmp_path / "aip")


def test_acs_journal_outputs_under_acs_folder(tmp_path):
    cfg = {"name": "Journal of Chemical Theory and Computation", "slug": "jctc", "acs_code": "jctcce"}

    assert _journal_output_root(str(tmp_path), cfg) == str(tmp_path / "acs")


def test_explicit_journal_cli_passes_year(monkeypatch, tmp_path):
    from papertrack import cli

    captured = {}

    class Args:
        journal = "jcp"
        data_dir = str(tmp_path)
        volume = "163"
        issue = "24"
        year = 2025
        backfill = False
        from_year = 0

    monkeypatch.setattr(
        cli,
        "filter_journal_to_md",
        lambda **kwargs: captured.update(kwargs),
    )

    cli._run_journal(Args(), zotero=None)

    assert captured["year"] == 2025


def test_parse_aip_toc_extracts_journal_entries():
    from papertrack.aip_fetch import _parse_aip_toc

    html = """
    <div class="al-article-items">
      <div class="issue-featured-image">
        <img src="https://example.test/toc.jpg" />
      </div>
      <h3 class="customLink item-title">
        <a href="/aip/jcp/article/164/16/164101/3387571/RE-ADC-The-algebraic-diagrammatic-construction">
          RE-ADC: The algebraic diagrammatic construction scheme
        </a>
      </h3>
      <div class="al-authors-list">
        <span class="wi-fullname brand-fg"><a>Jonas Leitner</a></span>
        <span class="al-author-delim">;</span>
        <span class="wi-fullname brand-fg"><a>Linus B. Dittmer</a></span>
      </div>
      <div class="ww-citation-primary">
        <em>J. Chem. Phys.</em> 164, 164101 (2026)
        <span class="citation-label">
          <a href="https://doi.org/10.1063/5.0323573">https://doi.org/10.1063/5.0323573</a>
        </span>
      </div>
      <a class="showAbstractLink js-show-abstract at-Show-Abstract-Link"
         data-articleid="3387571"
         data-is-lay-abstract="False"
         data-abstract-type="abstract"
         href="javascript:;">Abstract</a>
    </div>
    """

    entries, abstract_ids = _parse_aip_toc(html, "Journal of Chemical Physics")

    entry = entries["10.1063/5.0323573"]
    assert entry.title == "RE-ADC: The algebraic diagrammatic construction scheme"
    assert entry.authors == ["Jonas Leitner", "Linus B. Dittmer"]
    assert entry.journal_name == "Journal of Chemical Physics"
    assert entry.volume == "164"
    assert entry.issue == "16"
    assert entry.pages == "164101"
    assert entry.date == "2026"
    assert entry.toc_image_url == "https://example.test/toc.jpg"
    assert abstract_ids["10.1063/5.0323573"] == "3387571"


def test_crossref_fetch_year_uses_cursor_pagination(monkeypatch):
    from papertrack import journal_fetch

    journal_fetch.clear_cache()
    seen_urls = []

    def fake_get(url):
        seen_urls.append(url)
        if "cursor=%2A" in url:
            return {
                "message": {
                    "next-cursor": "next page",
                    "items": [
                        {
                            "DOI": "10.1063/first",
                            "title": ["First"],
                            "volume": "164",
                            "issue": "1",
                        }
                    ],
                }
            }
        return {
            "message": {
                "next-cursor": "next page",
                "items": [
                    {
                        "DOI": "10.1063/second",
                        "title": ["Second"],
                        "volume": "164",
                        "issue": "2",
                    }
                ],
            }
        }

    monkeypatch.setattr(journal_fetch, "_crossref_get", fake_get)

    articles = journal_fetch._fetch_year("0021-9606", 2026)

    assert [item["DOI"] for item in articles] == ["10.1063/first", "10.1063/second"]
    assert "cursor=%2A" in seen_urls[0]
    assert "cursor=next+page" in seen_urls[1]


def test_crossref_abstract_is_plain_text():
    from papertrack.journal_entry import JournalEntry

    entry = JournalEntry.from_crossref(
        {
            "DOI": "10.1063/example",
            "title": ["Title"],
            "abstract": "<jats:p>First sentence.</jats:p><jats:p>Second sentence.</jats:p>",
        },
        "Journal of Chemical Physics",
    )

    assert entry.abstract == "First sentence. Second sentence."


def test_aip_provider_uses_official_source_before_crossref(monkeypatch):
    from papertrack import aip_fetch, journal_fetch

    official_entry = JournalEntry(
        doi="10.1063/official",
        title="Official article",
        authors=[],
        abstract="Official abstract",
    )

    def fake_aip_issue(journal_slug, volume, issue, journal_name):
        assert journal_slug == "jcp"
        return {official_entry.doi: official_entry}

    def fake_crossref_issue(*args, **kwargs):
        raise AssertionError("CrossRef should not be called when AIP returns entries")

    monkeypatch.setattr(aip_fetch, "query_aip_issue", fake_aip_issue)
    monkeypatch.setattr(journal_fetch, "query_journal_issue", fake_crossref_issue)

    entries = journal_fetch.query_journal_issue_full(
        "0021-9606",
        "163",
        "24",
        "The Journal of Chemical Physics",
        2025,
        provider="aip",
        journal_slug="jcp",
    )

    assert entries == {official_entry.doi: official_entry}


def test_aip_provider_uses_crossref_only_to_fill_missing_official_abstracts(monkeypatch):
    from papertrack import aip_fetch, journal_fetch

    with_abstract = JournalEntry(
        doi="10.1063/with-abstract",
        title="Official article with abstract",
        authors=[],
        abstract="Official abstract",
    )
    missing_abstract = JournalEntry(
        doi="10.1063/missing-abstract",
        title="Official article missing abstract",
        authors=[],
        abstract="",
    )

    monkeypatch.setattr(
        aip_fetch,
        "query_aip_issue",
        lambda *args, **kwargs: {
            with_abstract.doi: with_abstract,
            missing_abstract.doi: missing_abstract,
        },
    )

    def fake_crossref_year(issn, year):
        return [
            {
                "DOI": "10.1063/missing-abstract",
                "title": ["CrossRef title should not replace official title"],
                "abstract": "<jats:p>CrossRef fallback abstract.</jats:p>",
            },
            {
                "DOI": "10.1063/crossref-extra",
                "title": ["CrossRef extra article"],
                "abstract": "<jats:p>Must not be added.</jats:p>",
            },
        ]

    monkeypatch.setattr(journal_fetch, "_fetch_year", fake_crossref_year)

    entries = journal_fetch.query_journal_issue_full(
        "0021-9606",
        "163",
        "24",
        "The Journal of Chemical Physics",
        2025,
        provider="aip",
        journal_slug="jcp",
    )

    assert list(entries) == ["10.1063/with-abstract", "10.1063/missing-abstract"]
    assert entries["10.1063/with-abstract"].abstract == "Official abstract"
    assert entries["10.1063/missing-abstract"].title == "Official article missing abstract"
    assert entries["10.1063/missing-abstract"].abstract == "CrossRef fallback abstract."


def test_aip_crossref_abstract_fallback_infers_year_from_official_metadata(monkeypatch):
    from papertrack import aip_fetch, journal_fetch

    missing_abstract = JournalEntry(
        doi="10.1063/missing-abstract",
        title="Official article missing abstract",
        authors=[],
        abstract="",
        date="2025",
    )
    seen_years = []

    monkeypatch.setattr(
        aip_fetch,
        "query_aip_issue",
        lambda *args, **kwargs: {missing_abstract.doi: missing_abstract},
    )

    def fake_crossref_year(issn, year):
        seen_years.append(year)
        return [
            {
                "DOI": "10.1063/missing-abstract",
                "abstract": "<jats:p>CrossRef fallback abstract.</jats:p>",
            },
        ]

    monkeypatch.setattr(journal_fetch, "_fetch_year", fake_crossref_year)

    entries = journal_fetch.query_journal_issue_full(
        "0021-9606",
        "163",
        "24",
        "The Journal of Chemical Physics",
        provider="aip",
        journal_slug="jcp",
    )

    assert seen_years == [2025]
    assert entries["10.1063/missing-abstract"].abstract == "CrossRef fallback abstract."


def test_aip_provider_falls_back_to_crossref_when_official_empty(monkeypatch):
    from papertrack import aip_fetch, journal_fetch

    crossref_entry = JournalEntry(
        doi="10.1063/crossref",
        title="CrossRef article",
        authors=[],
        abstract="CrossRef abstract",
    )

    monkeypatch.setattr(aip_fetch, "query_aip_issue", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        journal_fetch,
        "query_journal_issue",
        lambda *args, **kwargs: {crossref_entry.doi: crossref_entry},
    )

    entries = journal_fetch.query_journal_issue_full(
        "0021-9606",
        "163",
        "24",
        "The Journal of Chemical Physics",
        2025,
        provider="aip",
        journal_slug="jcp",
    )

    assert entries == {crossref_entry.doi: crossref_entry}


def test_aip_fetch_html_retries_impersonation_rounds(monkeypatch):
    from papertrack import aip_fetch

    attempts = []

    class Response:
        def __init__(self, status_code, text):
            self.status_code = status_code
            self.text = text

    def fake_get(url, impersonate, timeout, allow_redirects):
        attempts.append(impersonate)
        if len(attempts) < 3:
            return Response(403, "blocked")
        return Response(200, '<html><div class="al-article-items"></div></html>')

    monkeypatch.setattr(aip_fetch, "AIP_IMPERSONATIONS", ("firefox", "chrome"))
    monkeypatch.setattr(aip_fetch.requests, "get", fake_get)
    monkeypatch.setattr(aip_fetch.time, "sleep", lambda *args: None)

    html = aip_fetch._fetch_html("https://pubs.aip.org/aip/jcp/issue/164/16", ".al-article-items")

    assert html
    assert attempts == ["firefox", "chrome", "firefox"]


def test_query_aip_issue_uses_official_abstract_ajax_not_article_pages(monkeypatch):
    from papertrack import aip_fetch

    aip_fetch._cache.clear()
    html = """
    <div class="al-article-items">
      <h3 class="customLink item-title">
        <a href="/aip/jcp/article/164/16/164101/3387571/Title">Official article</a>
      </h3>
      <div class="ww-citation-primary">
        <em>J. Chem. Phys.</em> 164, 164101 (2026)
        <span class="citation-label">
          <a href="https://doi.org/10.1063/example">https://doi.org/10.1063/example</a>
        </span>
      </div>
      <a class="showAbstractLink js-show-abstract at-Show-Abstract-Link"
         data-articleid="3387571"
         data-is-lay-abstract="False"
         data-abstract-type="abstract"
         href="javascript:;">Abstract</a>
    </div>
    """
    fetched_urls = []
    fetched_abstract_ids = []

    def fake_fetch(url, required_selector=""):
        fetched_urls.append(url)
        if "/article/" in url:
            raise AssertionError("query_aip_issue must not fetch article pages")
        return html

    def fake_fetch_abstract(article_id, referer_url):
        fetched_abstract_ids.append((article_id, referer_url))
        return "Official abstract text."

    monkeypatch.setattr(aip_fetch, "_fetch_html", fake_fetch)
    monkeypatch.setattr(aip_fetch, "_fetch_aip_abstract", fake_fetch_abstract)

    entries = aip_fetch.query_aip_issue("jcp", "164", "16", "Journal of Chemical Physics")

    assert list(entries) == ["10.1063/example"]
    assert entries["10.1063/example"].abstract == "Official abstract text."
    assert fetched_urls == ["https://pubs.aip.org/aip/jcp/issue/164/16"]
    assert fetched_abstract_ids == [("3387571", "https://pubs.aip.org/aip/jcp/issue/164/16")]


def test_query_aip_issue_does_not_cache_empty_fetch_failures(monkeypatch):
    from papertrack import aip_fetch

    aip_fetch._cache.clear()
    calls = []

    def fake_fetch(url, required_selector=""):
        calls.append(url)
        return ""

    monkeypatch.setattr(aip_fetch, "_fetch_html", fake_fetch)

    assert aip_fetch.query_aip_issue("jcp", "164", "16") == {}
    assert aip_fetch.query_aip_issue("jcp", "164", "16") == {}
    assert calls == [
        "https://pubs.aip.org/aip/jcp/issue/164/16",
        "https://pubs.aip.org/jcp/issue/164/16",
        "https://pubs.aip.org/aip/jcp/issue/164/16",
        "https://pubs.aip.org/jcp/issue/164/16",
    ]


def test_fetch_aip_abstract_uses_official_ajax_endpoint(monkeypatch):
    from papertrack import aip_fetch

    seen = []

    class Response:
        status_code = 200
        text = (
            '{"Success":true,"Html":"'
            '<section class=\\"abstract\\" aria-label=\\"Main abstract\\">'
            '<h2>Abstract</h2><p>Official abstract text.</p></section>"}'
        )

        def json(self):
            return {
                "Success": True,
                "Html": (
                    '<section class="abstract" aria-label="Main abstract">'
                    "<h2>Abstract</h2><p>Official abstract text.</p></section>"
                ),
            }

    def fake_get(url, params, headers, impersonate, timeout, allow_redirects):
        seen.append((url, params, headers, impersonate))
        return Response()

    monkeypatch.setattr(aip_fetch, "AIP_IMPERSONATIONS", ("chrome",))
    monkeypatch.setattr(aip_fetch.requests, "get", fake_get)

    abstract = aip_fetch._fetch_aip_abstract(
        "3387571",
        "https://pubs.aip.org/aip/jcp/issue/164/16",
    )

    assert abstract == "Official abstract text."
    assert seen[0][0] == "https://pubs.aip.org/PlatformArticle/ArticleAbstractAjax"
    assert seen[0][1] == {"articleId": "3387571", "layAbstract": "False"}
    assert seen[0][2]["Referer"] == "https://pubs.aip.org/aip/jcp/issue/164/16"


def test_fetch_aip_abstract_retries_impersonation_rounds(monkeypatch):
    from papertrack import aip_fetch

    attempts = []

    class Response:
        status_code = 200
        text = '{"Success":true,"Html":"<p>Official abstract text.</p>"}'

        def json(self):
            if len(attempts) < 3:
                return {"Success": True, "Html": ""}
            return {"Success": True, "Html": "<p>Official abstract text.</p>"}

    def fake_get(url, params, headers, impersonate, timeout, allow_redirects):
        attempts.append(impersonate)
        return Response()

    monkeypatch.setattr(aip_fetch, "AIP_IMPERSONATIONS", ("firefox", "chrome"))
    monkeypatch.setattr(aip_fetch.requests, "get", fake_get)
    monkeypatch.setattr(aip_fetch.time, "sleep", lambda *args: None)

    abstract = aip_fetch._fetch_aip_abstract(
        "3387571",
        "https://pubs.aip.org/aip/jcp/issue/164/16",
    )

    assert abstract == "Official abstract text."
    assert attempts == ["firefox", "chrome", "firefox"]


def test_non_article_titles_are_filtered_from_crossref_year(monkeypatch):
    from papertrack import journal_fetch

    journal_fetch.clear_cache()

    def fake_get(url):
        if "cursor=done" in url:
            return {"message": {"next-cursor": "done", "items": []}}
        return {
            "message": {
                "next-cursor": "done",
                "items": [
                    {
                        "DOI": "10.1063/article",
                        "title": ["Research article"],
                        "volume": "163",
                        "issue": "24",
                    },
                    {
                        "DOI": "10.1063/erratum",
                        "title": ["Erratum: Research article"],
                        "volume": "163",
                        "issue": "24",
                    },
                ],
            }
        }

    monkeypatch.setattr(journal_fetch, "_crossref_get", fake_get)

    articles = journal_fetch._fetch_year("0021-9606", 2025)

    assert [item["DOI"] for item in articles] == ["10.1063/article"]

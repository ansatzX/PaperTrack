from pathlib import Path

import pytest

from papertrack.cli import _journal_output_root
from papertrack.journal_entry import JournalEntry
from papertrack.source_policy import SourcePolicy


def test_jcp_provider_outputs_under_aip_folder(tmp_path):
    cfg = {"name": "Journal of Chemical Physics", "slug": "jcp", "provider": "aip"}

    assert _journal_output_root(str(tmp_path), cfg) == str(tmp_path / "aip")


def test_acs_journal_outputs_under_acs_folder(tmp_path):
    cfg = {"name": "Journal of Chemical Theory and Computation", "slug": "jctc", "acs_code": "jctcce"}

    assert _journal_output_root(str(tmp_path), cfg) == str(tmp_path / "acs")


def test_aps_journal_outputs_under_aps_folder(tmp_path):
    cfg = {"name": "Physical Review Letters", "slug": "prl", "provider": "aps"}

    assert _journal_output_root(str(tmp_path), cfg) == str(tmp_path / "aps")


def test_builtin_journal_configs_include_jcim_jacs_and_prl():
    from papertrack.codex import get_journals

    journals = get_journals()

    assert journals["jcim"]["acs_code"] == "jcisd8"
    assert journals["jacs"]["acs_code"] == "jacsat"
    assert journals["prl"]["provider"] == "aps"
    assert journals["prl"]["issn"] == "0031-9007"


def test_explicit_journal_cli_passes_year(monkeypatch, tmp_path):
    from papertrack import cli

    captured = {}

    monkeypatch.setattr(cli, "ZoteroQuery", lambda: None)
    monkeypatch.setattr(
        cli,
        "journal_configs",
        {
            "jcp": {
                "name": "Journal of Chemical Physics",
                "issn": "0021-9606",
                "slug": "jcp",
                "provider": "aip",
            }
        },
    )
    monkeypatch.setattr(
        cli,
        "filter_journal_to_md",
        lambda **kwargs: captured.update(kwargs),
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "papertrack",
            "journal",
            "issue",
            "--journal",
            "jcp",
            "--volume",
            "163",
            "--issue",
            "24",
            "--year",
            "2025",
            "--data-dir",
            str(tmp_path),
        ],
    )

    cli.main()

    assert captured["journal_name"] == "Journal of Chemical Physics"
    assert captured["journal_slug"] == "jcp"
    assert captured["volume"] == "163"
    assert captured["issue"] == "24"
    assert captured["year"] == 2025
    assert captured["provider"] == "aip"
    assert captured["md_folder"] == str(tmp_path / "aip")


def test_journal_latest_dispatches_auto_without_backfill(monkeypatch, tmp_path):
    from papertrack import cli

    captured = {}

    monkeypatch.setattr(cli, "ZoteroQuery", lambda: None)
    monkeypatch.setattr(
        cli,
        "journal_configs",
        {
            "jctc": {
                "name": "Journal of Chemical Theory and Computation",
                "issn": "1549-9618",
                "slug": "jctc",
                "acs_code": "jctcce",
            }
        },
    )
    monkeypatch.setattr(cli, "filter_journal_auto", lambda **kwargs: captured.update(kwargs))
    monkeypatch.setattr(
        "sys.argv",
        [
            "papertrack",
            "journal",
            "latest",
            "--journal",
            "jctc",
            "--data-dir",
            str(tmp_path),
        ],
    )

    cli.main()

    assert captured["journal_slug"] == "jctc"
    assert captured["acs_code"] == "jctcce"
    assert captured["md_folder"] == str(tmp_path / "acs")
    assert captured["backfill"] is False
    assert captured["from_year"] == 0


def test_journal_backfill_dispatches_auto_with_from_year(monkeypatch, tmp_path):
    from papertrack import cli

    captured = {}

    monkeypatch.setattr(cli, "ZoteroQuery", lambda: None)
    monkeypatch.setattr(
        cli,
        "journal_configs",
        {
            "jcp": {
                "name": "Journal of Chemical Physics",
                "issn": "0021-9606",
                "slug": "jcp",
                "provider": "aip",
            }
        },
    )
    monkeypatch.setattr(cli, "filter_journal_auto", lambda **kwargs: captured.update(kwargs))
    monkeypatch.setattr(
        "sys.argv",
        [
            "papertrack",
            "journal",
            "backfill",
            "--journal",
            "jcp",
            "--from-year",
            "2018",
            "--data-dir",
            str(tmp_path),
        ],
    )

    cli.main()

    assert captured["journal_slug"] == "jcp"
    assert captured["provider"] == "aip"
    assert captured["md_folder"] == str(tmp_path / "aip")
    assert captured["backfill"] is True
    assert captured["from_year"] == 2018


def test_prl_journal_dispatches_as_aps_provider(monkeypatch, tmp_path):
    from papertrack import cli

    captured = {}

    monkeypatch.setattr(cli, "ZoteroQuery", lambda: None)
    monkeypatch.setattr(
        cli,
        "journal_configs",
        {
            "prl": {
                "name": "Physical Review Letters",
                "issn": "0031-9007",
                "slug": "prl",
                "provider": "aps",
            }
        },
    )
    monkeypatch.setattr(cli, "filter_journal_auto", lambda **kwargs: captured.update(kwargs))
    monkeypatch.setattr(
        "sys.argv",
        [
            "papertrack",
            "journal",
            "latest",
            "--journal",
            "prl",
            "--data-dir",
            str(tmp_path),
        ],
    )

    cli.main()

    assert captured["journal_slug"] == "prl"
    assert captured["provider"] == "aps"
    assert captured["md_folder"] == str(tmp_path / "aps")


def test_filter_journal_auto_uses_aps_issue_discovery(monkeypatch, tmp_path):
    from papertrack import aps_loi, report

    state = {}
    captured = []

    monkeypatch.setattr(report, "_load_state", lambda md_folder: state)
    monkeypatch.setattr(report, "_save_state", lambda md_folder, new_state: state.update(new_state))
    monkeypatch.setattr(aps_loi, "discover_issues_aps", lambda journal_slug, from_year=0: [(2026, 136, 1)])
    monkeypatch.setattr(
        report,
        "filter_journal_to_md",
        lambda **kwargs: captured.append(kwargs) or True,
    )

    report.filter_journal_auto(
        journal_name="Physical Review Letters",
        journal_slug="prl",
        issn="0031-9007",
        acs_code="",
        md_folder=str(tmp_path),
        provider="aps",
    )

    assert captured[0]["journal_slug"] == "prl"
    assert captured[0]["volume"] == "136"
    assert captured[0]["issue"] == "1"
    assert captured[0]["provider"] == "aps"
    assert state == {"prl": {"processed": [[136, 1]]}}


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


def test_parse_aps_issue_extracts_journal_entries():
    from papertrack.aps_fetch import _parse_aps_issue

    html = """
    <div class="browse-result-card issue">
      <div class="content align-start w-full">
        <section class="headline">
          <h3 class="title">
            <a href="/prl/abstract/10.1103/yp9h-sr2m">
              Smaller Grains Are Not Stronger: Microcrystalline Metals at Ultrahigh Strain Rates
            </a>
          </h3>
        </section>
        <section class="citation-container font-normal">
          <p>Laura Wu, Yuan Yao, Luyan Li, Qi Tang, and Mostafa Hassani</p>
          <p><span>Phys. Rev. Lett. <b>136</b>, 016104 (2026)</span>
             <span>- Published  9 January, 2026</span></p>
        </section>
        <section class="summary-container">
          <div class="image-frame">
            <img class="keyimage" src="//cdn.journals.aps.org/example.png" />
          </div>
          <p></p>
          <div><p>A textbook rule breaks down for high-speed deformations.</p></div>
        </section>
      </div>
    </div>
    """

    entries = _parse_aps_issue(html, "prl", "Physical Review Letters", "136", "1")

    entry = entries["10.1103/yp9h-sr2m"]
    assert entry.title == "Smaller Grains Are Not Stronger: Microcrystalline Metals at Ultrahigh Strain Rates"
    assert entry.authors == ["Laura Wu", "Yuan Yao", "Luyan Li", "Qi Tang", "Mostafa Hassani"]
    assert entry.journal_name == "Physical Review Letters"
    assert entry.volume == "136"
    assert entry.issue == "1"
    assert entry.pages == "016104"
    assert entry.date == "2026-01-09"
    assert entry.abstract == "A textbook rule breaks down for high-speed deformations."


def test_parse_aps_issue_supports_legacy_h4_titles():
    from papertrack.aps_fetch import _parse_aps_issue

    html = """
    <div class="browse-result-card issue">
      <section class="headline">
        <h4 class="title">
          <a href="/prl/abstract/10.1103/PhysRevLett.127.270401">
            Quantum Vacuum Sagnac Effect
          </a>
        </h4>
      </section>
      <section class="citation-container">
        <p>Guilherme C. Matos, Reinaldo de Melo e Souza, Paulo A. Maia Neto, and François Impens</p>
        <p><span>Phys. Rev. Lett. <b>127</b>, 270401 (2021)</span>
           <span> - Published 29 December, 2021</span></p>
      </section>
      <section class="summary-container"><p></p></section>
    </div>
    """

    entries = _parse_aps_issue(html, "prl", "Physical Review Letters", "127", "27")

    assert list(entries) == ["10.1103/PhysRevLett.127.270401"]
    entry = entries["10.1103/PhysRevLett.127.270401"]
    assert entry.title == "Quantum Vacuum Sagnac Effect"
    assert entry.authors == [
        "Guilherme C. Matos",
        "Reinaldo de Melo e Souza",
        "Paulo A. Maia Neto",
        "François Impens",
    ]
    assert entry.pages == "270401"


def test_parse_aps_historical_issue_fixtures():
    from papertrack.aps_fetch import _parse_aps_issue

    fixture_dir = Path(__file__).parent / "fixtures" / "aps"
    issue_127 = _parse_aps_issue(
        (fixture_dir / "prl-127-27.html").read_text(encoding="utf-8"),
        "prl",
        "Physical Review Letters",
        "127",
        "27",
    )
    issue_129 = _parse_aps_issue(
        (fixture_dir / "prl-129-27.html").read_text(encoding="utf-8"),
        "prl",
        "Physical Review Letters",
        "129",
        "27",
    )

    assert list(issue_127) == ["10.1103/PhysRevLett.127.270401"]
    assert issue_127["10.1103/PhysRevLett.127.270401"].abstract == (
        "Official historical issue summary."
    )
    assert list(issue_129) == ["10.1103/PhysRevLett.129.270402"]
    assert issue_129["10.1103/PhysRevLett.129.270402"].title == (
        "Certified Quantum Measurement"
    )


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
    from papertrack.adapter_result import AdapterResult, SUCCESS

    official_entry = JournalEntry(
        doi="10.1063/official",
        title="Official article",
        authors=[],
        abstract="Official abstract",
    )

    def fake_aip_issue(
        journal_slug, volume, issue, journal_name, provider_runtime=None
    ):
        assert journal_slug == "jcp"
        assert provider_runtime is None
        return AdapterResult(
            entries={official_entry.doi: official_entry},
            outcome=SUCCESS,
            provider="aip",
            source="journal",
        )

    def fake_crossref_issue(*args, **kwargs):
        raise AssertionError("CrossRef should not be called when AIP returns entries")

    monkeypatch.setattr(aip_fetch, "query_aip_issue_result", fake_aip_issue)
    monkeypatch.setattr(journal_fetch, "_fetch_year", fake_crossref_issue)

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


def test_aps_provider_uses_official_source_before_crossref(monkeypatch):
    from papertrack import aps_fetch, journal_fetch
    from papertrack.adapter_result import AdapterResult, SUCCESS

    official_entry = JournalEntry(
        doi="10.1103/yp9h-sr2m",
        title="Official PRL article",
        authors=[],
        abstract="Official issue summary",
    )

    monkeypatch.setattr(
        aps_fetch,
        "query_aps_issue_result",
        lambda *args, **kwargs: AdapterResult(
            entries={official_entry.doi: official_entry},
            outcome=SUCCESS,
            provider="aps",
            source="journal",
        ),
    )

    def fake_crossref_issue(*args, **kwargs):
        raise AssertionError("CrossRef should not be called when APS returns entries")

    monkeypatch.setattr(journal_fetch, "_fetch_year", fake_crossref_issue)

    entries = journal_fetch.query_journal_issue_full(
        "0031-9007",
        "136",
        "1",
        "Physical Review Letters",
        2026,
        provider="aps",
        journal_slug="prl",
    )

    assert entries == {official_entry.doi: official_entry}


def test_aip_metadata_mode_does_not_fill_official_abstracts_from_crossref(monkeypatch):
    from papertrack import aip_fetch, journal_fetch
    from papertrack.adapter_result import AdapterResult, SUCCESS

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
        "query_aip_issue_result",
        lambda *args, **kwargs: AdapterResult(entries={
            with_abstract.doi: with_abstract,
            missing_abstract.doi: missing_abstract,
        }, outcome=SUCCESS, provider="aip", source="journal"),
    )

    def fake_crossref_year(*args, **kwargs):
        raise AssertionError("authoritative issue dispatch must not call CrossRef")

    monkeypatch.setattr(journal_fetch, "_fetch_year", fake_crossref_year)
    events = []
    monkeypatch.setattr(journal_fetch, "record_fetch_attempt", lambda event, html=None: events.append(event))

    entries = journal_fetch.query_journal_issue_full(
        "0021-9606",
        "163",
        "24",
        "The Journal of Chemical Physics",
        2025,
        provider="aip",
        journal_slug="jcp",
        source_policy=SourcePolicy.from_environment(
            {"PAPERTRACK_CROSSREF_MODE": "metadata"}
        ),
    )

    assert list(entries) == ["10.1063/with-abstract", "10.1063/missing-abstract"]
    assert entries["10.1063/with-abstract"].abstract == "Official abstract"
    assert entries["10.1063/missing-abstract"].title == "Official article missing abstract"
    assert entries["10.1063/missing-abstract"].abstract == ""
    assert events == []


def test_aip_metadata_mode_does_not_infer_crossref_year_from_official_metadata(monkeypatch):
    from papertrack import aip_fetch, journal_fetch
    from papertrack.adapter_result import AdapterResult, SUCCESS

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
        "query_aip_issue_result",
        lambda *args, **kwargs: AdapterResult(
            entries={missing_abstract.doi: missing_abstract},
            outcome=SUCCESS,
            provider="aip",
            source="journal",
        ),
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
        source_policy=SourcePolicy.from_environment(
            {"PAPERTRACK_CROSSREF_MODE": "metadata"}
        ),
    )

    assert seen_years == []
    assert entries["10.1063/missing-abstract"].abstract == ""


def test_official_only_provider_does_not_request_crossref_abstracts(monkeypatch):
    from papertrack import aip_fetch, journal_fetch
    from papertrack.adapter_result import AdapterResult, SUCCESS

    official_entry = JournalEntry(
        doi="10.1063/official-only",
        title="Official title",
        authors=["Official Author"],
        abstract="",
    )
    monkeypatch.setattr(
        aip_fetch,
        "query_aip_issue_result",
        lambda *args, **kwargs: AdapterResult(
            entries={official_entry.doi: official_entry},
            outcome=SUCCESS,
            provider="aip",
            source="journal",
        ),
    )
    monkeypatch.setattr(
        journal_fetch,
        "_fetch_year",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("official-only mode must not call CrossRef")
        ),
    )

    entries = journal_fetch.query_journal_issue_full(
        "0021-9606",
        "163",
        "24",
        "The Journal of Chemical Physics",
        provider="aip",
        journal_slug="jcp",
        source_policy=SourcePolicy.official_only(),
    )

    assert entries == {official_entry.doi: official_entry}
    assert entries[official_entry.doi].abstract == ""


def test_official_provider_does_not_use_crossref_for_issue_membership(monkeypatch):
    from papertrack import aip_fetch, journal_fetch
    from papertrack.adapter_result import AdapterResult, OFFICIAL_EMPTY_SUSPICIOUS

    monkeypatch.setattr(
        aip_fetch,
        "query_aip_issue_result",
        lambda *args, **kwargs: AdapterResult(
            entries={},
            outcome=OFFICIAL_EMPTY_SUSPICIOUS,
            provider="aip",
            source="journal",
        ),
    )

    def fake_crossref_issue(*args, **kwargs):
        raise AssertionError("CrossRef must not define official issue membership")

    monkeypatch.setattr(journal_fetch, "_fetch_year", fake_crossref_issue)

    result = journal_fetch.query_journal_issue_full_result(
        "0021-9606",
        "163",
        "24",
        "The Journal of Chemical Physics",
        2025,
        provider="aip",
        journal_slug="jcp",
    )

    assert result.outcome == OFFICIAL_EMPTY_SUSPICIOUS
    assert result.entries == {}


@pytest.mark.parametrize(
    ("provider", "module_name", "function_name", "dispatch_kwargs"),
    (
        (
            "acs",
            "acs_fetch",
            "query_acs_issue_result",
            {"acs_code": "jcisd8", "journal_slug": "jcim"},
        ),
        (
            "aip",
            "aip_fetch",
            "query_aip_issue_result",
            {"provider": "aip", "journal_slug": "jcp"},
        ),
        (
            "aps",
            "aps_fetch",
            "query_aps_issue_result",
            {"provider": "aps", "journal_slug": "prl"},
        ),
    ),
)
def test_authoritative_dispatch_passes_identical_provider_runtime(
    monkeypatch, provider, module_name, function_name, dispatch_kwargs
):
    from papertrack import journal_fetch
    from papertrack.adapter_result import AdapterResult, SUCCESS

    adapter = __import__(f"papertrack.{module_name}", fromlist=[function_name])
    runtime = object()
    seen = []

    def query_issue(*args, **kwargs):
        seen.append(kwargs["provider_runtime"])
        return AdapterResult(
            entries={
                "10.0000/example": JournalEntry(
                    doi="10.0000/example",
                    title="Official article",
                    authors=[],
                    abstract="Official abstract",
                )
            },
            outcome=SUCCESS,
            provider=provider,
            source="journal",
        )

    monkeypatch.setattr(adapter, function_name, query_issue)
    result = journal_fetch.query_journal_issue_full_result(
        "1234-5678",
        "1",
        "2",
        "Example Journal",
        provider_runtime=runtime,
        **dispatch_kwargs,
    )

    assert result.ok
    assert seen == [runtime]


@pytest.mark.parametrize(
    ("provider", "journal_slug", "volume", "issue", "expected_url", "module_name"),
    (
        ("acs", "jcim", "66", "12", "https://pubs.acs.org/toc/jcisd8/66/12", "acs_fetch"),
        ("aip", "jcp", "163", "24", "https://pubs.aip.org/aip/jcp/issue/163/24", "aip_fetch"),
        ("aps", "prl", "136", "1", "https://journals.aps.org/prl/issues/136/1", "aps_fetch"),
    ),
)
def test_official_exception_records_complete_failure_context(
    monkeypatch,
    provider,
    journal_slug,
    volume,
    issue,
    expected_url,
    module_name,
):
    from papertrack import journal_fetch

    adapter = __import__(f"papertrack.{module_name}", fromlist=["adapter"])
    if provider == "acs":
        monkeypatch.setattr(
            adapter,
            "query_acs_issue_result",
            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("adapter boom")),
        )
        kwargs = {"acs_code": "jcisd8", "journal_slug": journal_slug}
    elif provider == "aip":
        monkeypatch.setattr(
            adapter,
            "query_aip_issue_result",
            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("adapter boom")),
        )
        kwargs = {"provider": "aip", "journal_slug": journal_slug}
    else:
        monkeypatch.setattr(
            adapter,
            "query_aps_issue_result",
            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("adapter boom")),
        )
        kwargs = {"provider": "aps", "journal_slug": journal_slug}

    events = []
    monkeypatch.setattr(
        journal_fetch,
        "record_fetch_attempt",
        lambda event, html=None: events.append(event),
    )

    result = journal_fetch.query_journal_issue_full_result(
        "1234-5678",
        volume,
        issue,
        "Example Journal",
        year=2026,
        **kwargs,
    )

    assert result.outcome == "fetch_failed"
    assert result.entries == {}
    assert len(events) == 1
    event = events[0]
    assert event["provider"] == provider
    assert event["journal"] == journal_slug
    assert event["volume"] == volume
    assert event["issue"] == issue
    assert event["url"] == expected_url
    assert event["source"] == "journal"
    assert event["source_role"] == "authoritative"
    assert event["stage"] == "issue_fetch"
    assert event["outcome"] == "fetch_failed"
    assert event["status"] is None
    assert event["status_code"] is None
    assert event["exception"] == "RuntimeError: adapter boom"
    assert event["message"] == "RuntimeError: adapter boom"
    assert event["context"] == {
        "issn": "1234-5678",
        "journal_name": "Example Journal",
        "adapter": provider,
    }


@pytest.mark.parametrize(
    ("provider", "journal_slug", "expected_url"),
    (
        ("mystery", "", "unconfigured://mystery/Example%20Journal/1/2"),
        ("aip", "", "unconfigured://aip/Example%20Journal/1/2"),
    ),
)
def test_no_adapter_failure_uses_explicit_unconfigured_issue_url(
    monkeypatch, provider, journal_slug, expected_url
):
    from papertrack import journal_fetch

    events = []
    monkeypatch.setattr(
        journal_fetch,
        "record_fetch_attempt",
        lambda event, html=None: events.append(event),
    )

    result = journal_fetch.query_journal_issue_full_result(
        "1234-5678",
        "1",
        "2",
        "Example Journal",
        provider=provider,
        journal_slug=journal_slug,
    )

    assert result.outcome == "fetch_failed"
    assert len(events) == 1
    event = events[0]
    assert event["url"] == expected_url
    assert event["url"]
    assert event["provider"] == provider
    assert event["journal"] == "Example Journal"
    assert event["volume"] == "1"
    assert event["issue"] == "2"
    assert event["status"] is None
    assert event["stage"] == "issue_fetch"
    assert event["source_role"] == "authoritative"
    assert event["outcome"] == "fetch_failed"


def test_crossref_non_article_issue_metadata_is_filtered():
    from papertrack import journal_fetch

    journal_fetch.clear_cache()
    metadata_item = {
        "DOI": "10.1021/civ066i011_2087001",
        "title": ["Issue Editorial Masthead"],
        "volume": "66",
        "issue": "11",
    }
    article_item = {
        "DOI": "10.1021/acs.jcim.6c00648",
        "title": ["PyRMD Studio"],
        "volume": "66",
        "issue": "11",
    }

    articles = [
        item
        for item in [metadata_item, article_item]
        if not JournalEntry.is_non_article(item["DOI"], item["title"][0])
    ]

    assert articles == [article_item]


def test_acs_issue_page_defines_membership_and_records_absent_abstract(
    monkeypatch, tmp_path
):
    from papertrack import acs_fetch
    from papertrack.official_snapshot import OfficialSnapshotStore
    from papertrack.provider_runtime import OfficialBrowserResult

    acs_fetch._cache.clear()
    html = """
    <div class="issue-item clearfix">
      <h3 class="issue-item_title">
        <a href="/doi/10.1021/acs.jcim.6c00001">Research article</a>
      </h3>
      <span class="hlFld-ContribAuthor"><given-names>Ada</given-names><surname>Lovelace</surname></span>
      <span class="hlFld-Abstract">Official abstract.</span>
      <div class="issue-item_info">
        <span class="issue-item_vol-num">66</span>, 12, 100-110 Publication Date: June 1, 2026
      </div>
    </div>
    <div class="issue-item clearfix">
      <h4 class="issue-item_title">
        <a href="/doi/10.1021/acs.jcim.6c00002">Correction</a>
      </h4>
      <span class="hlFld-ContribAuthor"><given-names>Grace</given-names><surname>Hopper</surname></span>
      <div class="issue-item_info">
        <span class="issue-item_vol-num">66</span>, 12, 111-112 Publication Date: June 1, 2026
      </div>
    </div>
    """
    fetched = []
    events = []

    class Runtime:
        snapshot_store = OfficialSnapshotStore(tmp_path / "snapshots")

        def fetch_issue(self, request):
            fetched.append(request.canonical_url)
            return OfficialBrowserResult(
                outcome="success",
                canonical_url=request.canonical_url,
                final_url=request.canonical_url,
                status_code=200,
                html=html,
                captured=(),
            )

    monkeypatch.setattr(
        acs_fetch,
        "record_fetch_attempt",
        lambda event, html=None: events.append(event),
    )

    result = acs_fetch.query_acs_issue_result(
        "jcisd8", "66", "12", "JCIM", Runtime()
    )

    assert list(result.entries) == [
        "10.1021/acs.jcim.6c00001",
        "10.1021/acs.jcim.6c00002",
    ]
    assert result.entries["10.1021/acs.jcim.6c00001"].authors == ["Ada Lovelace"]
    assert result.entries["10.1021/acs.jcim.6c00002"].abstract == ""
    assert result.metadata["article_count"] == 2
    assert result.metadata["missing_fields"] == [
        {"doi": "10.1021/acs.jcim.6c00002", "field": "abstract"}
    ]
    assert fetched == ["https://pubs.acs.org/toc/jcisd8/66/12"]
    assert [event for event in events if event.get("outcome") == "official_field_absent"] == [
        {
            "source_role": "authoritative",
            "source": "journal",
            "provider": "acs",
            "journal": "jcisd8",
            "volume": "66",
            "issue": "12",
            "doi": "10.1021/acs.jcim.6c00002",
            "field": "abstract",
            "url": "https://pubs.acs.org/toc/jcisd8/66/12",
            "stage": "issue_page_parse",
            "outcome": "official_field_absent",
        }
    ]


def test_acs_loi_uses_shared_provider_request_floor(monkeypatch):
    from papertrack import acs_loi
    from papertrack.http_fetch import HttpFetchResult

    captured = {}

    def fake_fetch(url, **kwargs):
        captured.update(kwargs)
        return HttpFetchResult(html="<html>official LOI</html>", status_code=200)

    monkeypatch.setattr(acs_loi, "fetch_html_resilient", fake_fetch)

    assert acs_loi._scrape("https://pubs.acs.org/loi/jcisd8") == (
        "<html>official LOI</html>"
    )
    assert captured["provider"] == "acs"
    assert captured["request_policy"].min_interval == 30.0
    assert captured["request_policy"].max_attempts == 3


def test_aip_uses_legacy_issue_url_only_after_canonical_404(monkeypatch):
    from papertrack import aip_fetch
    from papertrack.adapter_result import FETCH_FAILED
    from papertrack.http_fetch import HttpFetchResult

    aip_fetch.clear_aip_caches()
    calls = []

    def fake_fetch(url):
        calls.append(url)
        return HttpFetchResult(
            outcome=FETCH_FAILED,
            status_code=404,
        )

    monkeypatch.setattr(aip_fetch, "_fetch_issue_http", fake_fetch)

    result = aip_fetch.query_aip_issue_result("jcp", "164", "16")

    assert result.outcome == FETCH_FAILED
    assert calls == [
        "https://pubs.aip.org/aip/jcp/issue/164/16",
        "https://pubs.aip.org/jcp/issue/164/16",
    ]


def test_query_aip_issue_uses_official_abstract_ajax_not_article_pages(monkeypatch):
    from papertrack import aip_fetch
    from papertrack.adapter_result import SUCCESS
    from papertrack.http_fetch import HttpFetchResult

    aip_fetch.clear_aip_caches()
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

    def fake_fetch(url):
        fetched_urls.append(url)
        if "/article/" in url:
            raise AssertionError("query_aip_issue must not fetch article pages")
        return HttpFetchResult(html=html, status_code=200)

    def fake_fetch_abstract(article_id, referer_url, journal_slug):
        fetched_abstract_ids.append((article_id, referer_url))
        return aip_fetch.AipAbstractResult(
            abstract="Official abstract text.",
            outcome=SUCCESS,
            status_code=200,
            url=aip_fetch.aip_abstract_url(journal_slug),
            article_id=article_id,
        )

    monkeypatch.setattr(aip_fetch, "_fetch_issue_http", fake_fetch)
    monkeypatch.setattr(aip_fetch, "_fetch_aip_abstract", fake_fetch_abstract)

    entries = aip_fetch.query_aip_issue("jcp", "164", "16", "Journal of Chemical Physics")

    assert list(entries) == ["10.1063/example"]
    assert entries["10.1063/example"].abstract == "Official abstract text."
    assert fetched_urls == ["https://pubs.aip.org/aip/jcp/issue/164/16"]
    assert fetched_abstract_ids == [("3387571", "https://pubs.aip.org/aip/jcp/issue/164/16")]


def test_query_aip_issue_does_not_cache_empty_fetch_failures(monkeypatch):
    from papertrack import aip_fetch
    from papertrack.adapter_result import FETCH_FAILED
    from papertrack.http_fetch import HttpFetchResult

    aip_fetch.clear_aip_caches()
    calls = []

    def fake_fetch(url):
        calls.append(url)
        return HttpFetchResult(outcome=FETCH_FAILED)

    monkeypatch.setattr(aip_fetch, "_fetch_issue_http", fake_fetch)

    assert aip_fetch.query_aip_issue("jcp", "164", "16") == {}
    assert aip_fetch.query_aip_issue("jcp", "164", "16") == {}
    assert calls == [
        "https://pubs.aip.org/aip/jcp/issue/164/16",
        "https://pubs.aip.org/aip/jcp/issue/164/16",
    ]


def test_query_aip_issue_records_official_empty_suspicious(monkeypatch, tmp_path):
    from papertrack import aip_fetch
    from papertrack.diagnostics import configure_diagnostics
    from papertrack.http_fetch import HttpFetchResult

    aip_fetch.clear_aip_caches()
    configure_diagnostics(diagnostics_dir=str(tmp_path))
    html = "<html><body><main>No article items today</main></body></html>"

    monkeypatch.setattr(
        aip_fetch,
        "_fetch_issue_http",
        lambda *args, **kwargs: HttpFetchResult(html=html, status_code=200),
    )

    entries = aip_fetch.query_aip_issue("jcp", "164", "16", "Journal of Chemical Physics")

    assert entries == {}
    attempts = (tmp_path / "attempts.jsonl").read_text(encoding="utf-8")
    assert '"outcome": "official_empty_suspicious"' in attempts
    assert '"provider": "aip"' in attempts
    snapshot_files = list((tmp_path / "snapshots" / "aip").glob("*.html"))
    assert len(snapshot_files) == 1
    assert snapshot_files[0].read_text(encoding="utf-8") == html


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

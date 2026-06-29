"""Unit tests for arxiv_entry, report, and arxiv_index_fetch modules"""
from bs4 import BeautifulSoup

from papertrack.arxiv_entry import ArxivEntry
from papertrack.arxiv_index_fetch import _parse_arxiv_result
from papertrack.journal_entry import JournalEntry
from papertrack.report import (
    parse_old_report,
    _gen_oneday_markdown,
    _render_journal_paper,
    _render_journal_report,
    parse_journal_report,
    _is_missing_or_corrupt_journal_report,
    filter_journal_to_md,
    filter_journal_auto,
)
from papertrack.zotero_query import ZoteroQuery


class TestArxivEntry:
    """Test ArxivEntry properties and utilities"""

    def test_properties(self):
        entry = ArxivEntry(arxiv_id="arXiv:2502.07673", title="T", authors=[], abstract="")
        assert entry.bare_id == "2502.07673"
        assert entry.arxiv_doi == "10.48550/arXiv.2502.07673"
        assert entry.arxiv_url == "https://arxiv.org/abs/2502.07673"

    def test_bare_id_from_url(self):
        assert ArxivEntry.bare_id_from_url("https://arxiv.org/abs/2502.07673") == "2502.07673"
        assert ArxivEntry.bare_id_from_url("https://arxiv.org/abs/2510.26887v2") == "2510.26887"
        assert ArxivEntry.bare_id_from_url("https://pubs.aip.org/jcp/article/...") is None


class TestParseOldReport:
    """Test parse_old_report function"""

    def test_parse_nonexistent_file(self):
        assert parse_old_report("/nonexistent/file.md") is None

    def test_parse_valid_report(self, tmp_path):
        content = """# 2025-02-01 preprint by arxiv_tools

## collected

### arXiv:2502.07673

Links:
- [x] [[#arXiv:2502.07673]]
Title:  Test Title
Authors:  Author 1, Author 2

## not collected

### arXiv:2502.07674

Links:
- [ ] [[#arXiv:2502.07674]]
Title:  Another Test
Authors:  Author 3

## update

- [x] [[#arXiv:2502.07673]]
- [ ] [[#arXiv:2502.07674]]
"""
        test_file = tmp_path / "test.md"
        test_file.write_text(content)

        result = parse_old_report(str(test_file))
        assert "arXiv:2502.07673" in result
        assert "arXiv:2502.07674" in result

    def test_parse_corrupt_report_returns_none(self, tmp_path):
        test_file = tmp_path / "bad.md"
        test_file.write_text("# broken\n\nno generated sections here", encoding="utf-8")

        assert parse_old_report(str(test_file)) is None


class TestJournalReportValidation:
    def test_missing_journal_report_is_rebuild_candidate(self, tmp_path):
        assert _is_missing_or_corrupt_journal_report(str(tmp_path), "jcp", "164", "16") is True

    def test_corrupt_journal_report_is_rebuild_candidate(self, tmp_path):
        report_file = tmp_path / "jcp" / "164" / "16.md"
        report_file.parent.mkdir(parents=True)
        report_file.write_text("# broken\n", encoding="utf-8")

        assert _is_missing_or_corrupt_journal_report(str(tmp_path), "jcp", "164", "16") is True

    def test_task_edits_do_not_make_journal_report_corrupt(self, tmp_path):
        report_file = tmp_path / "jcp" / "164" / "16.md"
        report_file.parent.mkdir(parents=True)
        report_file.write_text(
            """# Journal of Chemical Physics Vol. 164, Iss. 16 — 2026

---
tags:
  - #jcp-v164-i16
---

```dataview
TASK
from #jcp-v164-i16

WHERE completed

```

## collected

## not collected

### 10.1063/example

Links:

- [x] [10.1063/example](https://doi.org/10.1063/example)

Title:  Test article
""",
            encoding="utf-8",
        )

        assert parse_journal_report(str(report_file)) == ["10.1063/example"]
        assert _is_missing_or_corrupt_journal_report(str(tmp_path), "jcp", "164", "16") is False

    def test_auto_rebuilds_processed_issue_when_file_missing(self, tmp_path, monkeypatch):
        from papertrack import crossref_loi, report

        state = {"jcp": {"processed": [[164, 16]]}}
        rebuilt = []

        monkeypatch.setattr(report, "_load_state", lambda md_folder: state)
        monkeypatch.setattr(report, "_save_state", lambda md_folder, new_state: state.update(new_state))
        monkeypatch.setattr(crossref_loi, "discover_issues_crossref", lambda issn, from_year=0: [(2026, 164, 16)])
        monkeypatch.setattr(
            report,
            "filter_journal_to_md",
            lambda **kwargs: rebuilt.append((kwargs["volume"], kwargs["issue"])),
        )

        filter_journal_auto(
            journal_name="Journal of Chemical Physics",
            journal_slug="jcp",
            issn="0021-9606",
            acs_code="",
            md_folder=str(tmp_path),
            provider="aip",
        )

        assert rebuilt == [("164", "16")]

    def test_auto_rebuilds_missing_older_processed_issue_in_normal_mode(self, tmp_path, monkeypatch):
        from papertrack import crossref_loi, report

        valid_latest = tmp_path / "jcp" / "164" / "16.md"
        valid_latest.parent.mkdir(parents=True)
        valid_latest.write_text(
            """# Journal of Chemical Physics Vol. 164, Iss. 16 — 2026

## collected

## not collected

### 10.1063/latest

Title:  Latest article
""",
            encoding="utf-8",
        )
        rebuilt = []

        monkeypatch.setattr(report, "_load_state", lambda md_folder: {"jcp": {"processed": [[163, 24], [164, 16]]}})
        monkeypatch.setattr(report, "_save_state", lambda md_folder, new_state: None)
        monkeypatch.setattr(crossref_loi, "discover_issues_crossref", lambda issn, from_year=0: [(2026, 164, 16)])
        monkeypatch.setattr(
            report,
            "filter_journal_to_md",
            lambda **kwargs: rebuilt.append((kwargs["volume"], kwargs["issue"])),
        )

        filter_journal_auto(
            journal_name="Journal of Chemical Physics",
            journal_slug="jcp",
            issn="0021-9606",
            acs_code="",
            md_folder=str(tmp_path),
            provider="aip",
        )

        assert rebuilt == [("163", "24")]

    def test_auto_skips_processed_issue_when_file_is_valid(self, tmp_path, monkeypatch):
        from papertrack import crossref_loi, report

        report_file = tmp_path / "jcp" / "164" / "16.md"
        report_file.parent.mkdir(parents=True)
        report_file.write_text(
            """# Journal of Chemical Physics Vol. 164, Iss. 16 — 2026

## collected

## not collected

### 10.1063/example

Title:  Test article
""",
            encoding="utf-8",
        )
        rebuilt = []

        monkeypatch.setattr(report, "_load_state", lambda md_folder: {"jcp": {"processed": [[164, 16]]}})
        monkeypatch.setattr(report, "_save_state", lambda md_folder, new_state: None)
        monkeypatch.setattr(crossref_loi, "discover_issues_crossref", lambda issn, from_year=0: [(2026, 164, 16)])
        monkeypatch.setattr(
            report,
            "filter_journal_to_md",
            lambda **kwargs: rebuilt.append((kwargs["volume"], kwargs["issue"])),
        )

        filter_journal_auto(
            journal_name="Journal of Chemical Physics",
            journal_slug="jcp",
            issn="0021-9606",
            acs_code="",
            md_folder=str(tmp_path),
            provider="aip",
        )

        assert rebuilt == []


class TestJournalRendering:
    def test_journal_rendering_uses_schema_and_omits_toc_image(self):
        entry = JournalEntry(
            doi="10.1063/example",
            title="Dense report article",
            authors=["A Author"],
            abstract="Abstract text",
            toc_image_url="https://example.test/toc.jpg",
        )

        paper = _render_journal_paper(entry)
        report = _render_journal_report(
            "Journal of Chemical Physics",
            "jcp",
            "164",
            "16",
            "2026",
            "aip",
            [],
            [paper],
            [],
        )

        assert report.startswith("---\npapertrack_format: journal-v2")
        assert "papertrack_provider: aip" in report
        assert "https://example.test/toc.jpg" not in report

    def test_filter_journal_to_md_preserves_existing_task_state(self, tmp_path, monkeypatch):
        from papertrack import report

        report_file = tmp_path / "jcp" / "164" / "16.md"
        report_file.parent.mkdir(parents=True)
        report_file.write_text(
            """# Journal of Chemical Physics Vol. 164, Iss. 16 — 2026

## collected

## not collected

### 10.1063/example

Links:

- [x] [10.1063/example](https://doi.org/10.1063/example) ✅ 2026-06-09

Title:  Old title
""",
            encoding="utf-8",
        )
        entry = JournalEntry(
            doi="10.1063/example",
            title="Updated title",
            authors=["A Author"],
            abstract="Abstract text",
            date="2026",
        )
        monkeypatch.setattr(
            report,
            "query_journal_issue_full",
            lambda *args, **kwargs: {entry.doi: entry},
        )

        filter_journal_to_md(
            journal_name="Journal of Chemical Physics",
            journal_slug="jcp",
            volume="164",
            issue="16",
            issn="0021-9606",
            md_folder=str(tmp_path),
            provider="aip",
        )

        rendered = report_file.read_text(encoding="utf-8")
        assert "- [x] [10.1063/example](https://doi.org/10.1063/example) ✅ 2026-06-09" in rendered
        assert "Updated title" in rendered
        assert not report_file.with_suffix(".md.bak").exists()

    def test_filter_journal_to_md_infers_acs_provider_for_schema(self, tmp_path, monkeypatch):
        from papertrack import report

        entry = JournalEntry(
            doi="10.1021/example",
            title="ACS article",
            authors=["A Author"],
            abstract="Abstract text",
            date="2026",
        )
        monkeypatch.setattr(
            report,
            "query_journal_issue_full",
            lambda *args, **kwargs: {entry.doi: entry},
        )

        filter_journal_to_md(
            journal_name="Journal of Chemical Theory and Computation",
            journal_slug="jctc",
            volume="22",
            issue="8",
            issn="1549-9618",
            md_folder=str(tmp_path),
            acs_code="jctcce",
        )

        rendered = (tmp_path / "jctc" / "22" / "8.md").read_text(encoding="utf-8")
        assert "papertrack_provider: acs" in rendered


class TestGenOnedayMarkdown:
    """Test markdown report generation"""

    def test_markdown_structure(self):
        arxiv_dict = {
            "arXiv:2502.07673": ArxivEntry(
                arxiv_id="arXiv:2502.07673",
                title="Test Title",
                authors=["Author 1", "Author 2"],
                abstract="Test abstract",
            ),
            "arXiv:2502.07674": ArxivEntry(
                arxiv_id="arXiv:2502.07674",
                title="Another Title",
                authors=["Author 3"],
                abstract="Another abstract",
            ),
        }

        result = _gen_oneday_markdown("2025-02-01", "quant-ph", arxiv_dict, None)

        assert "2025-02-01" in result
        assert "#quant-ph-2025-02-01" in result
        assert "## collected" in result
        assert "## not collected" in result
        assert "Test Title" in result

    def test_update_section_when_new_papers(self):
        arxiv_dict = {
            "arXiv:2502.07673": ArxivEntry(
                arxiv_id="arXiv:2502.07673", title="T", authors=[], abstract=""),
            "arXiv:2502.99999": ArxivEntry(
                arxiv_id="arXiv:2502.99999", title="New Paper", authors=[], abstract=""),
        }

        result = _gen_oneday_markdown(
            "2025-02-01", "quant-ph", arxiv_dict, None,
            old_data=["arXiv:2502.07673"],  # only the first was in old report
        )

        assert "## update" in result
        assert "arXiv:2502.99999" in result
        assert "arXiv:2502.07673" not in result.split("## update")[1] if "## update" in result else True


class TestParseArxivResult:
    """Test _parse_arxiv_result (HTML → ArxivEntry)"""

    def test_extracts_journal_ref_and_comments(self):
        html = """<li class="arxiv-result">
 <p class="list-title is-inline-block"><a href="https://arxiv.org/abs/2406.01974">arXiv:2406.01974</a></p>
 <p class="title is-5 mathjax">Test Title</p>
 <p class="authors"><a href="...">A Author</a></p>
 <p class="abstract mathjax"><span class="abstract-full">Some abstract.</span></p>
 <p class="comments is-size-7">Comments: 5 pages</p>
 <p class="comments is-size-7">Journal ref: Phys. Rev. Lett. 134, 010201 (2025)</p>
</li>"""

        soup = BeautifulSoup(html, "lxml")
        entry = _parse_arxiv_result(soup.find("li"))

        assert entry is not None
        assert entry.journal_ref == "Phys. Rev. Lett. 134, 010201 (2025)"
        assert entry.comments == "5 pages"

    def test_extracts_external_doi(self):
        html = """<li class="arxiv-result">
 <p class="list-title is-inline-block"><a href="https://arxiv.org/abs/2406.01974">arXiv:2406.01974</a></p>
 <p class="title is-5 mathjax">Test</p>
 <p class="authors"><a href="...">Author</a></p>
 <p class="abstract mathjax"><span class="abstract-full">Abs.</span></p>
 <span class="tag is-dark is-size-7">doi</span>
 <span class="tag is-light is-size-7"><a href="https://doi.org/10.1038/s42005-025-01947-z">10.1038/s42005-025-01947-z</a></span>
 <span class="tag is-small is-link tooltip is-tooltip-top">quant-ph</span>
</li>"""

        soup = BeautifulSoup(html, "lxml")
        entry = _parse_arxiv_result(soup.find("li"))

        assert entry is not None
        assert entry.external_doi == "10.1038/s42005-025-01947-z"
        assert entry.external_doi_url == "https://doi.org/10.1038/s42005-025-01947-z"
        assert entry.subjects == ["quant-ph"]


class TestZoteroQuery:
    """Test ZoteroQuery index building and matching logic"""

    def test_match_entry_reports_three_layer_exact_matches(self, monkeypatch):
        class FakeZotero:
            def everything(self, items):
                return items

            def top(self):
                return [
                    {
                        "key": "A1",
                        "data": {
                            "itemType": "preprint",
                            "DOI": "10.48550/ARXIV.2502.07673",
                            "url": "",
                        },
                    },
                    {
                        "key": "B2",
                        "data": {
                            "itemType": "journalArticle",
                            "DOI": "10.1103/PhysRevLett.134.010201",
                            "url": "",
                        },
                    },
                    {
                        "key": "C3",
                        "data": {
                            "itemType": "webpage",
                            "DOI": "",
                            "url": "https://arxiv.org/abs/2502.99999",
                        },
                    },
                ]

        zq = ZoteroQuery()
        zq.zot = FakeZotero()
        zq._ensure_index()

        # Layer 1: arXiv DOI match
        e1 = ArxivEntry(arxiv_id="arXiv:2502.07673", title="T", authors=[], abstract="")
        m1 = zq.match_entry(e1)
        assert m1.is_collected is True
        assert m1.match_kind == "arxiv_doi"
        assert m1.zotero_key == "A1"
        assert m1.zotero_item_type == "preprint"

        # Layer 2: external DOI match
        e2 = ArxivEntry(arxiv_id="arXiv:9999.99999", title="T", authors=[], abstract="",
                        external_doi="10.1103/physrevlett.134.010201")
        m2 = zq.match_entry(e2)
        assert m2.is_collected is True
        assert m2.match_kind == "external_doi"
        assert m2.zotero_key == "B2"
        assert m2.zotero_item_type == "journalArticle"

        # Layer 3: arXiv ID from URL match (webpage-type item without DOI)
        e3 = ArxivEntry(arxiv_id="arXiv:2502.99999", title="T", authors=[], abstract="")
        m3 = zq.match_entry(e3)
        assert m3.is_collected is True
        assert m3.match_kind == "arxiv_url"
        assert m3.zotero_key == "C3"
        assert m3.zotero_item_type == "webpage"

        # No match
        e4 = ArxivEntry(arxiv_id="arXiv:8888.88888", title="T", authors=[], abstract="")
        assert zq.match_entry(e4).is_collected is False

        # Backwards-compatible bool wrapper
        assert zq.find_by_entry(e1) is True

    def test_match_doi_reports_publisher_doi_match(self, monkeypatch):
        class FakeZotero:
            def everything(self, items):
                return items

            def top(self):
                return [
                    {
                        "key": "J1",
                        "data": {
                            "itemType": "journalArticle",
                            "DOI": "10.1063/5.0323573",
                            "url": "",
                        },
                    }
                ]

        zq = ZoteroQuery()
        zq.zot = FakeZotero()
        zq._ensure_index()

        match = zq.match_doi("10.1063/5.0323573")

        assert match.is_collected is True
        assert match.match_kind == "publisher_doi"
        assert match.zotero_key == "J1"
        assert match.zotero_item_type == "journalArticle"
        assert zq.find_by_doi("10.1063/5.0323573") is True

    def test_index_not_built_when_zotero_unavailable(self, monkeypatch):
        class BrokenZotero:
            def top(self):
                raise ConnectionError("Zotero not running")

        zq = ZoteroQuery()
        zq.zot = BrokenZotero()
        zq._ensure_index()

        e = ArxivEntry(arxiv_id="arXiv:2502.07673", title="T", authors=[], abstract="")
        assert zq.find_by_entry(e) is False
        assert zq.match_entry(e).is_collected is False


from datetime import date


def test_filter_arxiv_days_to_md_processes_exact_days(tmp_path, monkeypatch):
    from papertrack import report
    from papertrack.arxiv_entry import ArxivEntry

    calls = []

    def fake_query(date_from_date, date_to_date, query_args):
        calls.append((date_from_date, date_to_date))
        return {
            "arXiv:2606.00001": ArxivEntry(
                arxiv_id="arXiv:2606.00001",
                title=f"Paper for {date_from_date}",
                authors=["A Author"],
                abstract="Abstract text",
            )
        }

    monkeypatch.setattr(report, "query_arxiv_dict", fake_query)

    report.filter_arxiv_days_to_md(
        days=[date(2026, 6, 8), date(2026, 6, 9)],
        md_folder=str(tmp_path / "arxiv" / "quant-ph"),
        query_args={"size": "200"},
        category="quant-ph",
        zotero=None,
    )

    assert calls == [
        ("2026-06-08", "2026-06-09"),
        ("2026-06-09", "2026-06-10"),
    ]
    assert (tmp_path / "arxiv" / "quant-ph" / "2026" / "06" / "08.md").exists()
    assert (tmp_path / "arxiv" / "quant-ph" / "2026" / "06" / "09.md").exists()


def test_filter_arxiv_days_to_md_skips_empty_days(tmp_path, monkeypatch):
    from papertrack import report

    monkeypatch.setattr(report, "query_arxiv_dict", lambda *args, **kwargs: {})

    report.filter_arxiv_days_to_md(
        days=[date(2026, 6, 9)],
        md_folder=str(tmp_path / "arxiv" / "quant-ph"),
        query_args={"size": "200"},
        category="quant-ph",
        zotero=None,
    )

    assert not (tmp_path / "arxiv" / "quant-ph" / "2026" / "06" / "09.md").exists()

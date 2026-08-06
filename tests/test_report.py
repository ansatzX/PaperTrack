"""Unit tests for arxiv_entry, report, and arxiv_index_fetch modules"""
from bs4 import BeautifulSoup
import pytest

from papertrack.arxiv_entry import ArxivEntry
from papertrack.arxiv_index_fetch import _parse_arxiv_page, _parse_arxiv_result
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
from papertrack.source_policy import SourcePolicy
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

    def test_issue_metadata_journal_report_is_rebuild_candidate(self, tmp_path):
        report_file = tmp_path / "jcim" / "66" / "11.md"
        report_file.parent.mkdir(parents=True)
        report_file.write_text(
            """---
papertrack_format: journal-v2
papertrack_source: journal
papertrack_provider: acs
papertrack_journal: jcim
papertrack_volume: "66"
papertrack_issue: "11"
---

```dataview
TASK
from #jcim-v66-i11
```

## collected

## not collected

### 10.1021/civ066i011_2087001

Title:  Issue Editorial Masthead
""",
            encoding="utf-8",
        )

        assert _is_missing_or_corrupt_journal_report(str(tmp_path), "jcim", "66", "11") is True

    def test_aps_crossref_fallback_report_is_rebuild_candidate(self, tmp_path):
        report_file = tmp_path / "prl" / "127" / "27.md"
        report_file.parent.mkdir(parents=True)
        report_file.write_text(
            """---
papertrack_format: journal-v2
papertrack_source: journal
papertrack_provider: aps
papertrack_journal: prl
papertrack_volume: "127"
papertrack_issue: "27"
---

```dataview
TASK
from #prl-v127-i27
```

## collected

## not collected

### 10.1103/physrevlett.127.270401

Title:  Raw <mml:math></mml:math> title
""",
            encoding="utf-8",
        )

        assert _is_missing_or_corrupt_journal_report(str(tmp_path), "prl", "127", "27") is True

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
            source_policy=SourcePolicy.from_environment(
                {"PAPERTRACK_CROSSREF_MODE": "metadata"}
            ),
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
            source_policy=SourcePolicy.from_environment(
                {"PAPERTRACK_CROSSREF_MODE": "metadata"}
            ),
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
            source_policy=SourcePolicy.from_environment(
                {"PAPERTRACK_CROSSREF_MODE": "metadata"}
            ),
        )

        assert rebuilt == []

    def test_auto_does_not_mark_new_issue_processed_when_report_write_fails(self, tmp_path, monkeypatch):
        from papertrack import crossref_loi, report

        state = {}

        monkeypatch.setattr(report, "_load_state", lambda md_folder: state)
        monkeypatch.setattr(report, "_save_state", lambda md_folder, new_state: state.update(new_state))
        monkeypatch.setattr(crossref_loi, "discover_issues_crossref", lambda issn, from_year=0: [(2026, 164, 16)])
        monkeypatch.setattr(report, "filter_journal_to_md", lambda **kwargs: False)

        filter_journal_auto(
            journal_name="Journal of Chemical Physics",
            journal_slug="jcp",
            issn="0021-9606",
            acs_code="",
            md_folder=str(tmp_path),
            provider="aip",
            source_policy=SourcePolicy.from_environment(
                {"PAPERTRACK_CROSSREF_MODE": "metadata"}
            ),
        )

        assert state == {"jcp": {"processed": []}}

    def test_aip_auto_official_only_requires_explicit_issue(self, tmp_path, monkeypatch):
        from papertrack import crossref_loi, report

        monkeypatch.setattr(report, "_load_state", lambda md_folder: {})
        monkeypatch.setattr(
            crossref_loi,
            "discover_issues_crossref",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("official-only mode must not call CrossRef discovery")
            ),
        )

        with pytest.raises(RuntimeError, match=r"journal issue.*--volume.*--issue"):
            filter_journal_auto(
                journal_name="Journal of Chemical Physics",
                journal_slug="jcp",
                issn="0021-9606",
                acs_code="",
                md_folder=str(tmp_path),
                provider="aip",
                source_policy=SourcePolicy.official_only(),
            )

    def test_acs_auto_falls_back_to_crossref_when_loi_fails(self, tmp_path, monkeypatch):
        from papertrack import acs_loi, crossref_loi, report

        state = {}
        processed = []
        monkeypatch.setattr(report, "_load_state", lambda md_folder: state)
        monkeypatch.setattr(
            report, "_save_state",
            lambda md_folder, new_state: state.update(new_state),
        )
        monkeypatch.setattr(
            acs_loi,
            "discover_issues",
            lambda acs_code, from_year=0: (_ for _ in ()).throw(
                RuntimeError("ACS LOI blocked")
            ),
        )
        monkeypatch.setattr(
            crossref_loi,
            "discover_issues_crossref",
            lambda issn, from_year=0: [(2026, 22, 14), (2026, 22, 13)],
        )
        monkeypatch.setattr(
            report,
            "filter_journal_to_md",
            lambda **kwargs: processed.append((kwargs["volume"], kwargs["issue"])) or True,
        )

        filter_journal_auto(
            journal_name="Journal of Chemical Theory and Computation",
            journal_slug="jctc",
            issn="1549-9618",
            acs_code="jctcce",
            md_folder=str(tmp_path),
            provider="acs",
            backfill=True,
            source_policy=SourcePolicy.official_only(),
        )

        # backfill re-sorts newest-first discovery into oldest-first processing.
        assert processed == [("22", "13"), ("22", "14")]
        assert state["jctc"]["processed"] == [[22, 13], [22, 14]]

    def test_acs_latest_falls_back_to_crossref_when_latest_fails(self, tmp_path, monkeypatch):
        from papertrack import acs_loi, crossref_loi, report

        processed = []
        monkeypatch.setattr(report, "_load_state", lambda md_folder: {})
        monkeypatch.setattr(report, "_save_state", lambda md_folder, new_state: None)
        monkeypatch.setattr(
            acs_loi,
            "get_latest_issue",
            lambda acs_code: (_ for _ in ()).throw(RuntimeError("ACS LOI blocked")),
        )
        monkeypatch.setattr(
            crossref_loi,
            "discover_issues_crossref",
            lambda issn, from_year=0: [(2026, 22, 14), (2026, 22, 13)],
        )
        monkeypatch.setattr(
            report,
            "filter_journal_to_md",
            lambda **kwargs: processed.append((kwargs["volume"], kwargs["issue"])) or True,
        )

        filter_journal_auto(
            journal_name="Journal of Chemical Theory and Computation",
            journal_slug="jctc",
            issn="1549-9618",
            acs_code="jctcce",
            md_folder=str(tmp_path),
            provider="acs",
            backfill=False,
            source_policy=SourcePolicy.official_only(),
        )

        # latest mode processes only the newest CrossRef-discovered issue.
        assert processed == [("22", "14")]

    def test_aps_auto_falls_back_to_crossref_when_issue_pages_fail(self, tmp_path, monkeypatch):
        from papertrack import aps_loi, crossref_loi, report

        state = {}
        processed = []
        monkeypatch.setattr(report, "_load_state", lambda md_folder: state)
        monkeypatch.setattr(
            report, "_save_state",
            lambda md_folder, new_state: state.update(new_state),
        )
        monkeypatch.setattr(
            aps_loi,
            "discover_issues_aps",
            lambda journal_slug, from_year=0: (_ for _ in ()).throw(
                RuntimeError("APS issue pages blocked")
            ),
        )
        monkeypatch.setattr(
            crossref_loi,
            "discover_issues_crossref",
            lambda issn, from_year=0: [(2026, 137, 1), (2026, 136, 24)],
        )
        monkeypatch.setattr(
            report,
            "filter_journal_to_md",
            lambda **kwargs: processed.append((kwargs["volume"], kwargs["issue"])) or True,
        )

        filter_journal_auto(
            journal_name="Physical Review Letters",
            journal_slug="prl",
            issn="0031-9007",
            acs_code="",
            md_folder=str(tmp_path),
            provider="aps",
            backfill=True,
            source_policy=SourcePolicy.official_only(),
        )

        assert processed == [("136", "24"), ("137", "1")]
        assert state["prl"]["processed"] == [[136, 24], [137, 1]]


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
        assert "tags:\n  - jcp-v164-i16" in report
        assert "from #jcp-v164-i16" in report
        assert "WHERE completed" in report
        assert "  - #jcp-v164-i16" not in report
        assert "https://example.test/toc.jpg" not in report

    def test_filter_journal_to_md_preserves_existing_task_state(self, tmp_path, monkeypatch):
        from papertrack import report
        from papertrack.adapter_result import AdapterResult, SUCCESS

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
            "query_journal_issue_full_result",
            lambda *args, **kwargs: AdapterResult(
                entries={entry.doi: entry},
                outcome=SUCCESS,
                provider="aip",
                source="journal",
            ),
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

    def test_filter_journal_to_md_passes_identical_provider_runtime(
        self, tmp_path, monkeypatch
    ):
        from papertrack import report
        from papertrack.adapter_result import AdapterResult, FETCH_FAILED

        runtime = object()
        seen = []
        monkeypatch.setattr(
            report,
            "query_journal_issue_full_result",
            lambda *args, **kwargs: seen.append(kwargs["provider_runtime"])
            or AdapterResult(
                entries={},
                outcome=FETCH_FAILED,
                provider="aip",
                source="journal",
            ),
        )

        assert not filter_journal_to_md(
            journal_name="Journal of Chemical Physics",
            journal_slug="jcp",
            volume="164",
            issue="16",
            issn="0021-9606",
            md_folder=str(tmp_path),
            provider="aip",
            provider_runtime=runtime,
        )
        assert seen == [runtime]

    def test_filter_journal_to_md_infers_acs_provider_for_schema(self, tmp_path, monkeypatch):
        from papertrack import report
        from papertrack.adapter_result import AdapterResult, SUCCESS

        entry = JournalEntry(
            doi="10.1021/example",
            title="ACS article",
            authors=["A Author"],
            abstract="Abstract text",
            date="2026",
        )
        monkeypatch.setattr(
            report,
            "query_journal_issue_full_result",
            lambda *args, **kwargs: AdapterResult(
                entries={entry.doi: entry},
                outcome=SUCCESS,
                provider="acs",
                source="journal",
            ),
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

    def test_computer_science_report_uses_valid_dataview_tag(self):
        result = _gen_oneday_markdown(
            "2026-07-07",
            "cs.AI",
            {
                "arXiv:2607.00001": ArxivEntry(
                    arxiv_id="arXiv:2607.00001",
                    title="AI paper",
                    authors=["A Author"],
                    abstract="Abstract",
                )
            },
            None,
        )

        assert "papertrack_category: cs.AI" in result
        assert "tags:\n  - cs-ai-2026-07-07" in result
        assert "from #cs-ai-2026-07-07" in result
        assert "WHERE completed" in result
        assert "#cs.AI-2026-07-07" not in result

    def test_hyphenated_arxiv_category_tag_remains_stable(self):
        result = _gen_oneday_markdown(
            "2026-07-07",
            "quant-ph",
            {
                "arXiv:2607.00001": ArxivEntry(
                    arxiv_id="arXiv:2607.00001",
                    title="Quantum paper",
                    authors=["A Author"],
                    abstract="Abstract",
                )
            },
            None,
        )

        assert "tags:\n  - quant-ph-2026-07-07" in result
        assert "from #quant-ph-2026-07-07" in result
        assert "  - #quant-ph-2026-07-07" not in result

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

    def test_extracts_search_hit_subject_tags(self):
        html = """<li class="arxiv-result">
 <p class="list-title is-inline-block"><a href="https://arxiv.org/abs/2606.01694">arXiv:2606.01694</a></p>
 <p class="title is-5 mathjax">Test</p>
 <p class="authors"><a href="...">Author</a></p>
 <p class="abstract mathjax"><span class="abstract-full">Abs.</span></p>
 <span class="tag is-small is-link tooltip is-tooltip-top">cs.CV</span>
 <span class="tag is-small search-hit tooltip is-tooltip-top">cs.AI</span>
 <span class="tag is-small is-grey tooltip is-tooltip-top">cs.LG</span>
</li>"""

        soup = BeautifulSoup(html, "lxml")
        entry = _parse_arxiv_result(soup.find("li"))

        assert entry is not None
        assert entry.subjects == ["cs.CV", "cs.AI", "cs.LG"]

    def test_strips_escaped_publisher_markup_from_title_and_abstract(self):
        html = """<li class="arxiv-result">
 <p class="list-title"><a>arXiv:2602.14927</a></p>
 <p class="title">H&lt;sub&gt;2&lt;/sub&gt; with &lt;i&gt;markup&lt;/i&gt;</p>
 <p class="authors"><a>A Author</a></p>
 <span class="abstract-full">Use H&lt;sub&gt;2&lt;/sub&gt; and &lt;a href="https://example.test"&gt;a citation&lt;/a&gt;.</span>
</li>"""

        entry = _parse_arxiv_result(BeautifulSoup(html, "lxml").find("li"))

        assert entry is not None
        assert entry.title == "H2 with markup"
        assert entry.abstract == "Use H2 and a citation."

    @pytest.mark.parametrize(
        "text",
        [
            "$x <a > y$",
            "overlap <a|b> remains",
            "comparison <i <= j> remains",
        ],
    )
    def test_preserves_angle_bracket_math_that_is_not_html(self, text):
        html = f"""<li class="arxiv-result">
 <p class="list-title"><a>arXiv:2602.14927</a></p>
 <p class="title">{text.replace('<', '&lt;').replace('>', '&gt;')}</p>
 <p class="authors"><a>A Author</a></p>
 <span class="abstract-full">{text.replace('<', '&lt;').replace('>', '&gt;')}</span>
</li>"""

        entry = _parse_arxiv_result(BeautifulSoup(html, "lxml").find("li"))

        assert entry is not None
        assert entry.title == text
        assert entry.abstract == text

    @pytest.mark.parametrize(
        "missing_html",
        [
            '<p class="title">Title</p><p class="authors"><a>A</a></p>',
            '<p class="title">Title</p><span class="abstract-full">Abstract</span>',
            '<p class="authors"><a>A</a></p><span class="abstract-full">Abstract</span>',
        ],
    )
    def test_rejects_entry_missing_required_report_metadata(self, missing_html):
        html = f"""<li class="arxiv-result">
 <p class="list-title"><a>arXiv:2606.00001</a></p>
 {missing_html}
</li>"""

        entry = _parse_arxiv_result(BeautifulSoup(html, "lxml").find("li"))

        assert entry is None

    def test_rejects_empty_identifier_link_and_empty_author_text(self):
        no_identifier = BeautifulSoup(
            """<li class="arxiv-result">
<p class="list-title"></p>
<p class="title">Title</p>
<p class="authors"><a>A</a></p>
<span class="abstract-full">Abstract</span>
</li>""",
            "lxml",
        ).find("li")
        empty_author = BeautifulSoup(
            """<li class="arxiv-result">
<p class="list-title"><a>arXiv:2606.00001</a></p>
<p class="title">Title</p>
<p class="authors"><a> </a></p>
<span class="abstract-full">Abstract</span>
</li>""",
            "lxml",
        ).find("li")

        assert _parse_arxiv_result(no_identifier) is None
        assert _parse_arxiv_result(empty_author) is None


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

        assert zq.index_available is False
        e = ArxivEntry(arxiv_id="arXiv:2502.07673", title="T", authors=[], abstract="")
        assert zq.find_by_entry(e) is False
        assert zq.match_entry(e).is_collected is False


from datetime import date


def test_filter_arxiv_days_to_md_processes_exact_days(tmp_path, monkeypatch):
    from papertrack import report
    from papertrack.adapter_result import AdapterResult, SUCCESS
    from papertrack.arxiv_entry import ArxivEntry

    calls = []

    def fake_query(date_from_date, date_to_date, query_args, required_category=""):
        calls.append((date_from_date, date_to_date, required_category))
        return AdapterResult(entries={
            "arXiv:2606.00001": ArxivEntry(
                arxiv_id="arXiv:2606.00001",
                title=f"Paper for {date_from_date}",
                authors=["A Author"],
                abstract="Abstract text",
            )
        }, outcome=SUCCESS, provider="arxiv", source="arxiv")

    monkeypatch.setattr(report, "query_arxiv_result", fake_query)

    report.filter_arxiv_days_to_md(
        days=[date(2026, 6, 8), date(2026, 6, 9)],
        md_folder=str(tmp_path / "arxiv" / "chem-ph"),
        query_args={"size": "200"},
        category="chem-ph",
        zotero=None,
    )

    assert calls == [
        ("2026-06-08", "2026-06-09", "physics.chem-ph"),
        ("2026-06-09", "2026-06-10", "physics.chem-ph"),
    ]
    assert (tmp_path / "arxiv" / "chem-ph" / "2026" / "06" / "08.md").exists()
    assert (tmp_path / "arxiv" / "chem-ph" / "2026" / "06" / "09.md").exists()


def test_filter_arxiv_days_to_md_skips_existing_written_day(tmp_path, monkeypatch):
    from papertrack import report
    from papertrack.adapter_result import AdapterResult, SUCCESS
    from papertrack.arxiv_entry import ArxivEntry

    existing = tmp_path / "arxiv" / "quant-ph" / "2026" / "06" / "08.md"
    existing.parent.mkdir(parents=True)
    existing.write_text(
        """---
papertrack_format: arxiv-v2
papertrack_source: arxiv
---

```dataview
TASK
from #quant-ph-2026-06-08
```

## collected

### arXiv:2606.00000

Title:  Existing paper

## not collected
""",
        encoding="utf-8",
    )

    calls = []

    def fake_query(date_from_date, date_to_date, query_args, required_category=""):
        calls.append((date_from_date, date_to_date, required_category))
        return AdapterResult(entries={
            "arXiv:2606.00001": ArxivEntry(
                arxiv_id="arXiv:2606.00001",
                title=f"Paper for {date_from_date}",
                authors=["A Author"],
                abstract="Abstract text",
            )
        }, outcome=SUCCESS, provider="arxiv", source="arxiv")

    monkeypatch.setattr(report, "query_arxiv_result", fake_query)

    report.filter_arxiv_days_to_md(
        days=[date(2026, 6, 8), date(2026, 6, 9)],
        md_folder=str(tmp_path / "arxiv" / "quant-ph"),
        query_args={"size": "200"},
        category="quant-ph",
        zotero=None,
    )

    assert calls == [("2026-06-09", "2026-06-10", "quant-ph")]
    assert (tmp_path / "arxiv" / "quant-ph" / "2026" / "06" / "09.md").exists()


def test_filter_arxiv_days_to_md_refreshes_existing_day_and_preserves_task_state(
    tmp_path, monkeypatch
):
    from papertrack import report
    from papertrack.adapter_result import AdapterResult, SUCCESS
    from papertrack.arxiv_entry import ArxivEntry

    existing = tmp_path / "arxiv" / "quant-ph" / "2026" / "06" / "08.md"
    existing.parent.mkdir(parents=True)
    existing.write_text(
        """---
papertrack_format: arxiv-v2
papertrack_source: arxiv
papertrack_category: quant-ph
papertrack_date: "2026-06-08"
tags:
  - #quant-ph-2026-06-08
---

```dataview
TASK
from #quant-ph-2026-06-08
```

## collected

## not collected

### arXiv:2606.00001

Links:

- [x] [arXiv:2606.00001](https://arxiv.org/abs/2606.00001) ✅ 2026-06-09

Title:  Stale title

Authors:  A Author,

Abstract:
> [!quote]- Abstract
> Stale abstract
""",
        encoding="utf-8",
    )

    calls = []

    def fake_query(date_from_date, date_to_date, query_args, required_category=""):
        calls.append((date_from_date, date_to_date, required_category))
        return AdapterResult(
            entries={
                "arXiv:2606.00001": ArxivEntry(
                    arxiv_id="arXiv:2606.00001",
                    title="Fresh title",
                    authors=["A Author"],
                    abstract="Fresh abstract",
                )
            },
            outcome=SUCCESS,
            provider="arxiv",
            source="arxiv",
        )

    monkeypatch.setattr(report, "query_arxiv_result", fake_query)

    report.filter_arxiv_days_to_md(
        days=[date(2026, 6, 8)],
        md_folder=str(tmp_path / "arxiv" / "quant-ph"),
        query_args={"size": "200"},
        category="quant-ph",
        zotero=None,
        refresh_existing=True,
    )

    refreshed = existing.read_text(encoding="utf-8")
    assert calls == [("2026-06-08", "2026-06-09", "quant-ph")]
    assert "Title:  Fresh title" in refreshed
    assert "Fresh abstract" in refreshed
    assert "- [x] [arXiv:2606.00001]" in refreshed
    assert "✅ 2026-06-09" in refreshed


def test_refresh_fetch_failure_preserves_existing_report_byte_for_byte(
    tmp_path, monkeypatch
):
    from papertrack import report
    from papertrack.adapter_result import AdapterResult, FETCH_FAILED

    existing = tmp_path / "arxiv" / "quant-ph" / "2026" / "06" / "08.md"
    existing.parent.mkdir(parents=True)
    original = """---
papertrack_format: arxiv-v2
papertrack_source: arxiv
---

```dataview
TASK
from #quant-ph-2026-06-08
```

## collected

### arXiv:2606.00001

## not collected
"""
    existing.write_text(original, encoding="utf-8")
    monkeypatch.setattr(
        report,
        "query_arxiv_result",
        lambda *args, **kwargs: AdapterResult(
            entries={},
            outcome=FETCH_FAILED,
            provider="arxiv",
            source="arxiv",
            message="network down",
        ),
    )

    report.filter_arxiv_days_to_md(
        days=[date(2026, 6, 8)],
        md_folder=str(tmp_path / "arxiv" / "quant-ph"),
        category="quant-ph",
        refresh_existing=True,
    )

    assert existing.read_bytes() == original.encode()


def test_refresh_does_not_replace_report_when_official_result_loses_old_paper(
    tmp_path, monkeypatch
):
    from papertrack import report
    from papertrack.adapter_result import AdapterResult, SUCCESS
    from papertrack.arxiv_entry import ArxivEntry

    existing = tmp_path / "arxiv" / "quant-ph" / "2026" / "06" / "08.md"
    existing.parent.mkdir(parents=True)
    original = """---
papertrack_format: arxiv-v2
papertrack_source: arxiv
---

```dataview
TASK
```

## collected

## not collected

### arXiv:2606.00001

- [x] [arXiv:2606.00001](https://arxiv.org/abs/2606.00001) ✅ 2026-06-09

### arXiv:2606.00002

- [ ] [arXiv:2606.00002](https://arxiv.org/abs/2606.00002)
"""
    existing.write_text(original, encoding="utf-8")
    monkeypatch.setattr(
        report,
        "query_arxiv_result",
        lambda *args, **kwargs: AdapterResult(
            entries={
                "arXiv:2606.00002": ArxivEntry(
                    arxiv_id="arXiv:2606.00002",
                    title="Still present",
                    authors=["A Author"],
                    abstract="Abstract",
                )
            },
            outcome=SUCCESS,
            provider="arxiv",
            source="arxiv",
        ),
    )

    report.filter_arxiv_days_to_md(
        days=[date(2026, 6, 8)],
        md_folder=str(tmp_path / "arxiv" / "quant-ph"),
        category="quant-ph",
        refresh_existing=True,
    )

    assert existing.read_text(encoding="utf-8") == original


def test_refresh_preserves_collected_section_when_zotero_is_unavailable(
    tmp_path, monkeypatch
):
    from papertrack import report
    from papertrack.adapter_result import AdapterResult, SUCCESS
    from papertrack.arxiv_entry import ArxivEntry

    existing = tmp_path / "arxiv" / "quant-ph" / "2026" / "06" / "08.md"
    existing.parent.mkdir(parents=True)
    existing.write_text(
        """---
papertrack_format: arxiv-v2
papertrack_source: arxiv
---

```dataview
TASK
```

## collected

### arXiv:2606.00001

- [ ] [arXiv:2606.00001](https://arxiv.org/abs/2606.00001)

Title:  Old title

## not collected
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        report,
        "query_arxiv_result",
        lambda *args, **kwargs: AdapterResult(
            entries={
                "arXiv:2606.00001": ArxivEntry(
                    arxiv_id="arXiv:2606.00001",
                    title="Fresh title",
                    authors=["A Author"],
                    abstract="Fresh abstract",
                )
            },
            outcome=SUCCESS,
            provider="arxiv",
            source="arxiv",
        ),
    )

    report.filter_arxiv_days_to_md(
        days=[date(2026, 6, 8)],
        md_folder=str(tmp_path / "arxiv" / "quant-ph"),
        category="quant-ph",
        zotero=None,
        refresh_existing=True,
    )

    refreshed = existing.read_text(encoding="utf-8")
    collected = refreshed.split("## collected", 1)[1].split("## not collected", 1)[0]
    assert "### arXiv:2606.00001" in collected
    assert "Title:  Fresh title" in collected


def test_refresh_preserves_collected_when_zotero_index_fetch_failed(
    tmp_path, monkeypatch
):
    from papertrack import report
    from papertrack.adapter_result import AdapterResult, SUCCESS
    from papertrack.arxiv_entry import ArxivEntry
    from papertrack.identity import MatchResult

    class UnavailableZotero:
        index_available = False

        def match_entry(self, entry):
            return MatchResult()

    existing = tmp_path / "arxiv" / "quant-ph" / "2026" / "06" / "08.md"
    existing.parent.mkdir(parents=True)
    existing.write_text(
        """---
papertrack_format: arxiv-v2
papertrack_source: arxiv
---

```dataview
TASK
```

## collected

### arXiv:2606.00001

- [ ] [arXiv:2606.00001](https://arxiv.org/abs/2606.00001)

Title:  Old title

## not collected
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        report,
        "query_arxiv_result",
        lambda *args, **kwargs: AdapterResult(
            entries={
                "arXiv:2606.00001": ArxivEntry(
                    arxiv_id="arXiv:2606.00001",
                    title="Fresh title",
                    authors=["A Author"],
                    abstract="Fresh abstract",
                )
            },
            outcome=SUCCESS,
            provider="arxiv",
            source="arxiv",
        ),
    )

    report.filter_arxiv_days_to_md(
        days=[date(2026, 6, 8)],
        md_folder=str(tmp_path / "arxiv" / "quant-ph"),
        category="quant-ph",
        zotero=UnavailableZotero(),
        refresh_existing=True,
    )

    refreshed = existing.read_text(encoding="utf-8")
    collected = refreshed.split("## collected", 1)[1].split("## not collected", 1)[0]
    assert "### arXiv:2606.00001" in collected


def test_parse_arxiv_page_rejects_partial_entry_parse():
    results = {
        "feed": {
            "summary": """
<li class="arxiv-result">
  <p class="list-title"><a>arXiv:2606.00001</a></p>
  <p class="title">Good paper</p>
</li>
<li class="arxiv-result">
  <p class="title">Missing identifier</p>
</li>
"""
        }
    }

    assert _parse_arxiv_page(results, "https://arxiv.test/search") is None


def test_parse_arxiv_page_records_the_missing_metadata_field(monkeypatch):
    from papertrack import arxiv_index_fetch

    attempts = []
    monkeypatch.setattr(
        arxiv_index_fetch,
        "record_fetch_attempt",
        lambda payload: attempts.append(payload),
    )
    results = {
        "feed": {
            "summary": """
<li class="arxiv-result">
  <p class="list-title"><a>arXiv:2606.00001</a></p>
  <p class="title">Paper</p>
  <p class="authors"><a>A Author</a></p>
</li>
"""
        }
    }

    assert arxiv_index_fetch._parse_arxiv_page(results, "https://arxiv.test") is None
    assert attempts[-1]["field"] == "abstract"


def test_filter_arxiv_days_to_md_skips_empty_days(tmp_path, monkeypatch):
    from papertrack import report
    from papertrack.adapter_result import AdapterResult, SUCCESS

    monkeypatch.setattr(
        report,
        "query_arxiv_result",
        lambda *args, **kwargs: AdapterResult(
            entries={},
            outcome=SUCCESS,
            provider="arxiv",
            source="arxiv",
        ),
    )

    report.filter_arxiv_days_to_md(
        days=[date(2026, 6, 9)],
        md_folder=str(tmp_path / "arxiv" / "quant-ph"),
        query_args={"size": "200"},
        category="quant-ph",
        zotero=None,
    )

    assert not (tmp_path / "arxiv" / "quant-ph" / "2026" / "06" / "09.md").exists()


def test_filter_arxiv_days_to_md_stops_on_rate_limit(tmp_path, monkeypatch):
    import pytest
    from papertrack import report
    from papertrack.adapter_result import AdapterResult, RATE_LIMITED

    calls = []

    def fake_query(date_from_date, date_to_date, query_args, required_category=""):
        calls.append(date_from_date)
        return AdapterResult(
            entries={},
            outcome=RATE_LIMITED,
            provider="arxiv",
            source="arxiv",
            message="HTTP 429",
        )

    monkeypatch.setattr(report, "query_arxiv_result", fake_query)

    with pytest.raises(RuntimeError, match="arXiv rate limited"):
        report.filter_arxiv_days_to_md(
            days=[date(2026, 6, 8), date(2026, 6, 9)],
            md_folder=str(tmp_path / "arxiv" / "quant-ph"),
            query_args={"size": "200"},
            category="quant-ph",
            zotero=None,
        )

    assert calls == ["2026-06-08"]

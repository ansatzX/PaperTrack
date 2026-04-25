"""Unit tests for arxiv_entry, report, and arxiv_index_fetch modules"""
import pytest
from bs4 import BeautifulSoup

from ArXiv_Tools.arxiv_entry import ArxivEntry
from ArXiv_Tools.arxiv_index_fetch import _parse_arxiv_result
from ArXiv_Tools.report import (
    parse_old_report,
    _gen_oneday_markdown,
)
from ArXiv_Tools.zotero_query import ZoteroQuery


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

    def test_find_by_entry_three_layer_match(self, monkeypatch):
        class FakeZotero:
            def everything(self, items):
                return items

            def top(self):
                return [
                    {"key": "A1", "data": {"DOI": "10.48550/arXiv.2502.07673", "url": ""}},
                    {"key": "B2", "data": {"DOI": "10.1103/PhysRevLett.134.010201", "url": ""}},
                    {"key": "C3", "data": {"DOI": "", "url": "https://arxiv.org/abs/2502.99999"}},
                ]

        zq = ZoteroQuery()
        zq.zot = FakeZotero()
        zq._ensure_index()

        # Layer 1: arXiv DOI match
        e1 = ArxivEntry(arxiv_id="arXiv:2502.07673", title="T", authors=[], abstract="")
        assert zq.find_by_entry(e1) is True

        # Layer 2: external DOI match
        e2 = ArxivEntry(arxiv_id="arXiv:9999.99999", title="T", authors=[], abstract="",
                        external_doi="10.1103/PhysRevLett.134.010201")
        assert zq.find_by_entry(e2) is True

        # Layer 3: arXiv ID from URL match (webpage-type item without DOI)
        e3 = ArxivEntry(arxiv_id="arXiv:2502.99999", title="T", authors=[], abstract="")
        assert zq.find_by_entry(e3) is True

        # No match
        e4 = ArxivEntry(arxiv_id="arXiv:8888.88888", title="T", authors=[], abstract="")
        assert zq.find_by_entry(e4) is False

    def test_index_not_built_when_zotero_unavailable(self, monkeypatch):
        class BrokenZotero:
            def top(self):
                raise ConnectionError("Zotero not running")

        zq = ZoteroQuery()
        zq.zot = BrokenZotero()
        zq._ensure_index()

        e = ArxivEntry(arxiv_id="arXiv:2502.07673", title="T", authors=[], abstract="")
        assert zq.find_by_entry(e) is False

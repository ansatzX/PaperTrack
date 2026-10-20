from bs4 import BeautifulSoup

from papertrack.reports.markdown import repair_presentation
from papertrack.reports.reading import reading_markdown
from papertrack.reports.render import _render_journal_paper, _render_paper
from papertrack.journals.entry import JournalEntry
from papertrack.arxiv.entry import ArxivEntry


def test_dom_math_has_delimiters_and_tex_structure():
    source = BeautifulSoup('<p>Mass <mjx-container><mjx-math><mjx-msup>'
                           '<mjx-mrow>10</mjx-mrow><mjx-script>13.9</mjx-script>'
                           '</mjx-msup><mjx-mo>&lt;</mjx-mo><mjx-mi>𝑀</mjx-mi>'
                           '</mjx-math></mjx-container>; H<sub>2</sub>O.</p>', 'html.parser')
    assert reading_markdown(source) == 'Mass $10^{13.9}<M$; H$_{2}$O.'


def test_callout_remains_contiguous_and_user_tasks_survive():
    old = ('- [x] [paper](url) ✅ 2026-01-01\n\nAuthors: A\n\n\n\nAbstract:\n'
           '> [!quote]- Abstract\n\n> A &lt; B and $x^2$.\n> △ Less\n\n\n\n'
           '<!-- papertrack_provenance: {} -->\n\nMy note.\n')
    repaired = repair_presentation(old)
    assert '> [!quote]- Abstract\n> A < B and $x^2$.' in repaired
    assert '△ Less' not in repaired and '\nAbstract:' not in repaired
    assert 'Authors: A\n\n> [!quote]' in repaired
    assert 'papertrack_provenance' not in repaired
    assert '- [x] [paper](url) ✅ 2026-01-01' in repaired
    assert 'My note.' in repaired
    assert repair_presentation(repaired) == repaired


def test_journal_template_not_html_escaped_or_split():
    entry = JournalEntry(doi='10.1234/x', title='Title', authors=['A'],
                         abstract='Mass $10^{13.9}<M$.\n\nNext paragraph.')
    entry.field_sources['abstract'] = 'official_browser'
    output = _render_journal_paper(entry)
    assert '> [!quote]- Abstract\n> Mass $10^{13.9}<M$.\n> \n> Next paragraph.' in output
    assert '\nAbstract:' not in output and '&lt;' not in output
    assert '\n\n\n' not in output
    assert 'papertrack_provenance' not in output


def test_arxiv_source_removes_only_collapse_control():
    from papertrack.arxiv.fetch import _parse_arxiv_result
    source = BeautifulSoup('''<li><p class="list-title"><a>arXiv:2601.00001</a></p>
    <p class="title">Title</p><p class="authors"><a>A</a></p>
    <span class="abstract-full">Less is more: $x&lt;y$.
    <a class="is-size-7">△ Less</a></span></li>''', 'html.parser')
    entry = _parse_arxiv_result(source.li)
    assert entry.abstract == 'Less is more: $x<y$.'


def test_multiline_authors_do_not_leave_large_callout_gap():
    before = 'Authors: A\nn\n~\nez\n\n\n\n> [!quote]- Abstract\n> Text\n'
    after = repair_presentation(before)
    assert 'ez\n\n> [!quote]' in after
    assert repair_presentation(after) == after


def test_arxiv_template_preserves_tex_and_has_one_continuous_callout():
    entry = ArxivEntry('arXiv:2601.00001', 'Title', ['A'],
                       'For $x<y$, $10^{13.9}$ grows.\n\nSecond paragraph.\n △ Less')
    output = _render_paper(entry)
    assert '> [!quote]- Abstract\n> For $x<y$, $10^{13.9}$ grows.\n> \n> Second paragraph.' in output
    assert output.count('Abstract') == 1
    assert 'Less' not in output and '&lt;' not in output


def test_provenance_cleanup_preserves_reading_state_notes_and_cached_evidence(tmp_path):
    from papertrack.reports.migration import remove_report_provenance

    root = tmp_path / 'data'
    report = root / 'aps/prl/135/25.md'
    report.parent.mkdir(parents=True)
    before = ('---\r\npapertrack_format: journal-v2\r\n---\r\n'
              '### 10.1234/CaseSensitive\r\n\r\n'
              '- [x] [paper](https://doi.org/10.1234/CaseSensitive) ✅ 2026-01-01\r\n'
              '> [!quote]- Extract（原文摘录）\r\n> For $x<y$.\r\n\r\n'
              '%% papertrack_provenance: {"fields": {}} %%\r\n\r\n'
              'My note includes `papertrack_provenance` and %% my comment %%.\r\n')
    report.write_bytes(before.encode())
    cached = root / '.papertrack-runs/source.md'
    cached.parent.mkdir()
    cached.write_bytes(before.encode())
    note = root / 'personal.md'
    note.write_text('<!-- papertrack_provenance: {} -->\nMy personal note.\n')
    backup = tmp_path / 'backups'

    results = remove_report_provenance(str(root), str(backup))

    assert len(results) == 1 and results[0].status == 'repaired'
    assert (backup / report.relative_to(root)).read_bytes() == before.encode()
    assert report.read_bytes() == before.replace(
        '%% papertrack_provenance: {"fields": {}} %%\r\n\r\n', '').encode()
    assert cached.read_bytes() == before.encode()
    assert note.read_text().startswith('<!-- papertrack_provenance:')
    assert remove_report_provenance(str(root), str(backup)) == []


def test_provenance_cleanup_refuses_conflicting_backup(tmp_path):
    from papertrack.reports.migration import remove_report_provenance

    root = tmp_path / 'data'
    root.mkdir()
    report = root / 'report.md'
    before = '---\npapertrack_format: journal-v2\n---\n<!-- papertrack_provenance: {} -->\n'
    report.write_text(before)
    backup = tmp_path / 'backups'
    backup.mkdir()
    (backup / 'report.md').write_text('Existing different backup')
    results = remove_report_provenance(str(root), str(backup))
    assert results[0].status == 'failed'
    assert report.read_text() == before

import json
from types import SimpleNamespace

import pytest

from papertrack.journals.entry import JournalEntry
from papertrack.reports.presentation import repair_journal_presentation


def setup_source(tmp_path, monkeypatch, raw_title, old_title, new_title):
    from papertrack.reports import presentation as presentation_migration
    from papertrack.journals import capture as browser_issue
    capture = tmp_path / 'capture.json'
    capture.write_text(json.dumps({'entries': [{'doi': 'https://doi.org/10.1063/example', 'title': raw_title}]}))
    report = tmp_path / '15.md'
    monkeypatch.setattr(presentation_migration, '_captures', lambda _: {
        str(report.resolve()): ({'journal': 'jcp', 'provider': 'aip', 'volume': '165', 'issue': '15'}, capture)})
    calls = []
    def validate(*args, **kwargs):
        calls.append('validated')
    def read(*args, render_math=True, **kwargs):
        assert calls == ['validated']
        return SimpleNamespace(entries={'10.1063/example': JournalEntry(
            '10.1063/example', new_title if render_math else old_title, [], 'Source abstract')})
    monkeypatch.setattr(browser_issue, 'validate_reading_controls', validate)
    monkeypatch.setattr(browser_issue, 'read_browser_issue', read)
    return report


@pytest.mark.parametrize('actual', ['CO2\nSpectroscopy', 'CO_{2}\nSpectroscopy'])
def test_only_exact_old_multiline_title_changes(tmp_path, monkeypatch, actual):
    report = setup_source(tmp_path, monkeypatch, 'CO2\nSpectroscopy', 'CO_{2}\nSpectroscopy', 'CO$_{2}$\nSpectroscopy')
    content = ('### 10.1063/example\n\n- [x] [10.1063/example](https://doi.org/10.1063/example) my task note\n\n'
               f'Title:  {actual}\n\nAuthors: A\n\nMy separate note.\n\n'
               '> [!quote]- Abstract\n> User edited abstract.\n')
    output = repair_journal_presentation(content, report, str(tmp_path))
    assert output == content.replace(f'Title:  {actual}', 'Title:  CO$_{2}$\nSpectroscopy')


def test_user_changed_title_and_notes_stay_exact(tmp_path, monkeypatch):
    report = setup_source(tmp_path, monkeypatch, 'CO2', 'CO_{2}', 'CO$_{2}$')
    content = '### 10.1063/example\n\nTitle:  My CO2 title\n\nAuthors: A\n\nMy note.\n'
    assert repair_journal_presentation(content, report, str(tmp_path)) == content


def test_failed_source_validation_prevents_title_migration(tmp_path, monkeypatch):
    from papertrack.journals import capture as browser_issue
    report = setup_source(tmp_path, monkeypatch, 'CO2', 'CO_{2}', 'CO$_{2}$')
    content = '### 10.1063/example\n\nTitle:  CO2\n\nAuthors: A\n'
    report.write_text(content)
    def fail(*args, **kwargs):
        raise ValueError('source mismatch')
    monkeypatch.setattr(browser_issue, 'validate_reading_controls', fail)
    with pytest.raises(ValueError, match='source mismatch'):
        repair_journal_presentation(content, report, str(tmp_path))
    assert report.read_text() == content

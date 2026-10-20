from copy import deepcopy

import pytest

from papertrack.journals.entry import JournalEntry
from papertrack.reports.render import write_journal_entries_to_md


@pytest.mark.parametrize('confirmed,text,title,allowed', [
    (True, 'This corrects the article DOI: 10.1103/original.', 'Erratum: A correction', True),
    (False, 'This corrects the article DOI: 10.1103/original.', 'Erratum: A correction', False),
    (True, 'A substantive original abstract.', 'Erratum: A correction', False),
    (True, 'This corrects the article DOI: 10.1103/original.', 'Ordinary article', False),
])
def test_only_verified_erratum_teaser_can_be_repaired_with_backup(tmp_path, confirmed, text, title, allowed):
    doi = '10.1103/example'
    entry = JournalEntry(doi=doi, title=title, authors=['Author'], abstract=text,
                         journal_name='Physical Review Letters', volume='137', issue='1', date='2026')
    args = ('Physical Review Letters', 'prl', '137', '1')
    assert write_journal_entries_to_md(*args, {doi: entry}, str(tmp_path), provider='aps')
    report = tmp_path / 'prl/137/1.md'
    report.write_text(report.read_text().replace('- [ ]', '- [x]'))
    original = report.read_bytes()
    updated = deepcopy(entry)
    updated.abstract = ''
    kwargs = dict(provider='aps', verified_absent_dois={doi} if confirmed else set(),
                  repair_backup_dir=str(tmp_path / 'evidence/report-backups'))
    if allowed:
        assert write_journal_entries_to_md(*args, {doi: updated}, str(tmp_path), **kwargs)
        assert text not in report.read_text()
        assert f'### {doi}' in report.read_text() and '- [x]' in report.read_text()
        backups = list((tmp_path / 'evidence/report-backups').glob('*.md'))
        assert len(backups) == 1 and backups[0].read_bytes() == original
    else:
        with pytest.raises(ValueError, match='prior abstract'):
            write_journal_entries_to_md(*args, {doi: updated}, str(tmp_path), **kwargs)
        assert report.read_bytes() == original

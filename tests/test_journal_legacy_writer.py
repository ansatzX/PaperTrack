from copy import deepcopy

import pytest

from papertrack.journals.entry import JournalEntry
from papertrack.reports.layout import migrate_journal_layout
from papertrack.reports.render import write_journal_entries_to_md


def setup_pair(tmp_path):
    entries = {'10.1021/test': JournalEntry('10.1021/test', 'Old', ['Author'], 'Old abstract', date='2026')}
    def write(data=None):
        return write_journal_entries_to_md('Journal', 'jctc', '21', '24', data or entries, str(tmp_path / 'acs'))
    write()
    canonical = tmp_path / 'acs/jctc/21/24.md'
    legacy = tmp_path / 'acs/jctc/21/24/report.md'
    legacy.parent.mkdir()
    legacy.write_bytes(canonical.read_bytes())
    return entries, write, canonical, legacy


def test_writer_requires_migration_and_does_not_update_two_live_copies(tmp_path):
    entries, write, canonical, legacy = setup_pair(tmp_path)
    original = [p.read_bytes() for p in (canonical, legacy)]
    entries['10.1021/test'].abstract = 'New abstract'
    with pytest.raises(ValueError, match='--journal-layout-only'):
        write()
    assert [p.read_bytes() for p in (canonical, legacy)] == original


def test_merge_completion_then_only_write_canonical(tmp_path):
    entries, write, canonical, legacy = setup_pair(tmp_path)
    note = ' my reading note'
    canonical.write_text(canonical.read_text().replace('(https://doi.org/10.1021/test)', '(https://doi.org/10.1021/test)' + note))
    legacy.write_text(canonical.read_text().replace('- [ ]', '- [x]') + '\n')
    legacy.write_text(legacy.read_text().replace(note, note + ' ✅ 2026-10-18'))
    old, current = legacy.read_bytes(), canonical.read_bytes()
    backups = tmp_path.parent / (tmp_path.name + '-backups')
    result = migrate_journal_layout(str(tmp_path), str(backups))
    assert [r.status for r in result] == ['migrated']
    assert (backups / 'acs/jctc/21/24/report.md').read_bytes() == old
    assert (backups / 'acs/jctc/21/24.md').read_bytes() == current
    assert not legacy.parent.exists()
    assert '- [x]' in canonical.read_text() and note + ' ✅ 2026-10-18' in canonical.read_text()
    changed = deepcopy(entries)
    changed['10.1021/test'].abstract = 'New abstract'
    write(changed)
    assert 'New abstract' in canonical.read_text()
    assert '- [x]' in canonical.read_text() and note in canonical.read_text()
    assert not legacy.exists()
    assert migrate_journal_layout(str(tmp_path), str(backups)) == []


@pytest.mark.parametrize('extra', ['\nMy independent note\n', '\n> My quote\n', '\n![my image](notes.png)\n'])
def test_layout_preserves_conflicting_user_text_without_choosing_a_copy(tmp_path, extra):
    _, _, canonical, legacy = setup_pair(tmp_path)
    legacy.write_text(legacy.read_text() + extra)
    before = [p.read_bytes() for p in (canonical, legacy)]
    result = migrate_journal_layout(str(tmp_path), str(tmp_path.parent / (tmp_path.name + '-backup')))
    assert result[0].status == 'failed' and 'Conflicting' in result[0].message
    assert [p.read_bytes() for p in (canonical, legacy)] == before


def test_only_generated_toc_and_spacing_may_differ(tmp_path):
    _, _, canonical, legacy = setup_pair(tmp_path)
    before = canonical.read_bytes()
    legacy.write_text(legacy.read_text().replace('Authors:', '\n![|700](https://pubs.acs.org/cms/10.1021/test/asset/images/medium/t.gif)\n\nAuthors:'))
    result = migrate_journal_layout(str(tmp_path), str(tmp_path.parent / (tmp_path.name + '-backup')))
    assert result[0].status == 'migrated'
    assert canonical.read_bytes() == before
    assert not legacy.exists()


def test_lone_legacy_moves_verbatim_with_notes_anchors_and_extra_assets(tmp_path):
    _, _, canonical, legacy = setup_pair(tmp_path)
    canonical.unlink()
    original = legacy.read_bytes() + b'\nMy private note\n'
    legacy.write_bytes(original)
    asset = legacy.parent / 'my-figure.svg'
    asset.write_text('<svg/>')
    result = migrate_journal_layout(str(tmp_path), str(tmp_path.parent / (tmp_path.name + '-backup')))
    assert result[0].status == 'migrated'
    assert canonical.read_bytes() == original
    assert not legacy.exists() and asset.read_text() == '<svg/>'


def test_conflicting_anchor_spelling_or_members_preserved(tmp_path):
    _, _, canonical, legacy = setup_pair(tmp_path)
    legacy.write_text(legacy.read_text().replace('### 10.1021/test', '### 10.1021/TEST'))
    before = [p.read_bytes() for p in (canonical, legacy)]
    result = migrate_journal_layout(str(tmp_path), str(tmp_path.parent / (tmp_path.name + '-backup')))
    assert result[0].status == 'failed'
    assert [p.read_bytes() for p in (canonical, legacy)] == before


def test_backup_conflict_does_not_remove_either_copy(tmp_path):
    _, _, canonical, legacy = setup_pair(tmp_path)
    backup = tmp_path.parent / (tmp_path.name + '-backup')
    target = backup / 'acs/jctc/21/24/report.md'
    target.parent.mkdir(parents=True)
    target.write_text('An earlier different backup')
    before = [p.read_bytes() for p in (canonical, legacy)]
    result = migrate_journal_layout(str(tmp_path), str(backup))
    assert result[0].status == 'failed'
    assert [p.read_bytes() for p in (canonical, legacy)] == before


def test_no_new_legacy_created(tmp_path):
    entries = {'10.1021/test': JournalEntry('10.1021/test', 'T', [], 'A')}
    write_journal_entries_to_md('Journal', 'jctc', '21', '24', entries, str(tmp_path))
    assert (tmp_path / 'jctc/21/24.md').is_file()
    assert not (tmp_path / 'jctc/21/24').exists()


@pytest.mark.parametrize('extra', ['\nMy independent note\n', '\n> My quote\n', '\n![my image](notes.png)\n'])
def test_canonical_writer_still_protects_user_notes(tmp_path, extra):
    _, write, canonical, legacy = setup_pair(tmp_path)
    legacy.unlink()
    canonical.write_text(canonical.read_text() + extra)
    before = canonical.read_bytes()
    with pytest.raises(ValueError, match='Unpreserved user text'):
        write()
    assert canonical.read_bytes() == before


def test_layout_backup_must_be_outside_data(tmp_path):
    setup_pair(tmp_path)
    with pytest.raises(ValueError, match='outside'):
        migrate_journal_layout(str(tmp_path), str(tmp_path / 'backup'))


def test_unchecked_legacy_cannot_uncheck_canonical(tmp_path):
    _, _, canonical, legacy = setup_pair(tmp_path)
    canonical.write_text(canonical.read_text().replace('- [ ]', '- [x]'))
    original = canonical.read_bytes()
    result = migrate_journal_layout(str(tmp_path), str(tmp_path.parent / (tmp_path.name + '-backup')))
    assert result[0].status == 'migrated'
    assert canonical.read_bytes() == original
    assert not legacy.exists()


def test_migration_does_not_follow_linked_journal_directory(tmp_path):
    outside = tmp_path.parent / (tmp_path.name + '-outside')
    report = outside / '21/24/report.md'
    report.parent.mkdir(parents=True)
    report.write_text('Do not move this linked report')
    (tmp_path / 'acs').mkdir()
    (tmp_path / 'acs/jctc').symlink_to(outside, target_is_directory=True)
    results = migrate_journal_layout(str(tmp_path), str(tmp_path.parent / (tmp_path.name + '-backup')))
    assert results[0].status == 'failed'
    assert report.read_text() == 'Do not move this linked report'
    assert not (outside / '21/24.md').exists()

"""Journal evidence lives with the runtime, not with Markdown exports."""
import json
from datetime import date, datetime

import pytest

from papertrack.journals.storage import move_legacy_cache, relocated_record, runs_root
from papertrack.journals.sync import _records, _archive_cache
from papertrack.reports.presentation import _captures


def test_moved_cache_preserves_bytes_and_resolves_all_record_paths(tmp_path):
    export = tmp_path / 'exports'
    source = export / '.papertrack-runs' / 'jcim-example'
    source.mkdir(parents=True)
    capture = source / 'custom.json'
    capture.write_text('{"raw":"unchanged"}')
    panel = source / 'custom.json.readings' / 'panel.html'
    panel.parent.mkdir()
    panel.write_bytes(b'<p>Exact source &amp; math</p>\r\n')
    report = export / 'acs/jcim/66/1.md'
    report.parent.mkdir(parents=True)
    report.write_text('- [x] user state\n')
    record = dict(journal='jcim', status='complete', updated_at='2026-10-21',
                  report_path=str(report), capture_path=str(capture),
                  artifacts={'reading': {'path': str(panel)}},
                  command=['--cd', str(source)])
    (source / 'result.json').write_text(json.dumps(record))
    before = {str(p.relative_to(source)): p.read_bytes()
              for p in source.rglob('*') if p.is_file()}

    destination = move_legacy_cache(export)
    assert destination == tmp_path / '.papertrack-runs'
    assert not (export / '.papertrack-runs').exists()
    assert move_legacy_cache(export) == destination  # Idempotent.
    moved = destination / source.name
    assert {str(p.relative_to(moved)): p.read_bytes()
            for p in moved.rglob('*') if p.is_file()} == before
    rows = _records(export, 'jcim')
    assert len(rows) == 1
    actual, folder = rows[0]
    assert folder == moved
    assert actual['capture_path'] == str(moved / 'custom.json')
    assert actual['artifacts']['reading']['path'] == str(moved / 'custom.json.readings/panel.html')
    assert actual['command'] == ['--cd', str(moved)]
    assert actual['report_path'] == str(report)
    # Relocation remains valid when exporting to a different directory later.
    assert relocated_record(record, tmp_path / 'other')['capture_path'] == actual['capture_path']
    _captures.cache_clear()
    assert _captures(str(export))[str(report)][1] == moved / 'custom.json'
    assert report.read_text() == '- [x] user state\n'


def test_runtime_and_legacy_cache_are_both_read_without_creating_export_cache(tmp_path):
    export = tmp_path / 'exports'
    for root, name in [(runs_root(), 'new'), (export / '.papertrack-runs', 'old')]:
        folder = root / name
        folder.mkdir(parents=True)
        (folder / 'result.json').write_text(json.dumps(dict(journal='jcim', updated_at=name)))
    assert {folder.name for _, folder in _records(export, 'jcim')} == {'old', 'new'}
    fresh_export = tmp_path / 'empty-export'
    assert len(_records(fresh_export, 'jcim')) == 1
    assert not fresh_export.exists()


def test_cache_move_never_merges_or_overwrites(tmp_path):
    export = tmp_path / 'exports'
    old = export / '.papertrack-runs'
    old.mkdir(parents=True)
    (old / 'evidence.html').write_text('source')
    runs_root().mkdir()
    with pytest.raises(FileExistsError, match='Refusing to merge'):
        move_legacy_cache(export)
    assert (old / 'evidence.html').read_text() == 'source'
    assert not (old / '.relocation.json').exists()


def test_moved_archive_cache_is_still_a_same_day_hit(tmp_path):
    export = tmp_path / 'exports'
    folder = export / '.papertrack-runs' / 'jcim-archive'
    folder.mkdir(parents=True)
    capture = folder / 'capture.json'
    url = 'https://pubs.acs.org/jcisd8/issue-covers'
    capture.write_text(json.dumps(dict(schema='papertrack-browser-archive-v1',
        requested_url=url, url=url, captured_at=datetime.now().astimezone().isoformat())))
    html = capture.with_name('capture.json.html')
    html.write_bytes(b'<p>Original official archive</p>')
    (folder / 'result.json').write_text(json.dumps(dict(journal='jcim',
        status='archive_captured', url=url, observed_url=url, capture_path=str(capture))))
    move_legacy_cache(export)
    hit = _archive_cache(_records(export, 'jcim'), url, date.today())
    assert hit == (runs_root() / folder.name / html.name, url)
    assert hit[0].read_bytes() == b'<p>Original official archive</p>'


def test_corrupt_relocation_fails_visibly_instead_of_losing_cache(tmp_path):
    folder = runs_root() / 'jcim-example'
    folder.mkdir(parents=True)
    (folder / 'result.json').write_text('{"journal":"jcim"}')
    (runs_root() / '.relocation.json').write_text('{bad json')
    with pytest.raises(RuntimeError, match='Cannot resolve relocated cache'):
        _records(tmp_path / 'exports', 'jcim')


def test_copied_run_uses_local_evidence_instead_of_earlier_temporary_cache(tmp_path):
    folder = runs_root() / 'jcim-old-copy'
    folder.mkdir(parents=True)
    capture = folder / 'capture.json'
    capture.write_text('local source')
    original = tmp_path / 'prior/.papertrack-runs' / folder.name / capture.name
    record = dict(journal='jcim', capture_path=str(original))
    (folder / 'result.json').write_text(json.dumps(record))
    actual, _ = _records(tmp_path / 'exports', 'jcim')[0]
    assert actual['capture_path'] == str(capture)
    # An explicitly external capture is not rebound to a different run.
    record['capture_path'] = str(original.parent.parent / 'other-run/capture.json')
    assert relocated_record(record, tmp_path / 'exports', folder) == record

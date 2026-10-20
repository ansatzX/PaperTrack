import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from papertrack.journals import cache as refresh
from papertrack import cli
from papertrack.config import journal_configs


DOI = '10.1021/example'
CFG = journal_configs['jctc']


def make_capture(root: Path, issue: str, *, abstract='First paragraph.', extra='Second x<sub>r</sub> value'):
    run_dir = root / '.papertrack-runs' / f'jctc-22-{issue}-source'
    run_dir.mkdir(parents=True)
    capture = run_dir / 'capture.json'
    url = f"https://pubs.acs.org/jctcce/issue/22/{issue}"
    row = dict(doi=DOI.replace('example', f'example{issue}'), title='A paper', authors=['A Author'],
               abstract=abstract, abstract_type='extract', control=f'control-{issue}', panel=f'panel-{issue}',
               citation=f'J. Chem. Theory Comput. (2026) 22 ({issue}): 1–2.', provenance={'source': 'official'})
    data = dict(schema='papertrack-browser-issue-v1', collector='papertrack-browser-v2',
                capture_method='fixed_browser', inventory_complete=True, errors=[], remaining_abstracts=0,
                url=url, volume='22', issue=issue, headings=[f'Volume 22, Issue {issue}'],
                card_count=1, entries=[row])
    card = (f'<div class="al-article-item-wrap"><div class="citation-label">'
            f'<a href="https://doi.org/{row["doi"]}">{row["doi"]}</a></div>'
            f'<a class="showAbstractLink" id="control-{issue}" aria-controls="panel-{issue}" '
            f'data-abstract-type="extract">Extract</a></div>')
    panel = f'<div id="panel-{issue}"><p>First paragraph.</p><div class="block-child-p">{extra}</div></div>'
    capture.write_text(json.dumps(data), encoding='utf-8')
    Path(str(capture) + '.html').write_text(card + panel, encoding='utf-8')
    (run_dir / 'result.json').write_text(json.dumps({
        'journal': 'jctc', 'provider': 'acs', 'volume': '22', 'issue': issue,
        'url': url, 'status': 'complete', 'updated_at': f'2026-10-18T00:00:{int(issue):02d}+00:00',
        'capture_path': str(capture), 'article_count': 1,
    }), encoding='utf-8')
    return run_dir, capture, row['doi']


def report_path(root, issue):
    path = root / 'acs' / 'jctc' / '22' / f'{issue}.md'
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def importer_that_writes(root, calls):
    def importer(task, cfg, zotero):
        calls.append((task.volume, task.issue, Path(task.browser_snapshot)))
        data = json.loads(Path(task.browser_snapshot).read_text())
        assert data['entries'][0]['abstract'].startswith('First paragraph.')
        report_path(root, task.issue).write_text(f'Imported {task.issue}\n', encoding='utf-8')
    return importer


def test_refresh_repairs_and_imports_local_cache_idempotently(tmp_path, monkeypatch):
    source_run, source_capture, doi = make_capture(tmp_path, '12')
    source_before = {p.name: p.read_bytes() for p in source_run.iterdir() if p.is_file()}
    calls = []
    monkeypatch.setattr(refresh, '_clear_presentation_capture_cache', lambda: calls.append(('clear',)))

    result = refresh.prepare_cached_extract_repairs(
        tmp_path, 'jctc', CFG, None, importer_that_writes(tmp_path, calls))

    assert len(result['repaired']) == 1
    assert result['repaired'][0]['status'] == 'complete'
    assert result['repaired'][0]['changed_dois'] == [doi]
    assert result['failures'] == []
    repaired_dir = Path(result['repaired'][0]['run_directory'])
    record = json.loads((repaired_dir / 'result.json').read_text())
    assert record['status'] == 'complete'
    assert record['kind'] == 'cached_extract_repair'
    assert record['cache_repaired'] is True and record['agent_invoked'] is False
    assert record['journal'] == 'jctc' and record['provider'] == 'acs'
    assert record['volume'] == '22' and record['issue'] == '12'
    assert record['source_run_directory'] == str(source_run.resolve())
    assert record['source_capture'] == str(source_capture.resolve())
    assert record['source_capture_sha256'] == hashlib.sha256(source_before['capture.json']).hexdigest()
    assert record['report_path'] == str(report_path(tmp_path, '12').resolve())
    assert all((source_run / name).read_bytes() == content for name, content in source_before.items())

    calls.clear()
    again = refresh.prepare_cached_extract_repairs(
        tmp_path, 'jctc', CFG, None, importer_that_writes(tmp_path, calls))
    assert again['repaired'] == [] and again['failures'] == []
    assert again['skipped'] == [{'volume': '22', 'issue': '12', 'reason': 'already_imported_repair'}]
    assert not calls
    assert len(list((tmp_path / '.papertrack-runs').glob('jctc-22-12-cached-repair-*'))) == 1


def test_unfinished_cache_repair_retries_import_without_copying_again(tmp_path, monkeypatch):
    make_capture(tmp_path, '12')
    calls = []

    def fail_once(task, cfg, zotero):
        calls.append(task.browser_snapshot)
        raise RuntimeError('temporary package import failure')

    failed = refresh.prepare_cached_extract_repairs(tmp_path, 'jctc', CFG, None, fail_once)
    assert len(failed['failures']) == 1
    repair_dirs = list((tmp_path / '.papertrack-runs').glob('jctc-22-12-cached-repair-*'))
    assert len(repair_dirs) == 1
    record_path = repair_dirs[0] / 'result.json'
    record = json.loads(record_path.read_text())
    assert record['status'] == 'cache_repaired'
    assert record['import_error'].endswith('temporary package import failure')

    imported = []
    retried = refresh.prepare_cached_extract_repairs(
        tmp_path, 'jctc', CFG, None, importer_that_writes(tmp_path, imported))
    assert retried['failures'] == []
    assert len(retried['repaired']) == 1 and retried['repaired'][0]['status'] == 'complete'
    assert len(list((tmp_path / '.papertrack-runs').glob('jctc-22-12-cached-repair-*'))) == 1
    assert len(calls) == 1 and len(imported) == 1


def test_bad_capture_is_visible_and_never_writes_markdown(tmp_path):
    make_capture(tmp_path, '12', abstract='This does not match the official first paragraph.')
    result = refresh.prepare_cached_extract_repairs(
        tmp_path, 'jctc', CFG, None, importer_that_writes(tmp_path, []))
    assert result['repaired'] == []
    assert len(result['failures']) == 1
    assert 'Not a proven legacy p-only Extract omission' in result['failures'][0]['failure']
    assert not report_path(tmp_path, '12').exists()
    assert not list((tmp_path / '.papertrack-runs').glob('jctc-22-12-cached-repair-*'))


def test_failed_issue_does_not_block_later_cache_candidate(tmp_path):
    make_capture(tmp_path, '12', abstract='Mismatch.')
    make_capture(tmp_path, '13')
    imported = []
    result = refresh.prepare_cached_extract_repairs(
        tmp_path, 'jctc', CFG, None, importer_that_writes(tmp_path, imported))
    assert [(x['volume'], x['issue']) for x in result['repaired']] == [('22', '13')]
    assert [(x['volume'], x['issue']) for x in result['failures']] == [('22', '12')]
    assert report_path(tmp_path, '13').is_file()
    assert not report_path(tmp_path, '12').exists()


def test_non_extract_validation_failure_stays_visible_and_does_not_repair(tmp_path, monkeypatch):
    make_capture(tmp_path, '12')
    attempts = []
    monkeypatch.setattr(refresh, 'validate_reading_controls',
                        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError('unsupported page control')))
    monkeypatch.setattr(refresh, 'repair_cached_extract_capture',
                        lambda *args, **kwargs: attempts.append('repair'))
    result = refresh.prepare_cached_extract_repairs(
        tmp_path, 'jctc', CFG, None, importer_that_writes(tmp_path, []))
    assert result['repaired'] == []
    assert result['failures'][0]['phase'] == 'source_validation_not_eligible'
    assert 'unsupported page control' in result['failures'][0]['failure']
    assert attempts == []
    assert not report_path(tmp_path, '12').exists()


def test_no_extract_capture_is_skipped_without_revalidating(tmp_path, monkeypatch):
    _, capture, _ = make_capture(tmp_path, '12')
    data = json.loads(capture.read_text())
    data['entries'][0]['abstract_type'] = 'abstract'
    capture.write_text(json.dumps(data))
    monkeypatch.setattr(refresh, 'validate_reading_controls',
                        lambda *args, **kwargs: pytest.fail('must skip validation'))
    monkeypatch.setattr(refresh, 'repair_cached_extract_capture',
                        lambda *args, **kwargs: pytest.fail('must skip repair'))
    result = refresh.prepare_cached_extract_repairs(
        tmp_path, 'jctc', CFG, None, importer_that_writes(tmp_path, []))
    assert result['failures'] == []
    assert result['skipped'] == [{'volume': '22', 'issue': '12', 'reason': 'no_extract_entries'}]


def test_failed_newer_record_does_not_mask_older_complete_capture(tmp_path, monkeypatch):
    old_run, _, _ = make_capture(tmp_path, '12')
    newer = tmp_path / '.papertrack-runs' / 'jctc-22-12-newer-partial'
    newer.mkdir()
    capture = newer / 'capture.json'
    source_data = json.loads((old_run / 'capture.json').read_text())
    source_data['entries'][0]['abstract'] = 'This source is not usable.'
    capture.write_text(json.dumps(source_data))
    Path(str(capture) + '.html').write_text((old_run / 'capture.json.html').read_text())
    (newer / 'result.json').write_text(json.dumps({
        'journal': 'jctc', 'provider': 'acs', 'volume': '22', 'issue': '12',
        'url': source_data['url'], 'status': 'failed', 'updated_at': '9999-01-01T00:00:00+00:00',
        'capture_path': str(capture),
    }))
    result = refresh.prepare_cached_extract_repairs(
        tmp_path, 'jctc', CFG, None, importer_that_writes(tmp_path, []))
    assert len(result['repaired']) == 1
    assert result['failures'] == []
    assert result['repaired'][0]['run_directory'].startswith(str(tmp_path))


def test_importer_must_write_report_before_result_is_complete(tmp_path):
    make_capture(tmp_path, '12')
    result = refresh.prepare_cached_extract_repairs(
        tmp_path, 'jctc', CFG, None, lambda *args: None)
    assert len(result['failures']) == 1
    assert 'Importer returned without writing report' in result['failures'][0]['failure']
    repair_dir = next((tmp_path / '.papertrack-runs').glob('jctc-22-12-cached-repair-*'))
    record = json.loads((repair_dir / 'result.json').read_text())
    assert record['status'] == 'cache_repaired'
    assert 'report_path' not in record
    assert not report_path(tmp_path, '12').exists()


def test_daily_migration_failure_does_not_skip_later_archive_discovery(tmp_path, monkeypatch):
    from papertrack.journals import cache as cached_capture_refresh

    calls = []
    monkeypatch.setattr(cached_capture_refresh, 'prepare_cached_extract_repairs',
                        lambda root, journal, cfg, zotero, importer: (
                            calls.append(('prepare', journal)) or
                            {'repaired': [], 'skipped': [], 'failures': ([{
                                'volume': '22', 'issue': '12', 'phase': 'repair', 'failure': 'no eligible capture'
                            }] if journal == 'jctc' else [])}))
    monkeypatch.setattr(cli, 'migrate_report_file',
                        lambda path, **kwargs: (calls.append(('migration', Path(path).name)) or
                                               SimpleNamespace(status='current', message='')))
    monkeypatch.setattr(cli, '_run_journal_refresh',
                        lambda task, zotero: calls.append(('archive_discovery', task.journal)))
    monkeypatch.setattr(cli, '_build_zotero', lambda: None)
    monkeypatch.setattr(cli, '_run_arxiv_backfill', lambda *args: None)
    monkeypatch.setattr(cli, '_run_arxiv_refresh', lambda *args: None)
    runtime_dir = tmp_path / 'runtime'
    runtime_dir.mkdir()
    monkeypatch.setenv('XDG_RUNTIME_DIR', str(runtime_dir))

    for journal in ('jctc', 'jcim'):
        cfg = journal_configs[journal]
        report_dir = Path(cli._journal_output_root(str(tmp_path), cfg)) / cfg['slug'] / '22'
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / '1.md').write_text('stub', encoding='utf-8')
    args = SimpleNamespace(
        repair_issue=[], data_dir=str(tmp_path), diagnostics_dir=str(tmp_path / 'logs'),
        journals='jctc,jcim', category='quant-ph', refresh_days=30,
        from_date='2026-01-01', agent_model='gpt-6-luna', agent_timeout=660,
    )
    with pytest.raises(RuntimeError, match='jctc:cached-extract:22/12'):
        cli._run_refresh(args, None)

    assert ('archive_discovery', 'jctc') in calls
    assert ('archive_discovery', 'jcim') in calls
    assert calls.index(('prepare', 'jctc')) < calls.index(('migration', '1.md')) < calls.index(('archive_discovery', 'jctc'))
    assert calls.index(('prepare', 'jcim')) < calls.index(('archive_discovery', 'jcim'))

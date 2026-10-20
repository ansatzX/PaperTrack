from datetime import date
from types import SimpleNamespace
import json

import pytest

from papertrack.journals import sync
from papertrack.config import journal_configs
from papertrack.journals.inventory import FormalIssue


@pytest.mark.parametrize('updated,today,hit', [
    ('2026-10-17T19:22:53.419958+00:00', date(2026, 10, 18), True),
    ('2026-10-17T19:22:53Z', date(2026, 10, 18), True),
    ('2026-10-18T18:00:00+00:00', date(2026, 10, 18), False),
    ('2026-10-18T01:00:00+08:00', date(2026, 10, 19), False),
    ('2026-10-18', date(2026, 10, 18), False),
    ('not a timestamp', date(2026, 10, 18), False),
    (None, date(2026, 10, 18), False),
])
def test_archive_cache_uses_local_calendar_day(monkeypatch, tmp_path, updated, today, hit):
    import time

    if not hasattr(time, 'tzset'):
        pytest.skip('Explicit process timezone requires tzset')
    url = 'https://pubs.acs.org/jcisd8/issue-covers'
    evidence = tmp_path / 'capture.json.html'
    evidence.write_text('<title>Official archive</title>')
    (tmp_path/'capture.json').write_text(json.dumps(dict(schema='papertrack-browser-archive-v1',
        requested_url=url, url=url, captured_at=updated)))
    records = [(dict(status='archive_captured', url=url, observed_url=url,
                     updated_at='2026-10-18T20:00:00Z'), tmp_path)]
    try:
        with monkeypatch.context() as env:
            env.setenv('TZ', 'Asia/Hong_Kong')
            time.tzset()
            result = sync._archive_cache(records, url, today)
            assert (result == (evidence, url)) if hit else result is None
    finally:
        time.tzset()


def test_sync_imports_valid_cache_and_fetches_only_holes(monkeypatch, tmp_path):
    from papertrack import cli
    rows = [FormalIssue('66', str(i), date(2026, 1, i), f'https://pubs.acs.org/jcisd8/issue/66/{i}')
            for i in [1, 2, 3]]
    monkeypatch.setattr(sync, 'discover_inventory', lambda *a: (rows, ['archive.html']))
    monkeypatch.setattr(sync, 'validated_cached_issue',
                        lambda records, cfg, row: tmp_path/'capture.json' if row.issue == '2' else None)
    calls = []
    monkeypatch.setattr(cli, '_build_zotero', lambda: None)
    monkeypatch.setattr(cli, '_import_journal_issue', lambda task, cfg, z: calls.append(('cache', task.issue)))
    monkeypatch.setattr(cli, '_run_journal_issue', lambda task, z: calls.append(('agent', task.issue)))
    args = SimpleNamespace(journal='jcim', data_dir=str(tmp_path), agent_model='gpt-6-luna', agent_timeout=660)
    sync.sync_journal(args, journal_configs['jcim'], from_date='2026-01-01', to_date='2026-10-17')
    assert calls == [('agent','1'),('cache','2'),('agent','3')]
    plan = json.loads(next((tmp_path/'.papertrack-runs').glob('*/plan.json')).read_text())
    assert plan['status'] == 'complete'
    assert [i['status'] for i in plan['issues']] == ['fetched', 'reused', 'fetched']


def test_markdown_or_partial_capture_is_not_a_cache_hit(tmp_path):
    folder = tmp_path/'old'
    folder.mkdir()
    (folder/'capture.json').write_text(json.dumps({'collector':'papertrack-browser-v2',
        'capture_method':'fixed_browser','remaining_abstracts':1}))
    (folder/'report.md').write_text('An existing report')
    records=[({'volume':'66','issue':'1','status':'complete'},folder)]
    row=FormalIssue('66','1',date(2026,1,12),'https://pubs.acs.org/jcisd8/issue/66/1')
    assert sync.validated_cached_issue(records,journal_configs['jcim'],row) is None


@pytest.mark.parametrize('journal', ['jctc', 'jcim', 'jacs', 'jcp', 'prl'])
def test_report_identity_path_mapping_for_all_daily_journals(tmp_path, journal):
    cfg = journal_configs[journal]
    provider = cfg.get('provider') or ('acs' if cfg.get('acs_code') else 'journals')
    reports = tmp_path/provider/cfg['slug']/'66'
    reports.mkdir(parents=True)
    (reports/'1.md').write_text('canonical report')
    (reports/'2').mkdir()
    (reports/'2'/'report.md').write_text('legacy report')
    (reports/'3').mkdir()  # A directory by itself is not an observed report.

    assert sync._previously_observed_issues(tmp_path, journal, cfg) == {('66', '1'), ('66', '2')}


def test_inventory_failure_never_starts_issue_fetches(monkeypatch,tmp_path):
    from papertrack import cli
    def fail(*args): raise ValueError('archive blocked')
    monkeypatch.setattr(sync,'discover_inventory',fail)
    monkeypatch.setattr(cli,'_run_journal_issue',lambda *a: pytest.fail('must not fetch'))
    args=SimpleNamespace(journal='jcim',data_dir=str(tmp_path))
    with pytest.raises(ValueError,match='archive blocked'):
        sync.sync_journal(args,journal_configs['jcim'],from_date='2026-01-01',to_date='2026-10-17')
    plan=json.loads(next((tmp_path/'.papertrack-runs').glob('*/plan.json')).read_text())
    assert plan['status']=='failed' and plan['issues']==[]


def test_inventory_reuses_fresh_archive_and_traverses_only_missing_page(monkeypatch, tmp_path):
    from papertrack.journals.task import archive_url
    root = tmp_path/'.papertrack-runs'/'old'
    root.mkdir(parents=True)
    cfg = journal_configs['prl']
    root.joinpath('result.json').write_text(json.dumps(dict(
        journal='prl', status='archive_captured', updated_at=date.today().isoformat(),
        url=archive_url(cfg), observed_url=archive_url(cfg))))
    from datetime import datetime, timezone
    root.joinpath('capture.json').write_text(json.dumps(dict(
        schema='papertrack-browser-archive-v1', requested_url=archive_url(cfg),
        url=archive_url(cfg), captured_at=datetime.now(timezone.utc).isoformat())))
    root.joinpath('capture.json.html').write_text('''<title>Archive</title>
    <a href="/prl/issues/137/1">3 July 2026</a>
    <table><tr><td><a href="/prl/issues/136">Volume 136</a></td><td>January - Present</td></tr></table>''')
    calls=[]
    def fetch(**kw):
        calls.append(kw['archive_page'])
        p=tmp_path/'volume.json'
        p.with_suffix('.json.html').write_text('<title>Volume</title><a href="/prl/issues/136/1">2 January 2026</a>')
        return SimpleNamespace(capture_path=p,record={'observed_url':kw['archive_page']})
    monkeypatch.setattr(sync,'run_issue_agent',fetch)
    args=SimpleNamespace(journal='prl',data_dir=str(tmp_path),agent_model='gpt-6-luna',agent_timeout=660)
    rows,_=sync.discover_inventory(args,cfg,date(2026,1,1),date(2026,10,17))
    assert [(r.volume,r.issue) for r in rows]==[('136','1'),('137','1')]
    assert calls==['https://journals.aps.org/prl/issues/136']


def test_complete_cache_requires_matching_html_not_status(tmp_path):
    folder=tmp_path/'run'
    folder.mkdir()
    capture=folder/'capture.json'
    data=dict(schema='papertrack-browser-issue-v1',capture_method='fixed_browser',
              collector='papertrack-browser-v2',url='https://pubs.acs.org/jcisd8/issue/66/1',
              volume='66',issue='1',headings=['Volume 66, Issue 1'],inventory_complete=True,
              card_count=1,errors=[],remaining_abstracts=0,
              entries=[dict(doi='10.1021/example',title='Paper',authors=['Author'],
                            citation='JCIM (2026) 66 (1): 1–4.',abstract_type='abstract',
                            abstract='Official abstract.',control='c',panel='p')])
    capture.write_text(json.dumps(data))
    records=[({'volume':'66','issue':'1','status':'complete'},folder)]
    row=FormalIssue('66','1',date(2026,1,12),data['url'])
    assert sync.validated_cached_issue(records,journal_configs['jcim'],row) is None
    html=folder/'capture.json.html'
    html.write_text('''<div class="al-article-item-wrap"><span class="citation-label">
    <a href="https://doi.org/10.1021/example">DOI</a></span>
    <a class="showAbstractLink" id="c" data-abstract-type="abstract" aria-controls="p">Abstract</a>
    </div><div id="p"><section class="abstract">Official abstract.</section></div>''')
    assert sync.validated_cached_issue(records,journal_configs['jcim'],row)==capture
    data['entries'][0]['abstract']='Invented caption'
    capture.write_text(json.dumps(data))
    assert sync.validated_cached_issue(records,journal_configs['jcim'],row) is None


def test_daily_discovery_does_not_refetch_known_failed_issue(monkeypatch, tmp_path):
    from papertrack import cli
    old = tmp_path / '.papertrack-runs' / 'jcim-sync-old'
    old.mkdir(parents=True)
    (old / 'plan.json').write_text(json.dumps(dict(
        journal='jcim', status='failed', archive_evidence=['saved-archive.html'],
        issues=[dict(volume='66', issue='1', status='failed')],
    )))
    rows = [FormalIssue('66', str(i), date(2026, 1, i),
                        f'https://pubs.acs.org/jcisd8/issue/66/{i}') for i in (1, 2)]
    monkeypatch.setattr(sync, 'discover_inventory', lambda *a: (rows, ['today.html']))
    monkeypatch.setattr(sync, 'validated_cached_issue', lambda *a: None)
    monkeypatch.setattr(cli, '_build_zotero', lambda: None)
    fetched = []
    monkeypatch.setattr(cli, '_run_journal_issue', lambda task, z: fetched.append(task.issue))
    args = SimpleNamespace(journal='jcim', data_dir=str(tmp_path), new_issues_only=True)
    with pytest.raises(RuntimeError, match='66/1'):
        sync.sync_journal(args, journal_configs['jcim'], from_date='2026-01-01', to_date='2026-10-17')
    assert fetched == ['2']
    plans = [json.loads(p.read_text()) for p in (tmp_path/'.papertrack-runs').glob('*/plan.json')
             if p.parent != old]
    assert [r['status'] for r in plans[0]['issues']] == ['repair_required', 'fetched']
    # The next daily discovery has no new issue: neither failed nor successful
    # historical identities trigger another agent, even when cache is absent.
    fetched.clear()
    with pytest.raises(RuntimeError, match='66/1, 66/2'):
        sync.sync_journal(args, journal_configs['jcim'], from_date='2026-01-01', to_date='2026-10-17')
    assert fetched == []


def test_daily_treats_existing_canonical_and_legacy_reports_as_known_not_cache(monkeypatch, tmp_path):
    from papertrack import cli
    reports = tmp_path/'acs'/'jcim'
    (reports/'66').mkdir(parents=True)
    (reports/'66'/'1.md').write_text('canonical report')
    (reports/'66'/'2').mkdir()
    (reports/'66'/'2'/'report.md').write_text('legacy report')
    rows = [FormalIssue('66', str(i), date(2026, 1, i),
                        f'https://pubs.acs.org/jcisd8/issue/66/{i}') for i in (1, 2)]
    monkeypatch.setattr(sync, 'discover_inventory', lambda *a: (rows, ['today.html']))
    monkeypatch.setattr(sync, 'validated_cached_issue', lambda *a: None)
    monkeypatch.setattr(cli, '_build_zotero', lambda: None)
    fetched = []
    monkeypatch.setattr(cli, '_run_journal_issue', lambda task, z: fetched.append(task.issue))
    args = SimpleNamespace(journal='jcim', data_dir=str(tmp_path), new_issues_only=True)
    with pytest.raises(RuntimeError, match='66/1, 66/2'):
        sync.sync_journal(args, journal_configs['jcim'], from_date='2026-01-01', to_date='2026-10-17')
    assert fetched == []
    plan = json.loads(next((tmp_path/'.papertrack-runs').glob('*/plan.json')).read_text())
    assert [row['status'] for row in plan['issues']] == ['repair_required', 'repair_required']

    # An explicit non-daily reconciliation remains an authorized way to fetch.
    args.new_issues_only = False
    sync.sync_journal(args, journal_configs['jcim'], from_date='2026-01-01', to_date='2026-10-17')
    assert fetched == ['1', '2']


def test_unattempted_plan_rows_resume_but_attempt_started_rows_do_not_retry(monkeypatch, tmp_path):
    from papertrack import cli
    old = tmp_path/'.papertrack-runs'/'jcim-sync-interrupted'
    old.mkdir(parents=True)
    (old/'plan.json').write_text(json.dumps(dict(
        journal='jcim', status='syncing', archive_evidence=['archive.html'],
        issues=[dict(volume='66', issue='1', status='missing')],
    )))
    rows = [FormalIssue('66', '1', date(2026, 1, 1),
                        'https://pubs.acs.org/jcisd8/issue/66/1')]
    monkeypatch.setattr(sync, 'discover_inventory', lambda *a: (rows, ['today.html']))
    monkeypatch.setattr(sync, 'validated_cached_issue', lambda *a: None)
    monkeypatch.setattr(cli, '_build_zotero', lambda: None)
    calls = []
    def interrupted(task, _zotero):
        plan_path = next(p for p in (tmp_path/'.papertrack-runs').glob('jcim-sync-*/plan.json')
                         if p.parent != old)
        plan = json.loads(plan_path.read_text())
        current = next(row for row in plan['issues'] if row['issue'] == task.issue)
        calls.append(current['status'])
        raise SystemExit('simulated process interruption')
    monkeypatch.setattr(cli, '_run_journal_issue', interrupted)
    args = SimpleNamespace(journal='jcim', data_dir=str(tmp_path), new_issues_only=True)
    with pytest.raises(SystemExit, match='simulated process interruption'):
        sync.sync_journal(args, journal_configs['jcim'], from_date='2026-01-01', to_date='2026-10-17')
    assert calls == ['attempt_started']

    # The persisted attempt marker makes the interrupted issue visible for
    # explicit repair without automatically calling the agent on every day.
    calls.clear()
    with pytest.raises(RuntimeError, match='66/1'):
        sync.sync_journal(args, journal_configs['jcim'], from_date='2026-01-01', to_date='2026-10-17')
    assert calls == []


def test_cached_parser_failure_does_not_refetch_or_block_new_issue(monkeypatch, tmp_path):
    from papertrack import cli
    rows = [FormalIssue('66', str(i), date(2026, 1, i),
                        f'https://pubs.acs.org/jcisd8/issue/66/{i}') for i in (1, 2)]
    monkeypatch.setattr(sync, 'discover_inventory', lambda *a: (rows, ['archive.html']))
    def cache(records, cfg, row):
        if row.issue == '1':
            raise ValueError('Cached HTML needs an extractor update, not a refetch')
        return None
    monkeypatch.setattr(sync, 'validated_cached_issue', cache)
    monkeypatch.setattr(cli, '_build_zotero', lambda: None)
    fetched = []
    monkeypatch.setattr(cli, '_run_journal_issue', lambda task, z: fetched.append(task.issue))
    args = SimpleNamespace(journal='jcim', data_dir=str(tmp_path), new_issues_only=True)
    with pytest.raises(RuntimeError, match='66/1'):
        sync.sync_journal(args, journal_configs['jcim'], from_date='2026-01-01', to_date='2026-10-17')
    assert fetched == ['2']
    plan = json.loads(next((tmp_path/'.papertrack-runs').glob('*/plan.json')).read_text())
    assert plan['issues'][0]['status'] == 'repair_required'


@pytest.mark.parametrize('old_plan', [False, True])
@pytest.mark.parametrize('new_issues_only', [False, True])
def test_wrapped_math_failure_is_repair_required_without_refetch(monkeypatch, tmp_path, old_plan, new_issues_only):
    from papertrack import cli
    folder = tmp_path/'.papertrack-runs'/'cached-issue'
    folder.mkdir(parents=True)
    url = 'https://pubs.acs.org/jcisd8/issue/66/1'
    data = dict(schema='papertrack-browser-issue-v1', capture_method='fixed_browser',
                collector='papertrack-browser-v2', url=url, volume='66', issue='1',
                headings=['Volume 66, Issue 1'], inventory_complete=True,
                card_count=1, errors=[], remaining_abstracts=0,
                entries=[dict(doi='10.1021/example', title='Paper', authors=['Author'],
                              citation='JCIM (2026) 66 (1): 1–4.', abstract_type='abstract',
                              abstract='r', control='c', panel='p')])
    (folder/'capture.json').write_text(json.dumps(data))
    (folder/'result.json').write_text(json.dumps(dict(journal='jcim', volume='66', issue='1', status='failed')))
    (folder/'capture.json.html').write_text(
        '<div class="al-article-item-wrap"><span class="citation-label">'
        '<a href="https://doi.org/10.1021/example">DOI</a></span>'
        '<a class="showAbstractLink" id="c" aria-controls="p" data-abstract-type="abstract">Abstract</a></div>'
        '<div id="p"><section class="abstract">r<math><mover accent="true"><mi></mi></mover></math></section></div>')
    if old_plan:
        old = folder.parent/'jcim-sync-old'
        old.mkdir()
        (old/'plan.json').write_text(json.dumps(dict(journal='jcim', archive_evidence=['old.html'],
            issues=[dict(volume='66', issue='1')])))
    row = FormalIssue('66', '1', date(2026, 1, 12), url)
    monkeypatch.setattr(sync, 'discover_inventory', lambda *args: ([row], ['archive.html']))
    monkeypatch.setattr(cli, '_build_zotero', lambda: None)
    monkeypatch.setattr(cli, '_run_journal_issue', lambda *args: pytest.fail('Must not refetch an unrenderable cached formula'))
    args = SimpleNamespace(journal='jcim', data_dir=str(tmp_path), new_issues_only=new_issues_only)
    with pytest.raises(RuntimeError, match='66/1'):
        sync.sync_journal(args, journal_configs['jcim'], from_date='2026-01-01', to_date='2026-10-18')
    plans = [json.loads(p.read_text()) for p in folder.parent.glob('*-sync-*/plan.json') if p.parent.name != 'jcim-sync-old']
    item = plans[0]['issues'][0]
    assert item['status'] == 'repair_required'
    assert 'Reading source cannot be rendered: 10.1021/example: Missing usable literal mathematical source text' in item['cache_error']
    assert item['capture'] is None


@pytest.mark.parametrize('mutation', ['missing_time', 'naive_time', 'wrong_request', 'wrong_observed',
                                      'wrong_record_url', 'wrong_path', 'missing_html', 'legacy_issue'])
def test_archive_freshness_requires_own_timestamp_and_matching_evidence(tmp_path, mutation):
    url = 'https://pubs.acs.org/jcisd8/issue-covers'
    metadata = dict(schema='papertrack-browser-archive-v1', requested_url=url, url=url,
                    captured_at='2026-10-18T12:00:00+00:00')
    record = dict(status='archive_captured', url=url, observed_url=url, updated_at=metadata['captured_at'])
    evidence = tmp_path/'capture.json.html'
    evidence.write_text('Archive')
    if mutation == 'missing_time': metadata.pop('captured_at')
    if mutation == 'naive_time': metadata['captured_at'] = '2026-10-18T12:00:00'
    if mutation == 'wrong_request': metadata['requested_url'] += '/year/2025'
    if mutation == 'wrong_observed': metadata['url'] += '/year/2025'
    if mutation == 'wrong_record_url': record['url'] += '/year/2025'
    if mutation == 'wrong_path': record['capture_path'] = str(tmp_path/'different.json')
    if mutation == 'missing_html': evidence.unlink()
    if mutation == 'legacy_issue':
        record.update(status='complete', archive_url=url)
        metadata['schema'] = 'papertrack-browser-issue-v1'
        (tmp_path/'capture.json.archive.html').write_text('Old archive')
    (tmp_path/'capture.json').write_text(json.dumps(metadata))
    assert sync._archive_cache([(record,tmp_path)], url, date(2026,10,18)) is None


def test_archive_captured_before_local_midnight_is_not_refreshed_by_later_completion(monkeypatch, tmp_path):
    import time
    if not hasattr(time, 'tzset'): pytest.skip('Requires tzset')
    url = 'https://pubs.acs.org/jcisd8/issue-covers'
    capture = tmp_path/'capture.json'
    evidence = tmp_path/'capture.json.html'
    evidence.write_text('Archive')
    data = dict(schema='papertrack-browser-archive-v1', requested_url=url, url=url,
                captured_at='2026-10-17T15:59:59Z')
    capture.write_text(json.dumps(data))
    records = [(dict(status='archive_captured', url=url, observed_url=url,
                     updated_at='2026-10-17T16:00:05Z'), tmp_path)]
    try:
        with monkeypatch.context() as env:
            env.setenv('TZ','Asia/Hong_Kong');time.tzset()
            assert sync._archive_cache(records,url,date(2026,10,18)) is None
            assert sync._archive_cache(records,url,date(2026,10,17)) == (evidence,url)
            data['captured_at']='2026-10-17T16:00:01Z'
            capture.write_text(json.dumps(data))
            assert sync._archive_cache(records,url,date(2026,10,18)) == (evidence,url)
    finally:
        time.tzset()

"""Agent handoff contracts and durable report acceptance, without live services."""
import json
import socket
from html import escape

import pytest

from papertrack import cli
from papertrack.config import journal_configs
from papertrack.journals.task import issue_task, issue_url
from papertrack.reports.render import filter_journal_to_md


def capture():
    return {
        'schema': 'papertrack-browser-issue-v1', 'capture_method': 'agent',
        'captured_at': '2026-10-16T10:00:00+08:00',
        'url': 'https://pubs.acs.org/jcisd8/issue/66/12',
        'volume': '66', 'issue': '12', 'headings': ['Volume 66, Issue 12'],
        'inventory_complete': True, 'card_count': 1, 'errors': [],
        'entries': [{'doi': '10.1021/ACS.JCIM.example', 'title': 'Observed title',
                     'authors': ['A Author'], 'citation': 'JCIM (2026) 66 (12): 12–19.',
                     'abstract': 'Observed expanded abstract.', 'abstract_type': 'abstract'}],
    }


def publish(tmp_path, data):
    path = tmp_path / 'capture.json'
    cards = []
    for index, row in enumerate(data['entries']):
        row.update(control=f'control-{index}', panel=f'panel-{index}')
        kind = row['abstract_type']
        control = (f'<a class="showAbstractLink" id="control-{index}" '
                   f'data-abstract-type="{kind}" aria-controls="panel-{index}">{kind}</a>'
                   if kind != 'unavailable' else '')
        cards.append(f'<div class="al-article-item-wrap"><span class="citation-label">'
                     f'<a href="https://doi.org/{escape(row["doi"])}">DOI</a></span>{control}</div>'
                     f'<div id="panel-{index}"><section class="abstract">'
                     f'<p>{escape(row["abstract"])}</p></section></div>')
    path.with_suffix('.json.html').write_text(''.join(cards))
    path.write_text(json.dumps(data))
    return filter_journal_to_md('JCIM', 'jcim', '66', '12', '1549-9596',
                                str(tmp_path), acs_code='jcisd8', browser_snapshot=str(path))


@pytest.mark.parametrize('key,expected', [
    ('jcim', 'https://pubs.acs.org/jcisd8/issue/66/12'),
    ('jcp', 'https://pubs.aip.org/aip/jcp/issue/66/12'),
    ('prl', 'https://journals.aps.org/prl/issues/66/12'),
])
def test_task_targets_formal_issue(key, expected):
    assert issue_url(journal_configs[key], '66', '12') == expected


@pytest.mark.parametrize('volume,issue', [('../66', '12'), ('66', '../12'), ('66', '')])
def test_task_rejects_non_numeric_placement(volume, issue):
    with pytest.raises(ValueError):
        issue_url(journal_configs['jcim'], volume, issue)


def test_task_describes_data_and_import_without_selectors(tmp_path):
    prompt = issue_task('jcim', journal_configs['jcim'], '66', '12', 2026,
                        str(tmp_path / 'capture.json'), str(tmp_path / 'reports'))
    assert 'verbatim' in prompt
    assert 'unavailable_reason' in prompt
    assert '--browser-snapshot' in prompt
    assert str(tmp_path / 'capture.json') in prompt
    assert 'inventory_complete' in prompt
    assert 'retry the SAME' in prompt
    assert 'If the same DOI fails again, stop' in prompt
    assert 'exactly ONE collector call per cua_repl invocation' in prompt
    assert 'Do not call browser.nameSession' in prompt
    assert 'Close EVERY task-created' in prompt


@pytest.mark.parametrize('mode', ['issue', 'latest', 'backfill'])
def test_task_cli_never_contacts_zotero_or_network(monkeypatch, capsys, tmp_path, mode):
    monkeypatch.setattr(cli, '_build_zotero', lambda: pytest.fail('task contacted Zotero'))
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **kw: pytest.fail('network'))
    argv = ['papertrack', 'journal', mode, '--journal', 'jcim', '--data-dir', str(tmp_path)]
    if mode == 'issue':
        argv += ['--volume', '66', '--issue', '12']
    if mode == 'backfill':
        argv += ['--from-year', '2020']
    monkeypatch.setattr('sys.argv', argv)
    cli.main()
    output = capsys.readouterr().out
    assert 'pubs.acs.org' in output
    assert not list(tmp_path.rglob('*.md'))


def test_capture_import_offline_and_preserves_checkbox_collection_and_anchor(monkeypatch, tmp_path):
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **kw: pytest.fail('network'))
    data = capture()
    assert publish(tmp_path, data)
    report = tmp_path / 'jcim/66/12.md'
    text = report.read_text().replace('10.1021/acs.jcim.example', '10.1021/ACS.JCIM.example')
    text = text.replace('- [ ] [', '- [x] [', 1)
    text = text.replace('## collected\n\n\n## not collected', '## collected')
    # Put the paper in the collected section independent of whitespace rendering.
    text = text.replace('## not collected', '') + '\n## not collected\n'
    report.write_text(text)
    data['entries'][0]['doi'] = data['entries'][0]['doi'].lower()
    data['entries'][0]['title'] = 'Updated title'
    assert publish(tmp_path, data)
    after = report.read_text()
    assert '### 10.1021/ACS.JCIM.example' in after
    assert '- [x] [10.1021/ACS.JCIM.example]' in after
    assert 'Updated title' in after
    assert '### 10.1021/' in after.split('## not collected')[0]


@pytest.mark.parametrize('method', ['agent', 'fixed_browser'])
@pytest.mark.parametrize('fault', ['inventory', 'abstract', 'errors', 'availability', 'identity', 'duplicate'])
def test_invalid_capture_preserves_prior_report(tmp_path, fault, method):
    data = capture()
    data['capture_method'] = method
    assert publish(tmp_path, data)
    target = tmp_path / 'jcim/66/12.md'
    before = target.read_bytes()
    if fault == 'inventory': data['inventory_complete'] = False
    if fault == 'abstract': data['entries'][0]['abstract'] = ''
    if fault == 'errors': data['errors'] = ['blocked abstract page']
    if fault == 'availability':
        data['entries'][0].update(abstract='', abstract_type='unavailable')
    if fault == 'identity': data['issue'] = '13'
    if fault == 'duplicate':
        data['entries'] *= 2
        data['card_count'] = 2
    with pytest.raises(ValueError): publish(tmp_path, data)
    assert target.read_bytes() == before


def test_lost_member_preserves_prior_report(tmp_path):
    data = capture()
    second = dict(data['entries'][0], doi='10.1021/second')
    data['entries'].append(second)
    data['card_count'] = 2
    assert publish(tmp_path, data)
    target = tmp_path / 'jcim/66/12.md'
    before = target.read_bytes()
    with pytest.raises(ValueError, match='prior issue members'):
        publish(tmp_path, capture())
    assert target.read_bytes() == before


@pytest.mark.parametrize('kind', ['abstract', 'extract'])
def test_unavailable_cannot_erase_prior_abstract(tmp_path, kind):
    data = capture()
    data['entries'][0]['abstract_type'] = kind
    assert publish(tmp_path, data)
    target = tmp_path / 'jcim/66/12.md'
    before = target.read_bytes()
    if kind == 'extract':
        assert '> [!quote]- Extract（原文摘录）'.encode() in before
        assert b'papertrack_provenance' not in before
    data['entries'][0].update(abstract='', abstract_type='unavailable', unavailable_reason='No control visible')
    with pytest.raises(ValueError, match='prior abstract'):
        publish(tmp_path, data)
    assert target.read_bytes() == before


def test_journal_repair_defers_to_agent_without_fetch(tmp_path):
    from papertrack.reports.repair import RepairCandidate, repair_reports
    candidate = RepairCandidate(str(tmp_path / 'jcim/66/12.md'), 'journal', 'missing',
                                journal_slug='jcim', volume='66', issue='12')
    result, = repair_reports(str(tmp_path), [candidate], assume_yes=True)
    assert result.outcome == 'deferred'
    assert result.fetch_attempted is False
    assert 'agent capture' in result.message


def test_empty_collected_section_stays_empty_on_rerender(tmp_path):
    data = capture()
    assert publish(tmp_path, data)
    report = tmp_path / 'jcim/66/12.md'
    before = report.read_text()
    assert '### ' not in before.split('## not collected')[0]
    assert publish(tmp_path, data)
    after = report.read_text()
    assert '### ' not in after.split('## not collected')[0]
    assert '### 10.1021/' in after.split('## not collected')[1]

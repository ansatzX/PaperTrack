"""Regression checks for the independently reproduced review findings."""
import json
import socket

import pytest

from papertrack.adapter_result import AdapterResult
from papertrack.journals.capture import read_browser_issue
from papertrack.journals.entry import JournalEntry
from papertrack.journals import agent
from papertrack.config import journal_configs
from papertrack.reports import render, markdown, migration, presentation
from tests.test_browser_issue import snapshot
from tests.test_journal_agent import fake_codex as _fake_codex


@pytest.fixture
def fake_codex(tmp_path, monkeypatch):
    return _fake_codex.__wrapped__(tmp_path, monkeypatch)


@pytest.mark.parametrize('outcome', ['fetch_failed', 'parse_failed'])
def test_arxiv_cli_propagates_authoritative_failure(monkeypatch, tmp_path, outcome):
    from papertrack import cli
    monkeypatch.setattr(cli, '_build_zotero', lambda: None)
    monkeypatch.setattr(render, 'query_arxiv_result', lambda *a, **k: AdapterResult(
        entries={}, outcome=outcome, provider='arxiv', source='arxiv'))
    monkeypatch.setattr('sys.argv', ['papertrack', 'arxiv', 'backfill', '--category',
                        'quant-ph', '--from-date', '2026-10-01', '--to-date',
                        '2026-10-01', '--data-dir', str(tmp_path)])
    with pytest.raises(render.ArxivFetchError, match=outcome):
        cli.main()
    assert not list(tmp_path.rglob('*.md'))


@pytest.mark.parametrize('value', ['../../escape', '/absolute', '１', '0', '-1', ''])
@pytest.mark.parametrize('component', ['volume', 'issue'])
def test_import_and_writer_reject_unsafe_placement(tmp_path, value, component):
    placement={'volume': '66', 'issue': '12', component: value}
    with pytest.raises(ValueError, match='positive ASCII'):
        read_browser_issue(str(tmp_path/'not-read.json'), provider='acs', code='jcisd8',
                           journal_name='JCIM', **placement)
    entry=JournalEntry('10.1021/example', 'Paper', ['Author'], 'Source abstract')
    with pytest.raises(ValueError, match='positive ASCII'):
        render.write_journal_entries_to_md('JCIM', 'jcim', entries={entry.doi:entry},
                                          md_folder=str(tmp_path), **placement)
    assert not list(tmp_path.rglob('*.md'))


@pytest.mark.parametrize('source', [
    '$[[4,2,2]]$', '$$\n[[4,2,2]]\n$$', r'\([[4,2,2]]\)',
    '`[[not a link]]`', '```text\n[[literal]]\n```',
])
def test_wikilink_escaping_preserves_math_and_code(source):
    result=markdown.abstract_markdown('[[link]] '+source)
    assert result == r'\[\[link\]\] '+source
    assert markdown.abstract_markdown(result)==result


def test_multiline_callout_math_is_unchanged():
    source='> [!quote]- Abstract\n> $$\n> [[4,2,2]]\n> $$\n'
    assert markdown.repair_presentation(source)==source


@pytest.mark.parametrize('relative', [
    'arxiv/cs.AI/2026/07/07.md',
    'arxiv/2026/07/07/cs.AI/07.md',
    'arxiv/2026/07/cs.AI/07.md',
    'arxiv/cs.AI/2026/07/07/cs.AI/07.md',
    'arxiv/cs.AI/2026/07/cs.AI/07.md',
    'arxiv/2026/07/cs.AI/07/07.md',
    'arxiv/cs.AI/2026/07/cs.AI/07/07.md',
])
def test_arxiv_migration_recognizes_supported_layouts(tmp_path, relative):
    target=tmp_path/relative
    target.parent.mkdir(parents=True)
    target.write_text('''# 2026-07-07 preprint by arxiv_tools

---
tags:
  - #cs.AI-2026-07-07
---

```dataview
TASK
from #cs.AI-2026-07-07
```

## collected

## not collected

### arXiv:2607.00001

- [x] [arXiv:2607.00001](https://arxiv.org/abs/2607.00001) my note
''')
    result=migration.migrate_report_file(str(target),data_dir=str(tmp_path))
    assert result.status=='migrated', result.message
    text=target.read_text()
    assert 'papertrack_category: cs.AI' in text
    assert 'papertrack_date: "2026-07-07"' in text
    assert '- [x]' in text and 'my note' in text
    assert migration.migrate_report_file(str(target), data_dir=str(tmp_path)).status=='current'


@pytest.mark.parametrize('relative', [
    'arxiv/2026/07/07/unknown/07.md', 'arxiv/cs.AI/2026/07/08/cs.AI/07.md',
    'arxiv/cs.AI/2026/07/07/cs.LG/07.md', 'arxiv/cs.AI/2026/99/07.md',
])
def test_unknown_or_conflicting_arxiv_layout_is_not_guessed(tmp_path, relative):
    with pytest.raises(ValueError):
        migration._arxiv_path_identity(tmp_path/relative,str(tmp_path))


def test_unavailable_cannot_bypass_recent_requirement(tmp_path, monkeypatch):
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **k: pytest.fail('network'))
    data=snapshot()
    data['entries'][0].update(abstract='',abstract_type='unavailable',unavailable_reason='No reading control')
    path=tmp_path/'capture.json';path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='Recent Abstract supplementation is unresolved'):
        read_browser_issue(str(path),provider='acs',code='jcisd8',volume='66',issue='12',journal_name='JCIM')


def test_presentation_uses_recorded_capture_path(tmp_path):
    folder=tmp_path/'.papertrack-runs/example';folder.mkdir(parents=True)
    actual=tmp_path/'captures/explicit.json';actual.parent.mkdir();actual.write_text('{}')
    report=tmp_path/'acs/jcim/66/12.md'
    (folder/'result.json').write_text(json.dumps(dict(status='complete',report_path=str(report),
        capture_path=str(actual),updated_at='2026-10-20')))
    presentation._captures.cache_clear()
    try:
        assert presentation._captures(str(tmp_path))[str(report.resolve())][1]==actual
    finally:
        presentation._captures.cache_clear()


@pytest.mark.parametrize('missing', [True, False])
def test_agent_records_unconfirmed_or_absent_scope(fake_codex, tmp_path, caplog, missing):
    control=tmp_path/'systemctl'
    control.write_text('#!/bin/bash\nif [[ "$*" == *" show "* ]]; then\n'
                       + ('echo not-found\n' if missing else 'echo loaded\n')
                       + 'fi\nexit 7\n')
    control.chmod(0o755)
    run=agent.run_issue_agent(journal='jcim',cfg=journal_configs['jcim'],volume='66',
        issue='12',year=2026,data_dir=str(tmp_path/'reports'))
    assert run.record['status']=='validated'
    assert run.record['resource_cleanup']['status']==('already_gone' if missing else 'unconfirmed')
    assert ('Could not confirm resource unit cleanup' in caplog.text)==(not missing)

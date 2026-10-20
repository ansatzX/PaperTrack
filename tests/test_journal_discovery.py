import pytest

from papertrack.config import journal_configs
from papertrack.journals.discovery import validate_latest_capture
from papertrack.journals.task import archive_url, issue_task, issue_url


@pytest.mark.parametrize('journal,links', [
    ('jcim', ['/jcisd8/issue/66/9', '/toc/jcisd8/66/12']),
    ('jcp', ['/aip/jcp/issue/164/9', '/jcp/issue/164/12']),
    ('prl', ['/prl/issues/136/9', '/prl/issues/136/12']),
])
def test_latest_observed_issue_uses_numeric_order_and_official_links(tmp_path, journal, links):
    cfg = journal_configs[journal]
    volume = {'jcim': '66', 'jcp': '164', 'prl': '136'}[journal]
    evidence = tmp_path / 'archive.html'
    evidence.write_text('<title>Issue archive</title>' + ''.join(
        f'<a href="{link}">Issue</a>' for link in links + [
            'https://example.org/prl/issues/999/999', '/doi/10.1021/example']))
    capture = dict(volume=volume, issue='12', url=issue_url(cfg, volume, '12'))
    assert validate_latest_capture(cfg, capture, evidence) == (volume, '12')
    capture['issue'] = '9'
    with pytest.raises(ValueError, match='not the latest'):
        validate_latest_capture(cfg, capture, evidence)


@pytest.mark.parametrize('html', ['<title>Just a moment...</title>',
                                 '<title>Archive</title><a href="/asap">Recent</a>'])
def test_discovery_failure_is_not_empty_success(tmp_path, html):
    evidence = tmp_path / 'archive.html'
    evidence.write_text(html)
    with pytest.raises(ValueError):
        validate_latest_capture(journal_configs['jcim'], {}, evidence)


def test_latest_prompt_uses_fixed_discovery_and_one_existing_collector(tmp_path):
    cfg = journal_configs['jcim']
    prompt = issue_task('jcim', cfg, '', '', 0, str(tmp_path/'capture.json'),
                        str(tmp_path), capture_only=True, discover_latest=True)
    assert archive_url(cfg) in prompt
    assert 'discoverCuaLatestIssue' in prompt
    assert 'var request = discovery.request;' in prompt
    assert 'collectCuaIssueBatch' in prompt
    assert 'Do not invent URLs' in prompt
    assert 'Do not run the import command' in prompt

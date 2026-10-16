import importlib.util
import json
from pathlib import Path
import pytest

spec = importlib.util.spec_from_file_location('workflow_render', Path(__file__).parents[1] / 'render.py')
render = importlib.util.module_from_spec(spec)
spec.loader.exec_module(render)

JOB = dict(journal='jcim', volume='66', issue='12', year=2026)

def capture(tmp_path):
    data = {'schema': 'papertrack-browser-issue-v1', 'url': 'https://pubs.acs.org/jcisd8/issue/66/12',
            'volume': '66', 'issue': '12', 'headings': ['Volume 66, Issue 12'], 'card_count': 1,
            'entries': [{'doi': '10.1021/test', 'title': 'Correction', 'authors': [],
                         'citation': '(2026) 66 (12): 1.', 'abstract': 'Publisher extract', 'abstract_type': 'extract'}]}
    file = tmp_path / 'capture.json'
    file.write_text(json.dumps(data))
    return data, file


def test_publish_preserves_checked_state_and_creates_backup(tmp_path):
    data, file = capture(tmp_path)
    result = render.publish(JOB, str(file), str(tmp_path / 'output'))
    target = Path(result['path'])
    target.write_text(target.read_text().replace('- [ ]', '- [x]'))
    old = target.read_bytes()
    again = render.publish(JOB, str(file), str(tmp_path / 'output'))
    assert '- [x]' in target.read_text()
    assert 'official_browser_extract' in target.read_text()
    assert again['abstracts'] == 1
    assert any(p.read_bytes() == old for p in target.parent.glob('*.bak'))


def test_wrong_issue_or_lost_content_leaves_report_untouched(tmp_path):
    data, file = capture(tmp_path)
    target = Path(render.publish(JOB, str(file), str(tmp_path / 'output'))['path'])
    old = target.read_bytes()
    data['entries'][0].update(abstract='', abstract_type='')
    file.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='prior_abstract'):
        render.publish(JOB, str(file), str(tmp_path / 'output'))
    assert target.read_bytes() == old
    data['url'] = data['url'].replace('/12', '/13')
    file.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        render.publish(JOB, str(file), str(tmp_path / 'output'))
    assert target.read_bytes() == old

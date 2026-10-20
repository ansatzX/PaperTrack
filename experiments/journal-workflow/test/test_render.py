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
                         'citation': '(2026) 66 (12): 1.', 'abstract': 'Publisher extract', 'abstract_type': 'extract',
                         'control': 'e', 'panel': 'p'}]}
    file = tmp_path / 'capture.json'
    file.write_text(json.dumps(data))
    Path(str(file) + '.html').write_text('''<div class="al-article-item-wrap">
    <span class="citation-label"><a href="https://doi.org/10.1021/test">DOI</a></span>
    <a class="showAbstractLink" id="e" data-abstract-type="extract" aria-controls="p">Extract</a>
    </div><div id="p"><p>Publisher extract</p></div>''')
    return data, file


def test_workflow_rejects_substituted_text_without_replacing_report(tmp_path):
    data, file = capture(tmp_path)
    target = Path(render.publish(JOB, str(file), str(tmp_path / 'output'))['path'])
    before = target.read_bytes()
    data['entries'][0]['abstract'] = 'An invented correction summary'
    file.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='Extract text conflicts'):
        render.publish(JOB, str(file), str(tmp_path / 'output'))
    assert target.read_bytes() == before


def test_publish_preserves_checked_state_and_creates_backup(tmp_path):
    data, file = capture(tmp_path)
    result = render.publish(JOB, str(file), str(tmp_path / 'output'))
    target = Path(result['path'])
    target.write_text(target.read_text().replace('- [ ]', '- [x]'))
    old = target.read_bytes()
    again = render.publish(JOB, str(file), str(tmp_path / 'output'))
    assert '- [x]' in target.read_text()
    assert '> [!quote]- Extract（原文摘录）' in target.read_text()
    assert 'papertrack_provenance' not in target.read_text()
    assert again['abstracts'] == 1
    assert any(p.read_bytes() == old for p in target.parent.glob('*.bak'))


def test_wrong_issue_or_lost_content_leaves_report_untouched(tmp_path):
    data, file = capture(tmp_path)
    target = Path(render.publish(JOB, str(file), str(tmp_path / 'output'))['path'])
    old = target.read_bytes()
    data['entries'][0].update(abstract='', abstract_type='unavailable',
                              unavailable_reason='No control on observed issue card')
    file.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='prior_abstract'):
        render.publish(JOB, str(file), str(tmp_path / 'output'))
    assert target.read_bytes() == old
    data['url'] = data['url'].replace('/12', '/13')
    file.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        render.publish(JOB, str(file), str(tmp_path / 'output'))
    assert target.read_bytes() == old

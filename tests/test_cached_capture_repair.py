import hashlib
import json
from pathlib import Path

import pytest

from papertrack.journals.cache_repair import repair_cached_extract_capture

DOI = '10.1021/example'
URL = 'https://pubs.acs.org/jctcce/issue/22/12'
ARGS = dict(provider='acs', code='jctcce', volume='22', issue='12', journal_name='JCTC')


def fixture(tmp_path, *, math='<math><mi>b...</mi></math>', sidecar=False):
    source_dir = tmp_path / 'source'
    source_dir.mkdir()
    capture = source_dir / 'capture.json'
    row = dict(doi=DOI, title='Correction', authors=['A'], abstract='First paragraph.',
               abstract_type='extract', control='control', panel='panel',
               citation='J. Chem. Theory Comput. (2026) 22 (12): 1–2.',
               provenance={'source': 'official'})
    data = dict(schema='papertrack-browser-issue-v1', capture_method='fixed_browser',
                inventory_complete=True, errors=[], url=URL, volume='22', issue='12',
                headings=['Volume 22, Issue 12'], card_count=1, entries=[row])
    card = ('<div class="al-article-item-wrap"><div class="citation-label">'
            f'<a href="https://doi.org/{DOI}">{DOI}</a></div>'
            '<a class="showAbstractLink" id="control" aria-controls="panel" data-abstract-type="extract">Extract</a></div>')
    panel = '<div id="panel"><p>First paragraph.</p><div class="block-child-p">Second x<sub>r</sub> ' + math + '</div></div>'
    capture.write_text(json.dumps(data))
    Path(str(capture) + '.html').write_text(card + ('' if sidecar else panel))
    if sidecar:
        folder = Path(str(capture) + '.readings')
        folder.mkdir()
        (folder / (hashlib.sha256(DOI.encode()).hexdigest() + '.json')).write_text(json.dumps(dict(
            schema='papertrack-reading-html-v1', doi=DOI, url=URL, card_html=card, panel_html=panel)))
    folder = Path(str(capture) + '.reading-failures')
    folder.mkdir()
    (folder / 'prior.json').write_text('{"prior_failure":true}')
    return capture


def bytes_tree(folder):
    return {str(p.relative_to(folder)): p.read_bytes() for p in folder.rglob('*') if p.is_file()}


@pytest.mark.parametrize('sidecar', [False, True])
def test_repairs_only_proven_omission_and_retains_evidence(tmp_path, sidecar):
    source = fixture(tmp_path, sidecar=sidecar)
    before = bytes_tree(source.parent)
    output = tmp_path / 'output'
    record = repair_cached_extract_capture(source, output, **ARGS)
    assert record['changed_dois'] == [DOI]
    assert bytes_tree(source.parent) == before
    result = json.loads((output / 'capture.json').read_text())
    assert 'Second x_{r} b...' in result['entries'][0]['abstract']
    assert result['entries'][0]['abstract_type'] == 'extract'
    assert result['entries'][0]['provenance'] == {'source': 'official'}
    for name, content in before.items():
        assert record['source_sha256'][name] == hashlib.sha256(content).hexdigest()
        if name != 'capture.json':
            assert (output / name).read_bytes() == content
    assert (output / 'extract-repair.json').is_file()


@pytest.mark.parametrize('mutation', ['different_text', 'abstract', 'errors', 'wrong_issue', 'broken_math'])
def test_rejects_unrelated_conflicts_and_invalid_source(tmp_path, mutation):
    source = fixture(tmp_path, math='<math><mover><mi></mi></mover></math>' if mutation == 'broken_math' else '<math><mi>x</mi></math>')
    data = json.loads(source.read_text())
    if mutation == 'different_text': data['entries'][0]['abstract'] = 'Other text'
    if mutation == 'abstract': data['entries'][0]['abstract_type'] = 'abstract'
    if mutation == 'errors': data['errors'] = [{'reason': 'incomplete'}]
    if mutation == 'wrong_issue': data['issue'] = '13'
    source.write_text(json.dumps(data))
    before = bytes_tree(source.parent)
    output = tmp_path / 'output'
    with pytest.raises(ValueError):
        repair_cached_extract_capture(source, output, **ARGS)
    assert not output.exists()
    assert not list(tmp_path.glob('.extract-repair-*'))
    assert bytes_tree(source.parent) == before


def test_dry_run_validates_without_publication(tmp_path):
    source = fixture(tmp_path)
    record = repair_cached_extract_capture(source, tmp_path / 'output', dry_run=True, **ARGS)
    assert record['validation']['reading_controls'] == 'passed'
    assert not (tmp_path / 'output').exists()
    assert not list(tmp_path.glob('.extract-repair-*'))


def test_existing_destination_cannot_be_overwritten(tmp_path):
    source = fixture(tmp_path)
    output = tmp_path / 'output'
    output.mkdir()
    with pytest.raises(FileExistsError):
        repair_cached_extract_capture(source, output, **ARGS)


def test_doi_urls_are_recorded_as_canonical_identifiers(tmp_path):
    source = fixture(tmp_path)
    data = json.loads(source.read_text())
    data['entries'][0]['doi'] = 'https://doi.org/' + DOI
    source.write_text(json.dumps(data))
    record = repair_cached_extract_capture(source, tmp_path / 'output', **ARGS)
    assert record['changed_dois'] == [DOI]


def test_nonempty_broken_extract_is_preserved_and_reusable_without_flag(tmp_path, caplog):
    from papertrack.journals.capture import read_browser_issue, validate_reading_controls
    source = fixture(tmp_path, math='<math><mover><mi>r</mi></mover></math>')
    output = tmp_path / 'output'
    repair_cached_extract_capture(source, output, **ARGS)
    capture = output / 'capture.json'
    data = json.loads(capture.read_text())
    validate_reading_controls(data, Path(str(capture) + '.html'), provider='acs')
    for render_math in (True, False, True):
        result = read_browser_issue(str(capture), render_math=render_math, **ARGS)
        assert result.entries[DOI].abstract.endswith(' r')
        assert result.entries[DOI].field_sources['abstract'] == 'official_browser_extract'
    assert 'Literal mathematical source retained' in caplog.text
    assert DOI in caplog.text

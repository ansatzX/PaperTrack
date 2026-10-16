from copy import deepcopy
import json

import pytest

from papertrack.browser_issue import read_browser_issue
from papertrack.journal_entry import JournalEntry
from papertrack.adapter_result import AdapterResult, SUCCESS


def snapshot():
    return {'schema': 'papertrack-browser-issue-v1', 'captured_at': '2026-09-11T04:00:00Z',
            'url': 'https://pubs.acs.org/jcisd8/issue/66/12', 'volume': '66', 'issue': '12',
            'headings': ['Volume 66, Issue 12'], 'card_count': 1,
            'entries': [{'doi': 'https://doi.org/10.1021/acs.jcim.5c02404', 'title': 'KNexPHENIX',
                         'authors': ['Suparno Nandi', 'Graeme L. Conn'],
                         'citation': 'J. Chem. Inf. Model. (2026) 66 (12): 6814–6820.',
                         'abstract': 'An abstract from the visible expanded issue card.', 'abstract_type': 'abstract'}]}


def load(tmp_path, data):
    path = tmp_path / 'capture.json'
    path.write_text(json.dumps(data))
    return read_browser_issue(str(path), provider='acs', code='jcisd8', volume='66', issue='12', journal_name='JCIM')


def test_capture_parses_official_placement_and_provenance(tmp_path):
    result = load(tmp_path, snapshot())
    entry = next(iter(result.entries.values()))
    assert result.ok
    assert entry.pages == '6814–6820'
    assert entry.field_sources['abstract'] == 'official_browser'
    assert result.metadata['transport'] == 'connected_browser'


@pytest.mark.parametrize('mutation', ['host', 'path', 'heading', 'inventory', 'duplicate', 'citation'])
def test_capture_rejects_wrong_or_incomplete_issue(tmp_path, mutation):
    data = snapshot()
    if mutation == 'host': data['url'] = data['url'].replace('pubs.acs.org', 'example.org')
    if mutation == 'path': data['url'] = data['url'].replace('/12', '/13')
    if mutation == 'heading': data['headings'] = ['Volume 66, Issue 13']
    if mutation == 'inventory': data['card_count'] = 2
    if mutation == 'duplicate':
        data['entries'] *= 2
        data['card_count'] = 2
    if mutation == 'citation': data['entries'][0]['citation'] = '(2026) 66 (13): 1–5.'
    with pytest.raises(ValueError): load(tmp_path, data)


def test_extract_fills_reading_abstract_with_original_source(tmp_path):
    data = snapshot()
    data['entries'][0]['abstract_type'] = 'extract'
    result = load(tmp_path, data)
    assert result.outcome == 'success'
    entry = next(iter(result.entries.values()))
    assert entry.abstract == data['entries'][0]['abstract']
    assert entry.field_sources['abstract'] == 'official_browser_extract'


def test_browser_snapshot_flows_through_report_preserving_checkbox(monkeypatch, tmp_path):
    from papertrack.report import filter_journal_to_md
    data = snapshot()
    path = tmp_path / 'capture.json'
    path.write_text(json.dumps(data))
    kwargs = dict(journal_name='JCIM', journal_slug='jcim', volume='66', issue='12', issn='1549-9596',
                  md_folder=str(tmp_path), acs_code='jcisd8', year=2026, browser_snapshot=str(path))
    assert filter_journal_to_md(**kwargs)
    report = tmp_path / 'jcim/66/12.md'
    first = report.read_text().replace('- [ ]', '- [x]', 1)
    report.write_text(first)
    assert filter_journal_to_md(**kwargs)
    text = report.read_text()
    assert '- [x]' in text
    assert 'official_browser' in text
    assert 'An abstract from the visible expanded issue card.' in text


def test_third_party_incomplete_rerun_keeps_prior_report(monkeypatch, tmp_path):
    from papertrack import report
    first = JournalEntry('10.1234/a', 'First', [], abstract='Existing abstract', volume='66', issue='12')
    second = JournalEntry('10.1234/b', 'Second', [], volume='66', issue='12')
    kwargs = dict(journal_name='JCIM', journal_slug='jcim', volume='66', issue='12', md_folder=str(tmp_path))
    report.write_journal_entries_to_md(**kwargs, entries={first.doi:first, second.doi:second})
    path = tmp_path / 'jcim/66/12.md'
    previous = path.read_text()
    monkeypatch.setattr(report, 'query_journal_issue_full_result', lambda **kw: AdapterResult(
        {first.doi:first}, SUCCESS, 'europe_pmc', 'journal', source_role='third_party_issue'))
    assert not report.filter_journal_to_md(**kwargs, issn='1549-9596')
    assert path.read_text() == previous


def test_acs_silverchair_parser_supports_new_issue_markup():
    from papertrack.acs_fetch import _parse_acs_toc_result
    html = '''<div class="al-article-items"><h3 class="item-title"><a>Paper</a></h3>
    <div class="al-authors-list"><span class="wi-fullname">Author One</span></div>
    <div class="ww-citation-primary">J. Chem. Inf. Model. (2026) 66 (12): 6814–6820.
    <span class="citation-label"><a href="https://doi.org/10.1021/acs.jcim.5c02404">DOI</a></span></div>
    <a class="showAbstractLink" data-abstract-type="abstract"></a>
    <div class="abstract-response-placeholder"><section class="abstract">Visible abstract.</section></div></div>'''
    result = _parse_acs_toc_result(html, 'JCIM')
    entry = next(iter(result.entries.values()))
    assert entry.abstract == 'Visible abstract.'
    assert entry.volume == '66' and entry.issue == '12'
    assert not result.critical_field_failures


def test_official_extract_does_not_trigger_pointless_enrichment(monkeypatch, tmp_path):
    from papertrack import metadata_sources
    from papertrack.journal_fetch import query_journal_issue_full_result
    data = snapshot()
    data['entries'][0]['abstract_type'] = 'extract'
    path = tmp_path / 'capture.json'
    path.write_text(json.dumps(data))
    monkeypatch.setattr(metadata_sources, 'europe_pmc_issue', lambda *args: pytest.fail('known extract queried'))
    result = query_journal_issue_full_result('1549-9596', '66', '12', 'JCIM', acs_code='jcisd8',
                                              browser_snapshot=str(path))
    assert result.ok
    assert result.metadata['abstract_unavailable_dois'] == []


def test_aip_capture_validates_article_link_issue_not_just_citation(tmp_path):
    data = snapshot()
    data.update(url='https://pubs.aip.org/aip/jcp/issue/164/16', volume='164', issue='16',
                headings=['Volume 164, Issue 16'])
    data['entries'][0].update(doi='https://doi.org/10.1063/5.0323573',
                              citation='J. Chem. Phys. 164, 164101 (2026)',
                              article_url='/aip/jcp/article/164/16/164101/3387571/RE-ADC')
    path = tmp_path / 'aip.json'
    path.write_text(json.dumps(data))
    kwargs = dict(provider='aip', code='jcp', volume='164', issue='16', journal_name='JCP')
    result = read_browser_issue(str(path), **kwargs)
    assert next(iter(result.entries.values())).pages == '164101'
    data['entries'][0]['article_url'] = '/aip/jcp/article/164/17/164101/3387571/RE-ADC'
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError): read_browser_issue(str(path), **kwargs)


def test_aps_capture_uses_issue_inventory_and_matching_doi_link(tmp_path):
    data = snapshot()
    data.update(url='https://journals.aps.org/prl/issues/136/1', volume='136', issue='1',
                headings=['Volume 136, Issue 1'])
    data['entries'][0].update(doi='10.1103/yf56-tfhd', citation='Phys. Rev. Lett. 136, 018101 (2026)',
                              article_url='/prl/abstract/10.1103/yf56-tfhd')
    path = tmp_path / 'aps.json'
    path.write_text(json.dumps(data))
    kwargs = dict(provider='aps', code='prl', volume='136', issue='1', journal_name='PRL')
    result = read_browser_issue(str(path), **kwargs)
    assert next(iter(result.entries.values())).pages == '018101'
    data['entries'][0]['article_url'] = '/prl/abstract/10.1103/wrong-doi'
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError): read_browser_issue(str(path), **kwargs)


def test_unloaded_extract_is_missing_not_unavailable(tmp_path):
    data = snapshot()
    data['entries'][0].update(abstract_type='extract', abstract='')
    result = load(tmp_path, data)
    assert result.outcome == 'partial'
    assert result.metadata['missing_abstract_dois'] == ['10.1021/acs.jcim.5c02404']
    assert result.metadata['abstract_unavailable_dois'] == []

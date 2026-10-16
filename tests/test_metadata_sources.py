from copy import deepcopy
import json

import pytest

from papertrack import metadata_sources as sources
from papertrack.journal_entry import JournalEntry
from papertrack.adapter_result import AdapterResult, SUCCESS, FETCH_FAILED


def entry(doi='10.1234/a', **kwargs):
    return JournalEntry(doi=doi, title=kwargs.pop('title', 'A paper'), authors=kwargs.pop('authors', []),
                        volume=kwargs.pop('volume', '66'), issue=kwargs.pop('issue', '12'), **kwargs)


def test_exact_doi_merge_preserves_official_and_excludes_other_issue():
    original = entry(doi='10.1234/A', title='Official title')
    sources.stamp(original, 'official')
    base = {original.doi: original}
    incoming = {str(i): e for i, e in enumerate([
        entry(doi='https://doi.org/10.1234/a', title='Other title', abstract='Complete abstract'),
        entry('10.1234/b'), entry('10.1234/c', issue='13'),
        entry('10.1234/d', title='Erratum: Wrong result')])}
    conflicts = sources.merge_entries(base, incoming, 'europe_pmc', allow_new=False, volume='66', issue='12')
    assert len(base) == 1
    assert original.title == 'Official title'
    assert original.abstract == 'Complete abstract'
    assert original.field_sources['abstract'] == 'europe_pmc'
    assert any(c['field'] == 'title' for c in conflicts)
    assert not incoming['0'].field_sources  # No mutation of source results.


def test_union_retains_complementary_members_and_deduplicates():
    base = {}
    sources.merge_entries(base, {'a': entry('10.1234/A')}, 'crossref', allow_new=True, volume='66', issue='12')
    sources.merge_entries(base, {'a': entry('10.1234/a', abstract='Text'), 'b': entry('10.1234/b')},
                          'europe_pmc', allow_new=True, volume='66', issue='12')
    assert set(base) == {'10.1234/a', '10.1234/b'}
    assert base['10.1234/a'].membership_sources == ['crossref', 'europe_pmc']


def test_short_openalex_fragment_falls_through_to_semantic_scholar(monkeypatch):
    calls = []
    abstract = ' '.join(f'word{i}' for i in range(50))
    def api(provider, url, payload=None):
        calls.append(provider)
        if provider == 'openalex':
            return {'results': [{'doi': 'https://doi.org/10.1234/a', 'abstract_inverted_index': {'tail': list(range(27))}}]}
        assert payload == {'ids': ['DOI:10.1234/a']}
        return [{'externalIds': {'DOI': '10.1234/A'}, 'abstract': abstract},
                {'externalIds': {'DOI': '10.1234/not-requested'}, 'abstract': abstract}]
    monkeypatch.setattr(sources, 'request_json', api)
    base = {'a': entry(), 'b': entry('10.1234/b', abstract='Official text')}
    sources.enrich_abstracts(base)
    assert calls == ['openalex', 'semantic_scholar']
    assert base['a'].abstract == abstract
    assert base['a'].field_sources['abstract'] == 'semantic_scholar'
    assert base['b'].abstract == 'Official text'
    assert len(base) == 2


@pytest.mark.parametrize('response', [[], None, 'bad', {'resultList': None}])
def test_malformed_europe_pmc_response_fails_soft(monkeypatch, response):
    monkeypatch.setattr(sources, 'request_json', lambda *args: response)
    assert sources.europe_pmc_issue('1549-9596', '66', '12', 'JCIM') == {}


def test_europe_pmc_rejects_wrong_issn_and_issue(monkeypatch):
    row = {'doi': '10.1234/a', 'title': 'Paper', 'abstractText': '<p>Abstract</p>',
           'journalInfo': {'volume': '66', 'issue': '12', 'journal': {'issn': '1549-9596'}}}
    wrong = deepcopy(row)
    wrong['doi'] = '10.1234/b'
    wrong['journalInfo']['issue'] = '13'
    monkeypatch.setattr(sources, 'request_json', lambda *args: {'resultList': {'result': [row, wrong]}})
    actual = sources.europe_pmc_issue('1549-9596', '66', '12', 'JCIM')
    assert list(actual) == ['10.1234/a']
    assert actual['10.1234/a'].abstract == 'Abstract'


def test_official_membership_is_enriched_without_source_mutation(monkeypatch):
    from papertrack import journal_fetch as fetch
    original = AdapterResult({'a': entry()}, SUCCESS, 'acs', 'journal')
    monkeypatch.setattr(sources, 'europe_pmc_issue', lambda *args: {'a': entry(abstract='Abstract'), 'b': entry('10.1234/b')})
    monkeypatch.setattr(fetch, '_crossref_issue_result', lambda *args: {})
    result = fetch._issue_fallback(original, provider='acs', journal_slug='jcim', journal_name='JCIM',
                                   issn='1549-9596', volume='66', issue='12', year=2026, run_fallback=True)
    assert list(result.entries) == ['a']
    assert result.entries['a'].abstract == 'Abstract'
    assert original.entries['a'].abstract == ''
    assert result.metadata['membership_authoritative']


def test_offline_rebuild_does_not_query_metadata(monkeypatch):
    from papertrack.journal_fetch import _issue_fallback
    monkeypatch.setattr(sources, 'europe_pmc_issue', lambda *args: pytest.fail('network in replay'))
    original = AdapterResult({}, FETCH_FAILED, 'acs', 'journal')
    assert _issue_fallback(original, provider='acs', journal_slug='jcim', journal_name='JCIM',
                           issn='1549-9596', volume='66', issue='12', year=2026, run_fallback=False) is original


def test_failed_provider_does_not_discard_official_inventory(monkeypatch):
    from papertrack import journal_fetch as fetch
    def broken(*args):
        raise ValueError('malformed provider payload')
    monkeypatch.setattr(sources, 'europe_pmc_issue', broken)
    monkeypatch.setattr(fetch, '_crossref_issue_result', lambda *args: {'a': entry(abstract='Crossref abstract')})
    official = AdapterResult({'a': entry()}, SUCCESS, 'acs', 'journal')
    result = fetch._issue_fallback(official, provider='acs', journal_slug='jcim', journal_name='JCIM',
                                   issn='1549-9596', volume='66', issue='12', year=2026, run_fallback=True)
    assert result.entries['a'].abstract == 'Crossref abstract'
    assert result.source_role == 'authoritative'


def test_lost_abstract_preserves_existing_report(monkeypatch, tmp_path):
    from papertrack import report
    first = entry(abstract='Prior abstract')
    kwargs = dict(journal_name='JCIM', journal_slug='jcim', volume='66', issue='12', md_folder=str(tmp_path))
    assert report.write_journal_entries_to_md(**kwargs, entries={first.doi:first})
    path = tmp_path / 'jcim/66/12.md'
    before = path.read_text()
    monkeypatch.setattr(report, 'query_journal_issue_full_result', lambda **kw: AdapterResult(
        {first.doi:entry()}, SUCCESS, 'europe_pmc', 'journal', source_role='third_party_issue'))
    assert not report.filter_journal_to_md(**kwargs, issn='1549-9596')
    assert path.read_text() == before

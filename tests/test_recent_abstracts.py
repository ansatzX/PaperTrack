import hashlib
import json
from pathlib import Path

import pytest

from papertrack.journals.capture import read_browser_issue, validate_reading_controls


def fixture(tmp_path):
    issue_url = 'https://pubs.acs.org/jcisd8/issue/66/12'
    recent_url = 'https://pubs.acs.org/jcisd8/latest-articles'
    doi = '10.1021/example'
    row = dict(doi=doi, title='Official issue title', authors=['Issue Author'],
               citation='JCIM (2026) 66 (12): 1–4.', abstract_type='unavailable',
               abstract='', unavailable_reason='No reading control on issue card')
    data = dict(schema='papertrack-browser-issue-v1', url=issue_url, recent_url=recent_url,
                volume='66', issue='12', headings=['Volume 66, Issue 12'],
                card_count=1, entries=[row])
    capture = tmp_path/'capture.json'
    capture.write_text(json.dumps(data))
    card = '<div class="al-article-item-wrap"><span class="citation-label"><a href="https://doi.org/10.1021/example">DOI</a></span>{}</div>'
    Path(str(capture)+'.html').write_text(card.format('')+f'<a href="{recent_url}">Latest Articles</a>')
    recent_row = dict(row, title='Different recent title', authors=['Different Author'],
                      abstract_type='abstract', abstract='The real corresponding Abstract.',
                      control='c', panel='p')
    page = dict(schema='papertrack-browser-recent-v1', url=recent_url,
                errors=[], remaining_abstracts=0, entries=[recent_row])
    pages = Path(str(capture)+'.recent-pages')
    pages.mkdir()
    page_path = pages/(hashlib.sha256(recent_url.encode()).hexdigest()+'.json')
    page_path.write_text(json.dumps(page))
    Path(str(page_path)+'.html').write_text(card.format('<a class="showAbstractLink" id="c" aria-controls="p" data-abstract-type="abstract">Abstract</a>')+'<div id="p"><section class="abstract">The real corresponding Abstract.</section></div>')
    manifest = dict(schema='papertrack-browser-recent-supplement-v1', issue_url=issue_url,
                    root_url=recent_url, complete=True, entries=[dict(doi=doi)],
                    pages=[dict(url=recent_url, next_url=None)])
    manifest_path = Path(str(capture)+'.recent.json')
    manifest_path.write_text(json.dumps(manifest))
    return data, capture, page_path, manifest_path


def load(capture):
    return read_browser_issue(str(capture), provider='acs', code='jcisd8',
                              volume='66', issue='12', journal_name='JCIM')


def test_recent_abstract_fills_exact_doi_without_replacing_issue_metadata(tmp_path):
    data, capture, _, _ = fixture(tmp_path)
    result = load(capture)
    assert set(result.entries) == {'10.1021/example'}
    entry = result.entries['10.1021/example']
    assert entry.title == 'Official issue title'
    assert entry.authors == ['Issue Author']
    assert entry.abstract == 'The real corresponding Abstract.'
    assert entry.field_sources['abstract'] == 'official_browser_recent'
    assert result.metadata['reading_coverage']['abstract'] == 1
    assert not result.metadata['abstract_unavailable_dois']
    validate_reading_controls(data, Path(str(capture)+'.html'), provider='acs')


def test_recent_requires_source_abstract_not_graphical_caption(tmp_path):
    _, capture, page_path, _ = fixture(tmp_path)
    html = Path(str(page_path)+'.html')
    html.write_text(html.read_text().replace('class="abstract"', 'class="abstract graphicalAbstract"'))
    with pytest.raises(ValueError, match='source panel has no reading text'):
        load(capture)


def test_recent_unresolved_or_unobserved_pages_fail(tmp_path):
    _, capture, _, manifest_path = fixture(tmp_path)
    manifest = json.loads(manifest_path.read_text())
    manifest['complete'] = False
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='Incomplete'):
        load(capture)
    manifest['complete'] = True
    manifest['pages'][0]['url'] += '?page=99'
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='observed'):
        load(capture)


def test_recent_other_doi_cannot_become_issue_member(tmp_path):
    _, capture, _, manifest_path = fixture(tmp_path)
    manifest = json.loads(manifest_path.read_text())
    manifest['entries'].append({'doi':'10.1021/not-in-issue'})
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='DOI index mismatch'):
        load(capture)


def test_recent_pages_follow_observed_next_link_and_fill_two_exact_members(tmp_path):
    data, capture, first_path, manifest_path = fixture(tmp_path)
    other = dict(data['entries'][0], doi='10.1021/second', title='Second issue title')
    data['entries'].append(other)
    data['card_count'] = 2
    capture.write_text(json.dumps(data))
    issue_html = Path(str(capture)+'.html')
    issue_html.write_text(issue_html.read_text()+'<div class="al-article-item-wrap"><span class="citation-label"><a href="https://doi.org/10.1021/second">DOI</a></span></div>')
    second_url = data['recent_url']+'?page=2'
    first_html = Path(str(first_path)+'.html')
    original_html = first_html.read_text()
    first_html.write_text(original_html+f'<a rel="next" href="{second_url}">Next</a>')
    second_path = first_path.parent/(hashlib.sha256(second_url.encode()).hexdigest()+'.json')
    page = json.loads(first_path.read_text())
    page['url'] = second_url
    page['entries'][0]['doi'] = '10.1021/second'
    second_path.write_text(json.dumps(page))
    Path(str(second_path)+'.html').write_text(original_html.replace('10.1021/example', '10.1021/second'))
    manifest = json.loads(manifest_path.read_text())
    manifest['pages'][0]['next_url'] = second_url
    manifest['pages'].append(dict(url=second_url, next_url=None))
    manifest['entries'].append({'doi':'10.1021/second'})
    manifest_path.write_text(json.dumps(manifest))
    result = load(capture)
    assert set(result.entries) == {'10.1021/example', '10.1021/second'}
    assert result.entries['10.1021/second'].title == 'Second issue title'
    first_html.write_text(original_html)  # A forged next-page index is insufficient.
    with pytest.raises(ValueError, match='pagination conflicts'):
        load(capture)


def test_unobserved_recent_link_error_prevents_fixed_capture_import(tmp_path):
    data, capture, _, _ = fixture(tmp_path)
    data.update(capture_method='fixed_browser', inventory_complete=True, recent_url=None,
                errors=[dict(reason='recent_link_unavailable', stage='recent_discovery',
                             target_dois=['10.1021/example'], observed_recent_url=None,
                             expected_recent_path='/jcisd8/latest-articles', url=data['url'])])
    capture.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='incomplete or has unresolved errors'):
        load(capture)


def test_recent_nonempty_broken_math_uses_same_literal_policy(tmp_path, caplog):
    data, capture, page_path, _ = fixture(tmp_path)
    page = json.loads(page_path.read_text())
    page['entries'][0]['abstract'] = 'Before r after.'
    page_path.write_text(json.dumps(page))
    html = Path(str(page_path) + '.html')
    html.write_text(html.read_text().replace('The real corresponding Abstract.',
        'Before <math><mover><mi>r</mi></mover></math> after.'))
    validate_reading_controls(data, Path(str(capture) + '.html'), provider='acs')
    assert load(capture).entries['10.1021/example'].abstract == 'Before r after.'
    assert '10.1021/example' in caplog.text

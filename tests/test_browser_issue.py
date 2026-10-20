import json
from pathlib import Path

import pytest

from papertrack.journals.capture import read_browser_issue


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


def test_aps_partial_source_cannot_be_imported_as_complete(tmp_path):
    from papertrack.journals.capture import validate_reading_controls

    data = dict(volume='137', issue='12', entries=[dict(doi='10.1103/example')])
    html = tmp_path / 'issue.html'
    html.write_text('''<main><h1><span>Volume 137, Issue 12 (partial)</span></h1>
    <section class="headline"><div class="title">
    <a href="/prl/abstract/10.1103/example">Paper</a></div></section></main>''')
    with pytest.raises(ValueError, match='Publisher marks this issue as partial'):
        validate_reading_controls(data, html, provider='aps')


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
    assert result.metadata['reading_coverage'] == {
        'abstract': 0, 'extract': 1, 'missing': 0, 'excluded': 0}


def test_mixed_coverage_excludes_non_articles_and_counts_missing_text(tmp_path):
    data = snapshot()
    original = data['entries'][0]
    data['entries'] = [
        original,
        dict(original, doi='10.1021/extract', abstract_type='extract'),
        dict(original, doi='10.1021/empty', abstract='   '),
        dict(original, doi='10.1021/masthead', title='Editorial Masthead',
             abstract_type='unavailable', abstract='', unavailable_reason='No control'),
    ]
    data['card_count'] = 4
    result = load(tmp_path, data)
    assert result.outcome == 'partial'
    assert result.metadata['reading_coverage'] == {
        'abstract': 1, 'extract': 1, 'missing': 1, 'excluded': 1}
    assert result.metadata['missing_abstract_dois'] == ['10.1021/empty']


def test_browser_snapshot_flows_through_report_preserving_checkbox(monkeypatch, tmp_path):
    from papertrack.reports.render import filter_journal_to_md
    data = snapshot()
    data['entries'][0].update(control='control', panel='panel')
    path = tmp_path / 'capture.json'
    path.write_text(json.dumps(data))
    html = tmp_path / 'capture.json.html'
    html.write_text('''<div class="al-article-item-wrap">
    <span class="citation-label"><a href="https://doi.org/10.1021/acs.jcim.5c02404">DOI</a></span>
    <a class="showAbstractLink" id="control" data-abstract-type="abstract" aria-controls="panel">Abstract</a>
    </div><div id="panel"><section class="abstract">An abstract from the visible expanded issue card.</section></div>''')
    kwargs = dict(journal_name='JCIM', journal_slug='jcim', volume='66', issue='12', issn='1549-9596',
                  md_folder=str(tmp_path), acs_code='jcisd8', year=2026, browser_snapshot=str(path))
    assert filter_journal_to_md(**kwargs)
    report = tmp_path / 'jcim/66/12.md'
    first = report.read_text().replace('- [ ]', '- [x]', 1)
    report.write_text(first)
    assert filter_journal_to_md(**kwargs)
    text = report.read_text()
    assert '- [x]' in text
    assert '> [!quote]- Abstract' in text
    assert 'papertrack_provenance' not in text
    assert 'An abstract from the visible expanded issue card.' in text
    data['entries'][0]['abstract'] = 'A figure caption substituted for the abstract'
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='Abstract text conflicts'):
        filter_journal_to_md(**kwargs)
    assert report.read_text() == text


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


@pytest.mark.parametrize('doi', ['10.1103/PhysRevLett.129', '10.1103/physrevlett.129.123'])
def test_capture_rejects_truncated_prl_doi(tmp_path, doi):
    data = snapshot()
    data.update(url='https://journals.aps.org/prl/issues/129/27', volume='129', issue='27',
                headings=['Volume 129, Issue 27'])
    data['entries'][0].update(doi=doi, citation='Phys. Rev. Lett. 129, 270502 (2022)',
                              article_url='/prl/abstract/' + doi)
    path = tmp_path / 'aps.json'
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='PRL DOI'):
        read_browser_issue(str(path), provider='aps', code='prl', volume='129', issue='27', journal_name='PRL')


@pytest.mark.parametrize('kind,label', [('abstract', 'Abstract'),
                                        ('extract', 'Extract（原文摘录）')])
def test_validated_source_type_reaches_markdown_template(tmp_path, kind, label):
    from papertrack.reports.render import _render_journal_paper

    data = snapshot()
    data['entries'][0]['abstract_type'] = kind
    entry = next(iter(load(tmp_path, data).entries.values()))
    rendered = _render_journal_paper(entry)
    assert f'> [!quote]- {label}\n> ' in rendered
    assert f'{label}:\n' not in rendered
    assert f'> {entry.abstract}' in rendered
    if kind == 'extract':
        assert '\nAbstract:' not in rendered
        assert '[!quote]- Abstract' not in rendered


def test_html_evidence_rejects_extract_when_abstract_is_offered(tmp_path):
    from papertrack.journals.capture import validate_reading_controls

    data = snapshot()
    row = data['entries'][0]
    row.update(abstract_type='extract', control='extract', panel='extract-panel')
    html = tmp_path / 'issue.html'
    html.write_text('''<div class="al-article-item-wrap">
    <span class="citation-label"><a href="https://doi.org/10.1021/acs.jcim.5c02404">DOI</a></span>
    <a class="showAbstractLink" id="extract" data-abstract-type="extract" aria-controls="extract-panel">Extract</a>
    <a class="showAbstractLink" id="abstract" data-abstract-type="abstract" aria-controls="abstract-panel">Abstract</a>
    </div><div id="extract-panel">Correction</div><div id="abstract-panel"><section class="abstract">Original abstract β</section></div>''')
    with pytest.raises(ValueError, match='type conflicts'):
        validate_reading_controls(data, html, provider='acs')
    row.update(abstract_type='abstract', control='abstract', panel='abstract-panel', abstract='Original abstract 𝛽')
    validate_reading_controls(data, html, provider='acs')
    row['abstract'] = 'A substituted figure caption'
    with pytest.raises(ValueError, match='Abstract text conflicts'):
        validate_reading_controls(data, html, provider='acs')
    row['panel'] = 'extract-panel'
    with pytest.raises(ValueError, match='control/panel conflicts'):
        validate_reading_controls(data, html, provider='acs')


@pytest.mark.parametrize('kind,reason', [('', ''), ('unavailable', '')])
def test_legacy_capture_cannot_hide_missing_abstract_evidence(tmp_path, kind, reason):
    data = snapshot()
    data['entries'][0].update(abstract='', abstract_type=kind, unavailable_reason=reason)
    with pytest.raises(ValueError, match='availability|observed evidence'):
        load(tmp_path, data)


def test_extract_text_requires_matching_source_paragraphs(tmp_path):
    from papertrack.journals.capture import validate_reading_controls

    data = snapshot()
    data['entries'][0].update(abstract_type='extract', control='e', panel='p',
                              abstract='Correction: x_{1} is 2.')
    html = tmp_path / 'issue.html'
    html.write_text('''<div class="al-article-item-wrap">
    <span class="citation-label"><a href="https://doi.org/10.1021/acs.jcim.5c02404">DOI</a></span>
    <a class="showAbstractLink" id="e" data-abstract-type="extract" aria-controls="p">Extract</a>
    </div><div id="p"><p>Correction: x<sub>1</sub> is 2.</p></div>''')
    validate_reading_controls(data, html, provider='acs')
    data['entries'][0]['abstract'] = 'An invented summary'
    with pytest.raises(ValueError, match='Extract text conflicts'):
        validate_reading_controls(data, html, provider='acs')


@pytest.mark.parametrize('source,captured,reason', [
    ('', '', 'source panel has no reading text'),
    ('', 'Unverified text', 'source panel has no reading text'),
    ('Official abstract.', '', 'capture has no reading text'),
])
def test_missing_reading_text_is_not_reported_as_text_conflict(tmp_path, source, captured, reason):
    from papertrack.journals.capture import validate_reading_controls

    data = snapshot()
    data['entries'][0].update(abstract_type='abstract', control='a', panel='p', abstract=captured)
    html = tmp_path / 'issue.html'
    html.write_text('''<div class="al-article-item-wrap">
    <span class="citation-label"><a href="https://doi.org/10.1021/acs.jcim.5c02404">DOI</a></span>
    <a class="showAbstractLink" id="a" data-abstract-type="abstract" aria-controls="p">Abstract</a>
    </div><div id="p"><section class="abstract">''' + source + '</section></div>')
    with pytest.raises(ValueError, match=reason):
        validate_reading_controls(data, html, provider='acs')


def test_aps_detail_html_is_required_and_bound_to_doi_and_text(tmp_path):
    import hashlib
    from papertrack.journals.capture import validate_reading_controls

    data = {'entries': [{'doi': '10.1103/example', 'title': 'Paper',
                         'abstract_type': 'abstract', 'abstract': 'An actual abstract.'}]}
    html = tmp_path / 'capture.json.html'
    with pytest.raises(ValueError, match='evidence is missing'):
        validate_reading_controls(data, html, provider='aps')
    html.write_text('<section class="headline"><div class="title">'
                    '<a href="/prl/abstract/10.1103/example">Paper</a></div></section>')
    folder = tmp_path / 'capture.json.articles'
    folder.mkdir()
    detail = folder / (hashlib.sha256(b'10.1103/example').hexdigest() + '.html')
    detail.write_text('<main><p>DOI: https://doi.org/10.1103/example</p>'
                      '<div id="abstract-section-content"><p>An actual abstract.</p>'
                      '<div>Close Prev Next View figure Physics Subject Headings</div></div></main>')
    validate_reading_controls(data, html, provider='aps')
    data['entries'][0]['abstract'] = 'A figure caption'
    with pytest.raises(ValueError, match='identity/text conflicts'):
        validate_reading_controls(data, html, provider='aps')
    data['entries'][0]['abstract'] = 'An actual abstract.'
    detail.write_text(detail.read_text().replace('10.1103/example', '10.1103/another'))
    with pytest.raises(ValueError, match='identity/text conflicts'):
        validate_reading_controls(data, html, provider='aps')


def test_reading_comparison_composes_accents_after_dom_whitespace():
    from papertrack.journals.capture import _comparable_reading_text as comparable
    # Real JCP caches include decomposed math accents separated by browser text
    # whitespace; canonical equivalence must not hide genuinely different math.
    assert comparable('A \u0303 and B \u0303') == comparable('Ã and B\u0303')
    assert comparable('6.4 A \u030a ps−1') == comparable('6.4 Å ps−1')
    assert comparable('⟨S \u0302 2⟩') == comparable('⟨Ŝ2⟩')
    assert comparable('A \u0303') != comparable('A')
    assert comparable('A \u0303') != comparable('A \u0302')
    assert comparable('S2') != comparable('S3')


def test_formal_erratum_is_retained_with_extract_provenance(tmp_path):
    data = snapshot()
    data['entries'][0].update(title='Erratum: Earlier article', abstract_type='extract',
                              abstract='The published equation contains a typographical error.')
    result = load(tmp_path, data)
    assert len(result.entries) == 1
    entry = next(iter(result.entries.values()))
    assert entry.field_sources['abstract'] == 'official_browser_extract'
    from papertrack.reports.render import _render_journal_paper
    rendered = _render_journal_paper(entry)
    assert '> [!quote]- Extract（原文摘录）' in rendered
    assert '> [!quote]- Abstract' not in rendered


def test_retained_reading_panel_survives_issue_dom_eviction(tmp_path):
    import hashlib
    from papertrack.journals.capture import validate_reading_controls
    data = snapshot()
    row = data['entries'][0]
    row.update(control='control', panel='panel')
    capture = tmp_path/'capture.json'
    capture.write_text(json.dumps(data))
    html = Path(str(capture)+'.html')
    card = '''<div class="al-article-item-wrap"><span class="citation-label">
    <a href="https://doi.org/10.1021/acs.jcim.5c02404">DOI</a></span>
    <a class="showAbstractLink" id="control" data-abstract-type="abstract" aria-controls="panel">Abstract</a></div>'''
    html.write_text(card+'<div id="panel"></div>')
    with pytest.raises(ValueError, match='source panel has no reading text'):
        validate_reading_controls(data, html, provider='acs')
    folder = Path(str(capture)+'.readings')
    folder.mkdir()
    from papertrack.journals.metadata import normalize_doi
    evidence = folder/(hashlib.sha256(normalize_doi(row['doi']).encode()).hexdigest()+'.json')
    saved = dict(schema='papertrack-reading-html-v1', url=data['url'], doi=row['doi'],
                 card_html=card, panel_html=f'<div id="panel"><section class="abstract">{row["abstract"]}</section></div>')
    evidence.write_text(json.dumps(saved))
    validate_reading_controls(data, html, provider='acs')
    parsed = read_browser_issue(str(capture), provider='acs', code='jcisd8', volume='66', issue='12', journal_name='JCIM')
    assert next(iter(parsed.entries.values())).abstract == row['abstract']
    # Diagnostic HTML must never qualify even if copied to the normal path.
    saved['schema'] = 'papertrack-failed-reading-html-v1'
    evidence.write_text(json.dumps(saved))
    with pytest.raises(ValueError, match='identity mismatch'):
        validate_reading_controls(data, html, provider='acs')
    saved['schema'] = 'papertrack-reading-html-v1'
    saved['doi'] = '10.1021/other'
    evidence.write_text(json.dumps(saved))
    with pytest.raises(ValueError, match='identity mismatch'):
        validate_reading_controls(data, html, provider='acs')


def test_issue_html_inventory_rejects_a_consistent_but_incomplete_capture(tmp_path):
    from papertrack.journals.capture import validate_reading_controls
    data = snapshot()
    row = data['entries'][0]
    row.update(control='c', panel='p')
    html = tmp_path/'capture.json.html'
    html.write_text('''<div class="al-article-item-wrap"><span class="citation-label">
    <a href="https://doi.org/10.1021/acs.jcim.5c02404">DOI</a></span>
    <a class="showAbstractLink" id="c" aria-controls="p">Abstract</a></div>
    <div id="p"><section class="abstract">An abstract from the visible expanded issue card.</section></div>
    <div class="al-article-item-wrap"><span class="citation-label">
    <a href="https://doi.org/10.1021/omitted">DOI</a></span></div>''')
    # JSON's own card_count and entries agree; only reverse HTML membership finds it.
    with pytest.raises(ValueError, match='missing from capture=.*10.1021/omitted'):
        validate_reading_controls(data, html, provider='acs')


def test_aps_inventory_deduplicates_highlights_but_rejects_extra_capture(tmp_path):
    from bs4 import BeautifulSoup
    from papertrack.journals.capture import _validate_issue_inventory
    soup = BeautifulSoup('''<section class="headline"><div class="title">
    <a href="/prl/abstract/10.1103/a">Paper</a>
    <a href="/prl/abstract/10.1103/a">Highlighted copy</a></div></section>''', 'lxml')
    data = dict(entries=[dict(doi='10.1103/a')])
    _validate_issue_inventory(data, soup, 'aps')
    data['entries'].append(dict(doi='10.1103/not-in-this-issue'))
    with pytest.raises(ValueError, match='not in issue HTML=.*not-in-this-issue'):
        _validate_issue_inventory(data, soup, 'aps')

@pytest.mark.parametrize('article_type', ['erratum', 'comment', 'reply', 'retraction'])
@pytest.mark.parametrize('mutation', ['', 'wrong_doi', 'ordinary_article', 'empty_body', 'abstract_control', 'abstract_section'])
def test_aps_unavailable_requires_matching_readable_article(tmp_path, mutation, article_type):
    import hashlib
    from papertrack.journals.capture import _validate_aps_abstracts
    folder = tmp_path / 'articles'
    folder.mkdir()
    doi = '10.1103/example'
    html = '<main><p>DOI: 10.1103/example</p><div id="article-page-wrapper" data-article-type="erratum"><div id="fulltext-content">Official correction text.</div></div></main>'
    if mutation == 'wrong_doi':
        html = html.replace(doi, '10.1103/other')
    elif mutation == 'ordinary_article':
        html = html.replace('data-article-type="erratum"', 'data-article-type="article"')
    elif mutation == 'empty_body':
        html = html.replace('Official correction text.', '')
    elif mutation == 'abstract_control':
        html += '<a id="section-abstract">Abstract</a>'
    elif mutation == 'abstract_section':
        html += '<div id="abstract-section-content"><p>Actual abstract.</p></div>'
    html = html.replace('data-article-type="erratum"', f'data-article-type="{article_type}"')
    (folder / (hashlib.sha256(doi.encode()).hexdigest() + '.html')).write_text(html)
    data = {'entries': [dict(doi=doi, title='Erratum', abstract='', abstract_type='unavailable', unavailable_reason='Observed no abstract on readable erratum')]}
    if mutation:
        with pytest.raises(ValueError, match='conflicts with HTML'):
            _validate_aps_abstracts(data, folder)
    else:
        _validate_aps_abstracts(data, folder)

@pytest.mark.parametrize('unavailable', [False, True])
def test_aps_complete_source_title_restores_math_even_without_abstract(tmp_path, unavailable):
    import hashlib
    from html import escape
    data = snapshot()
    doi = '10.1103/example'
    data.update(url='https://journals.aps.org/prl/issues/137/14',volume='137',issue='14',headings=['Volume 137, Issue 14'])
    data['entries'][0].update(doi=doi,title='Dependence of -meson',citation='Phys. Rev. Lett. 137, 149901 (2026)',article_url='/prl/abstract/'+doi)
    if unavailable:
        data['entries'][0].update(abstract='',abstract_type='unavailable',unavailable_reason='Observed source absence')
    p=tmp_path/'capture.json';p.write_text(json.dumps(data))
    folder=Path(str(p)+'.articles');folder.mkdir()
    detail=folder/(hashlib.sha256(doi.encode()).hexdigest()+'.html')
    title='Dependence of <math><mi>ρ</mi></math>-meson'
    html='<meta name="citation_title" content="'+escape(title,quote=True)+'"><main><p>DOI: '+doi+'</p></main>'
    detail.write_text(html)
    kwargs=dict(provider='aps',code='prl',volume='137',issue='14',journal_name='PRL')
    result=read_browser_issue(str(p),**kwargs)
    assert result.entries[doi].title == 'Dependence of $ρ$-meson'
    detail.write_text(html.replace('DOI: '+doi,'DOI: 10.1103/other'))
    with pytest.raises(ValueError,match='title identity'):
        read_browser_issue(str(p),**kwargs)


def test_saved_issue_math_restores_title_and_author_by_exact_doi(tmp_path):
    data=snapshot()
    data['entries'][0].update(title='CO 2 paper',authors=['Gonza n ~ ez'])
    p=tmp_path/'capture.json';p.write_text(json.dumps(data))
    Path(str(p)+'.html').write_text('<div class="al-article-item-wrap"><span class="citation-label"><a href="https://doi.org/10.1021/acs.jcim.5c02404">DOI</a></span><h3 class="item-title"><a>CO<sub>2</sub> paper</a></h3><span class="wi-fullname">Gonza<math><mover accent="true"><mi>n</mi><mo>~</mo></mover></math>ez</span></div>')
    entry=read_browser_issue(str(p),provider='acs',code='jcisd8',volume='66',issue='12',journal_name='JCIM').entries['10.1021/acs.jcim.5c02404']
    assert entry.title=='CO$_{2}$ paper'
    assert entry.authors==['Gonzañez']


def test_real_jcp_extract_block_keeps_prose_and_does_not_hide_invalid_math():
    from bs4 import BeautifulSoup
    from papertrack.journals.capture import _silverchair_reading_nodes
    from papertrack.reports.reading import reading_markdown
    panel = BeautifulSoup((Path(__file__).parent/'fixtures/jcp_extract_block.html').read_text(), 'lxml')
    nodes, separator = _silverchair_reading_nodes(panel, 'extract')
    assert len(nodes) == 1
    text = separator.join(node.get_text() for node in nodes)
    assert text.startswith('The authors would like to point out an error in Eq. (54)')
    assert 'effective dynamic kernel matrix elements is' in text
    assert 'Ξ' in text and text.endswith('j...')
    assert _silverchair_reading_nodes(panel, 'abstract')[0] == []
    # The retained publisher teaser also has incomplete mathematics; selection
    # repair must not flatten or invent the missing accent/fraction denominator.
    with pytest.raises(ValueError, match='Unsupported MathML'):
        reading_markdown(nodes[0])


def test_extract_outermost_blocks_are_selected_once_and_preserve_formula_prose():
    from bs4 import BeautifulSoup
    from papertrack.journals.capture import _silverchair_reading_nodes
    from papertrack.reports.reading import reading_markdown
    soup = BeautifulSoup('<aside><p>unrelated</p></aside><div id="panel">'
                         '<div class="block-child-p">Before <p>inner <math><msup><mi>x</mi><mn>2</mn></msup></math></p> after'
                         '<div class="block-child-p"> nested</div></div><p>Last</p></div>', 'lxml')
    nodes, separator = _silverchair_reading_nodes(soup.find(id='panel'), 'extract')
    text = separator.join(reading_markdown(node) for node in nodes)
    assert len(nodes) == 2
    assert text == 'Before inner $x^{2}$ after nested\n\nLast'
    assert 'unrelated' not in text


@pytest.mark.parametrize('marker,blocked', [
    ('<option selected value="/jcisd8/issue/66/12">Volume 66, Issue 12 (In Progress)</option>', True),
    ('<option selected value="/jcisd8/issue/66/12">Volume 66, Issue 12 (partial)</option>', True),
    ('<option selected>In Progress</option>', False),
    ('<option value="/jcisd8/issue/66/13">Volume 66, Issue 13 (In Progress)</option>', False),
    ('<option selected value="/jcisd8/issue/66/13">Volume 66, Issue 13 (In Progress)</option>', False),
    ('<option selected value="/jcisd8/issue/66/12">Future date - Volume 66, Issue 12</option>', False),
    ('<h2>Volume 66, Issue 12 (In Progress)</h2>', True),
    ('<h2>Volume 66, Issue 13 (In Progress)</h2>', False),
])
def test_current_issue_completion_markers_are_identity_bound(tmp_path, marker, blocked):
    from bs4 import BeautifulSoup
    from papertrack.journals.capture import _assert_complete_issue_source
    data = snapshot()
    if blocked:
        with pytest.raises(ValueError, match='Publisher marks current issue as incomplete'):
            _assert_complete_issue_source(data, BeautifulSoup(marker, 'lxml'))
        path = tmp_path / 'capture.json'
        path.write_text(json.dumps(data))
        Path(str(path) + '.html').write_text(marker)
        with pytest.raises(ValueError, match='Publisher marks current issue as incomplete'):
            read_browser_issue(str(path), provider='acs', code='jcisd8', volume='66', issue='12', journal_name='JCIM')
    else:
        _assert_complete_issue_source(data, BeautifulSoup(marker, 'lxml'))

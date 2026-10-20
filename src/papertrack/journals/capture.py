"""Validate a DOM capture from the connected-browser issue collector."""
from __future__ import annotations

import json
import hashlib
from pathlib import Path
import re
import unicodedata
from urllib.parse import urlsplit

from papertrack.adapter_result import AdapterResult, SUCCESS, PARTIAL
from papertrack.journals.entry import JournalEntry
from papertrack.journals.metadata import normalize_doi, stamp


def _comparable_reading_text(text: str) -> str:
    # Rendered math can separate a base glyph and combining accent with DOM
    # whitespace. Removing that whitespace creates a new composable sequence;
    # normalize again so it compares equally with precomposed HTML glyphs.
    compact = re.sub(r'\s+', '', unicodedata.normalize('NFKC', text))
    return unicodedata.normalize('NFKC', compact)


def _reading_panel(row, data, html_path, issue_html):
    """Prefer a DOI-bound raw panel retained while open over an evicted panel."""
    from bs4 import BeautifulSoup

    doi = normalize_doi(row['doi'])
    folder = Path(str(html_path).removesuffix('.html') + '.readings')
    evidence = folder / (hashlib.sha256(doi.encode()).hexdigest() + '.json')
    if not evidence.is_file():
        return issue_html.find(id=row.get('panel')) if row.get('panel') else None
    saved = json.loads(evidence.read_text(encoding='utf-8'))
    if (saved.get('schema') != 'papertrack-reading-html-v1'
            or saved.get('url') != data.get('url')
            or normalize_doi(saved.get('doi', '')) != doi
            or not isinstance(saved.get('card_html'), str)
            or not isinstance(saved.get('panel_html'), str)):
        raise ValueError(f'Reading HTML evidence identity mismatch: {doi}')
    card = BeautifulSoup(saved['card_html'], 'lxml')
    link = card.select_one('.citation-label a')
    controls = card.select('.showAbstractLink')
    if (not link or normalize_doi(link.get('href', '')) != doi
            or not any(c.get('id') == row.get('control')
                       and c.get('aria-controls') == row.get('panel') for c in controls)):
        raise ValueError(f'Reading HTML evidence control mismatch: {doi}')
    panel = BeautifulSoup(saved['panel_html'], 'lxml').find(id=row.get('panel'))
    if panel is None:
        raise ValueError(f'Reading HTML evidence panel missing: {doi}')
    return panel



def _silverchair_reading_nodes(panel, kind):
    """Select the same publisher reading nodes for validation and rendering."""
    if kind == 'abstract':
        sections = panel.select('section.abstract[aria-label="Main abstract"]')
        return (sections or panel.select('section.abstract:not(.graphicalAbstract)')), ' '
    if kind == 'extract':
        blocks = panel.select('p, div.block-child-p')
        identities = {id(block) for block in blocks}
        return [block for block in blocks
                if not any(id(parent) in identities for parent in block.parents)], '\n\n'
    return [], ''


def _render_silverchair_reading(row, data, html_path, issue_html, *, render_math=True):
    from papertrack.reports.reading import reading_content

    panel = _reading_panel(row, data, html_path, issue_html)
    if panel is None:
        raise ValueError(f'Reading source panel missing: {normalize_doi(row["doi"])}')
    nodes, separator = _silverchair_reading_nodes(panel, row.get('abstract_type'))
    sidecar = Path(str(html_path).removesuffix('.html') + '.readings') / (
        hashlib.sha256(normalize_doi(row['doi']).encode()).hexdigest() + '.json')
    evidence_path = sidecar if sidecar.is_file() else html_path
    try:
        return separator.join(reading_content(node, render_math=render_math, doi=normalize_doi(row['doi']), source=str(evidence_path)).strip() for node in nodes).strip()
    except ValueError as error:
        raise ValueError(f'Reading source cannot be rendered: {normalize_doi(row["doi"])}: {error}') from error


def _assert_complete_issue_source(data, soup):
    """Reject only completion markers bound to this observed issue identity."""
    if data.get('schema') == 'papertrack-browser-recent-v1':
        return
    url = urlsplit(data.get('url', ''))
    identity = re.fullmatch(r'/(?:aip/)?([^/]+)/issues?/(\d+)/(\d+)/?', url.path)
    if not identity or identity.groups()[1:] != (str(data.get('volume')), str(data.get('issue'))):
        return
    marker = re.compile(r'\b(?:in progress|partial)\b', re.I)
    for option in soup.select('option[selected], option.selected'):
        from urllib.parse import urljoin
        value = option.get('value', '')
        if not value:
            continue
        target = urlsplit(urljoin(data['url'], value))
        candidate = re.fullmatch(r'/(?:aip/)?([^/]+)/issues?/(\d+)/(\d+)/?', target.path)
        if (target.hostname == url.hostname and candidate and candidate.groups() == identity.groups()
                and marker.search(option.get_text(' ', strip=True))):
            raise ValueError('Publisher marks current issue as incomplete: ' + option.get_text(' ', strip=True))
    label = re.compile(r'\bVolume\s+' + re.escape(identity[2]) + r'\s*,?\s*Issue\s+'
                       + re.escape(identity[3]) + r'(?!\d)', re.I)
    for heading in soup.select('h1,h2,.volume-issue__wrap'):
        text = heading.get_text(' ', strip=True)
        if label.search(text) and marker.search(text):
            raise ValueError('Publisher marks current issue as incomplete: ' + text)


def _validate_issue_inventory(data, soup, provider):
    """A legal subset of papers is not a complete formal issue capture."""
    if data.get('schema') == 'papertrack-browser-recent-v1':
        # Recent captures intentionally contain only target issue DOIs; their
        # independent source check must not turn recent-only DOIs into members.
        return
    if provider != 'aps':
        _assert_complete_issue_source(data, soup)
    captured = [normalize_doi(row.get('doi', '')) for row in data['entries']]
    if not captured or '' in captured or len(captured) != len(set(captured)):
        raise ValueError('Invalid or duplicate captured issue DOI inventory')
    if provider == 'aps':
        partial_heading = f"Volume {data.get('volume')}, Issue {data.get('issue')} (partial)"
        if any(h.get_text(' ', strip=True) == partial_heading for h in soup.select('h1,h2')):
            raise ValueError('Publisher marks this issue as partial; completeness is unproven')
        observed = {normalize_doi(a.get('href', '').split('/abstract/', 1)[-1])
                    for a in soup.select('section.headline .title a[href*="/abstract/10."]')}
    else:
        cards = soup.select('.al-article-item-wrap') or soup.select('.al-article-items')
        observed = {normalize_doi(a.get('href', '')) for card in cards
                    for a in card.select('.citation-label a')}
    observed.discard('')
    missing, extra = observed - set(captured), set(captured) - observed
    if not observed or missing or extra:
        raise ValueError('Issue HTML DOI inventory mismatch: '
                         f'missing from capture={sorted(missing)}; '
                         f'not in issue HTML={sorted(extra)}')


def validate_reading_controls(data: dict, html_path: Path, *, provider: str) -> None:
    """Check whole issue membership and DOI-bound retained reading HTML."""
    from bs4 import BeautifulSoup

    if provider not in ('aps', 'acs', 'aip'):
        return
    if not html_path.is_file():
        raise ValueError('Issue HTML evidence is missing')
    soup = BeautifulSoup(html_path.read_text(encoding='utf-8'), 'lxml')
    _validate_issue_inventory(data, soup, provider)
    if provider == 'aps':
        _validate_aps_abstracts(data, Path(str(html_path).removesuffix('.html') + '.articles'))
        return
    _validate_silverchair_rows(data, soup, html_path, data['entries'])
    if data.get('schema') == 'papertrack-browser-issue-v1' and data.get('recent_url'):
        from papertrack.journals.recent import load_recent_abstracts
        load_recent_abstracts(data, Path(str(html_path).removesuffix('.html')), provider=provider)


def _validate_silverchair_rows(data, soup, html_path, rows):
    cards = soup.select('.al-article-item-wrap') or soup.select('.al-article-items')
    by_doi = {}
    for card in cards:
        link = card.select_one('.citation-label a')
        if link:
            doi = normalize_doi(link.get('href', ''))
            if doi in by_doi:
                raise ValueError(f'Duplicate HTML article evidence: {doi}')
            by_doi[doi] = card
    for row in rows:
        doi = normalize_doi(row['doi'])
        if JournalEntry.is_non_article(doi, row.get('title', '')):
            continue
        card = by_doi.get(doi)
        if card is None:
            raise ValueError(f'Missing HTML article evidence: {doi}')
        controls = card.select('.showAbstractLink')
        kinds = [(c.get('data-abstract-type') or c.get_text()).strip().lower()
                 for c in controls]
        if any(kind not in ('abstract', 'extract') for kind in kinds):
            raise ValueError(f'Unknown reading control in HTML evidence: {doi}')
        expected = 'abstract' if 'abstract' in kinds else 'extract' if 'extract' in kinds else 'unavailable'
        if row.get('abstract_type') != expected:
            raise ValueError(f'Abstract/Extract type conflicts with HTML evidence: {doi}')
        if expected in ('abstract', 'extract'):
            selected = next(c for c, kind in zip(controls, kinds) if kind == expected)
            panel = _reading_panel(row, data, html_path, soup)
            if (row.get('control') != selected.get('id') or
                    row.get('panel') != selected.get('aria-controls') or
                    not row.get('panel') or panel is None):
                raise ValueError(f'Reading control/panel conflicts with HTML evidence: {doi}')
            nodes, separator = _silverchair_reading_nodes(panel, expected)
            if expected == 'extract':
                # Compare collector notation without mutating retained source
                # nodes subsequently used by formula rendering during resume.
                from copy import deepcopy
                nodes = [deepcopy(node) for node in nodes]
                for paragraph in nodes:
                    for node in paragraph.select('sup, sub'):
                        prefix = '^' if node.name == 'sup' else '_'
                        node.replace_with(prefix + '{' + node.get_text() + '}')
            source = separator.join(node.get_text() for node in nodes)
            # Browser MathJax may use italic Unicode where HTML MathML uses
            # base glyphs. Normalize only for comparison, never rewrite text.
            if not source.strip():
                raise ValueError(f'{expected.title()} source panel has no reading text: {doi}')
            if not row.get('abstract', '').strip():
                raise ValueError(f'{expected.title()} capture has no reading text: {doi}')
            if _comparable_reading_text(source) != _comparable_reading_text(row.get('abstract', '')):
                raise ValueError(f'{expected.title()} text conflicts with HTML evidence: {doi}')


def _validate_aps_abstracts(data: dict, folder: Path) -> None:
    from bs4 import BeautifulSoup

    for row in data['entries']:
        doi = normalize_doi(row['doi'])
        if JournalEntry.is_non_article(doi, row.get('title', '')):
            continue
        key = hashlib.sha256(doi.encode('utf-8')).hexdigest()
        path = folder / (key + '.html')
        if not path.is_file():
            raise ValueError(f'APS abstract HTML evidence is missing: {doi}')
        soup = BeautifulSoup(path.read_text(encoding='utf-8'), 'lxml')
        observed = {normalize_doi(p.get_text().strip()[4:].strip())
                    for p in soup.select('main p') if p.get_text().strip().startswith('DOI:')}
        section = soup.select_one('#abstract-section-content')
        paragraphs = section.find_all('p', recursive=False) if section else []
        if row.get('abstract_type') == 'unavailable':
            fulltext = soup.select_one('#article-page-wrapper[data-article-type="erratum"] #fulltext-content, #article-page-wrapper[data-article-type="comment"] #fulltext-content, #article-page-wrapper[data-article-type="reply"] #fulltext-content, #article-page-wrapper[data-article-type="retraction"] #fulltext-content')
            control = soup.select_one('#section-abstract, a[href="#abstract-section-content"]')
            if (doi not in observed or section is not None or control is not None
                    or fulltext is None or not fulltext.get_text(strip=True)
                    or row.get('abstract') or not row.get('unavailable_reason')):
                raise ValueError(f'APS unavailable abstract conflicts with HTML evidence: {doi}')
            continue
        if any(p.select_one('mjx-lazy') for p in paragraphs):
            raise ValueError(f'APS abstract math evidence is incomplete: {doi}')
        source = '\n\n'.join(p.get_text().strip() for p in paragraphs)
        if (doi not in observed or row.get('abstract_type') != 'abstract' or not source or
                _comparable_reading_text(source) != _comparable_reading_text(row.get('abstract', ''))):
            raise ValueError(f'APS abstract identity/text conflicts with HTML evidence: {doi}')


def read_browser_issue(path: str, *, provider: str, code: str, volume: str,
                       issue: str, journal_name: str, render_math: bool = True) -> AdapterResult:
    """Reject wrong issues, incomplete card extraction and non-publisher captures.

    Snapshots are explicit local input, never automatically promoted by age or
    used for an unrelated issue. Abstract failures remain visible; no network enrichment is performed.
    """
    data = json.loads(Path(path).read_text())
    if data.get('schema') != 'papertrack-browser-issue-v1':
        raise ValueError('Unsupported browser capture schema')
    url = urlsplit(data.get('url', ''))
    host = {'acs': 'pubs.acs.org', 'aip': 'pubs.aip.org', 'aps': 'journals.aps.org'}.get(provider)
    prefixes = {f'/{code}/issue/{volume}/{issue}'}
    if provider == 'aip':
        prefixes.add(f'/aip/{code}/issue/{volume}/{issue}')
    if provider == 'aps':
        prefixes = {f'/{code}/issues/{volume}/{issue}'}
    # AIP uses /jcp/issue/...; callers supply the configured slug.
    if (url.scheme != 'https' or url.hostname != host or url.path.rstrip('/') not in prefixes or
            str(data.get('volume')) != str(volume) or str(data.get('issue')) != str(issue) or
            f'Volume {volume}, Issue {issue}' not in data.get('headings', [])):
        raise ValueError('Browser capture does not match requested publisher and issue')
    if data.get('capture_method') in ('agent', 'fixed_browser'):
        if data.get('inventory_complete') is not True or data.get('errors'):
            raise ValueError('Agent capture is incomplete or has unresolved errors')
    rows = data.get('entries', [])
    if not rows or data.get('card_count') != len(rows):
        raise ValueError('Browser capture has no complete card inventory')
    entries = {}
    seen = set()
    no_abstract_control = []
    reading_types = {'abstract': [], 'extract': []}
    issue_html = None
    source_cards = {}
    from papertrack.journals.recent import load_recent_abstracts
    recent = load_recent_abstracts(data, path, provider=provider, render_math=render_math)
    if provider in ('acs', 'aip') and Path(path + '.html').is_file():
        from bs4 import BeautifulSoup
        issue_html = BeautifulSoup(Path(path + '.html').read_text(encoding='utf-8'), 'lxml')
        _assert_complete_issue_source(data, issue_html)
        for card in issue_html.select('.al-article-item-wrap') or issue_html.select('.al-article-items'):
            link = card.select_one('.citation-label a')
            if link:
                source_cards[normalize_doi(link.get('href', ''))] = card
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('Invalid article record')
        kind = row.get('abstract_type')
        if kind not in ('abstract', 'extract', 'unavailable'):
            raise ValueError('Capture must declare abstract availability')
        if not isinstance(row.get('abstract', ''), str):
            raise ValueError('Abstract must be source text')
        if kind == 'unavailable' and not row.get('unavailable_reason', '').strip():
            raise ValueError('Unavailable abstract requires observed evidence')
        if (not isinstance(row.get('authors', []), list) or
                any(not isinstance(a, str) for a in row.get('authors', []))):
            raise ValueError('Authors must be a list of names')
        doi = normalize_doi(row.get('doi', ''))
        if provider == 'aps' and doi.startswith('10.1103/physrevlett.'):
            if not re.fullmatch(r'10\.1103/physrevlett\.\d+\.\d{6}', doi):
                raise ValueError('Malformed or truncated PRL DOI')
        title = row.get('title', '').strip()
        citation = row.get('citation', '')
        if provider == 'aps':
            match = re.search(r'(\d+),\s*(\d+)\s*\((\d{4})\)', citation)
            article_path = urlsplit(row.get('article_url', '')).path
            placement_ok = bool(match and match.group(1) == str(volume) and
                                article_path.lower() == f'/{code}/abstract/{doi}')
            pages, year = (match.group(2), match.group(3)) if match else ('', '')
        elif provider == 'aip':
            match = re.search(r'(\d+),\s*(\d+)\s*\((\d{4})\)', citation)
            article_path = urlsplit(row.get('article_url', '')).path
            expected_paths = (f'/{code}/article/{volume}/{issue}/', f'/aip/{code}/article/{volume}/{issue}/')
            placement_ok = bool(match and match.group(1) == str(volume) and article_path.startswith(expected_paths))
            pages, year = (match.group(2), match.group(3)) if match else ('', '')
        else:
            match = re.search(r'\((\d{4})\)\s*(\d+)\s*\(([^)]+)\)(?::\s*([^\.]+))?', citation)
            placement_ok = bool(match and match.group(2) == str(volume) and match.group(3) == str(issue))
            pages, year = ((match.group(4) or '').strip(), match.group(1)) if match else ('', '')
        if not doi or not title or doi in seen or not placement_ok:
            raise ValueError('Browser capture contains invalid, duplicate or mismatched article cards')
        seen.add(doi)
        if JournalEntry.is_non_article(doi, title):
            continue
        entry = JournalEntry(doi=doi, title=title, authors=row.get('authors', []),
                             abstract=row.get('abstract', '') if row.get('abstract_type') in ('abstract', 'extract') else '',
                             journal_name=journal_name, volume=str(volume), issue=str(issue),
                             pages=pages, date=year)
        card = source_cards.get(doi)
        if card is not None:
            from papertrack.reports.reading import reading_markdown, reading_text, author_text
            source_title = card.select_one('h3.item-title a')
            if source_title is not None and source_title.select_one('mjx-container, math, sup, sub'):
                entry.title = (reading_markdown if render_math else reading_text)(source_title).strip()
            names = card.select('.wi-fullname') or card.select('[itemprop="author"]')
            if any(name.select_one('mjx-container, math') for name in names):
                entry.authors = [author_text(name) for name in names]
        if provider == 'aps':
            detail = Path(path + '.articles') / (hashlib.sha256(doi.encode('utf-8')).hexdigest() + '.html')
            if detail.is_file():
                from bs4 import BeautifulSoup
                from papertrack.reports.reading import reading_text, reading_markdown
                render = reading_markdown if render_math else reading_text
                soup = BeautifulSoup(detail.read_text(encoding='utf-8'), 'lxml')
                # APS issue titles can contain unrendered mjx-lazy placeholders.
                # The DOI-bound detail HTML retains the publisher's complete
                # citation title as explicit MathML, independent of viewport.
                citation_title = soup.select_one('meta[name="citation_title"][content]')
                heading = soup.select_one('.title-wrapper h1')
                detail_dois = {normalize_doi(p.get_text().strip()[4:].strip())
                               for p in soup.select('main p')
                               if p.get_text().strip().startswith('DOI:')}
                if (citation_title is not None or heading is not None) and doi not in detail_dois:
                    raise ValueError(f'APS title identity conflicts with HTML evidence: {doi}')
                if citation_title is not None:
                    title_node = BeautifulSoup(citation_title['content'], 'lxml')
                    entry.title = render(title_node).strip()
                elif heading is not None:
                    if heading.select_one('mjx-lazy'):
                        raise ValueError(f'APS title math evidence is incomplete: {doi}')
                    entry.title = render(heading).strip()
                if not entry.title:
                    raise ValueError(f'APS source title is empty: {doi}')
                if entry.abstract:
                    paragraphs = soup.select('#abstract-section-content > p')
                    from papertrack.reports.reading import reading_content
                    entry.abstract = '\n\n'.join(reading_content(p, render_math=render_math, doi=doi, source=str(detail)).strip() for p in paragraphs)
        elif issue_html is not None and entry.abstract and row.get('panel'):
            entry.abstract = _render_silverchair_reading(
                row, data, Path(path + '.html'), issue_html, render_math=render_math)
        if doi in recent:
            entry.abstract = recent[doi]
            kind = 'abstract'
        if kind not in ('abstract', 'extract'):
            no_abstract_control.append(doi)
        stamp(entry, 'official_browser')
        if doi in recent:
            entry.field_sources['abstract'] = 'official_browser_recent'
        elif entry.abstract and kind == 'extract':
            entry.field_sources['abstract'] = 'official_browser_extract'
        entries[doi] = entry
        if entry.abstract and entry.abstract.strip():
            reading_types[kind].append(doi)
        else:
            entry.abstract = ''
    if not entries:
        raise ValueError('Browser capture contains no articles')
    missing = [e.doi for e in entries.values() if not e.abstract]
    return AdapterResult(entries, PARTIAL if missing else SUCCESS, provider, 'journal',
                         metadata={'source': 'official', 'transport': 'connected_browser',
                                   'url': data['url'], 'captured_at': data.get('captured_at'),
                                   'membership_authoritative': True, 'missing_abstract_dois': missing,
                                   'reading_coverage': {
                                       'abstract': len(reading_types['abstract']),
                                       'extract': len(reading_types['extract']),
                                       'missing': len(missing),
                                       'excluded': len(rows) - len(entries),
                                   },
                                   'browser_errors': data.get('errors', []),
                                   'browser_error_history': data.get('error_history', []),
                                   'abstract_unavailable_dois': no_abstract_control})

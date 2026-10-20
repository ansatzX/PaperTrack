"""Exact-DOI Abstract supplementation from retained official recent-page HTML."""
import hashlib
import json
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from papertrack.journals.entry import JournalEntry
from papertrack.journals.metadata import normalize_doi


def load_recent_abstracts(data, capture, *, provider, render_math=True):
    """Never add members or replace issue titles/authors with recent metadata."""
    capture = Path(capture)
    manifest = Path(str(capture) + '.recent.json')
    if not data.get('recent_url') and not manifest.exists():
        return {}
    if provider not in ('acs', 'aip') or not manifest.is_file():
        raise ValueError('Recent Abstract supplementation is unresolved')
    supplement = json.loads(manifest.read_text())
    if (supplement.get('schema') != 'papertrack-browser-recent-supplement-v1'
            or supplement.get('issue_url') != data['url']
            or supplement.get('root_url') != data.get('recent_url')
            or supplement.get('complete') is not True or supplement.get('remaining_dois')):
        raise ValueError('Incomplete or mismatched recent supplement')
    issue_url = urlsplit(data['url'])
    root_url = supplement['root_url']
    code = issue_url.path.split('/issue/')[0].rsplit('/', 1)[-1]
    issue_html = BeautifulSoup(Path(str(capture) + '.html').read_text(), 'lxml')
    links = {urljoin(data['url'], a['href']) for a in issue_html.select('a[href]')}
    if root_url not in links:
        raise ValueError('Recent URL was not observed on the official issue page')
    targets = {normalize_doi(row['doi']) for row in data['entries']
               if row.get('abstract_type') == 'unavailable'
               and not JournalEntry.is_non_article(normalize_doi(row['doi']), row.get('title', ''))}
    pages = supplement.get('pages', [])
    if not pages or len(pages) > 20:
        raise ValueError('Invalid recent page inventory')
    from papertrack.journals.capture import validate_reading_controls, _reading_panel
    from papertrack.reports.reading import reading_content

    expected_url = root_url
    verified = {}
    for page in pages:
        url = page['url']
        parsed_url = urlsplit(url)
        if (url != expected_url or parsed_url.scheme != 'https'
                or parsed_url.netloc != issue_url.netloc
                or parsed_url.path != f'/{code}/latest-articles'):
            raise ValueError('Recent page is not an observed same-journal continuation')
        key = hashlib.sha256(url.encode()).hexdigest()
        page_path = Path(str(capture) + '.recent-pages') / (key + '.json')
        source = json.loads(page_path.read_text())
        if (source.get('schema') != 'papertrack-browser-recent-v1'
                or source.get('url') != url or source.get('errors')
                or source.get('remaining_abstracts') != 0):
            raise ValueError('Recent page capture is incomplete')
        page_dois = [normalize_doi(row['doi']) for row in source['entries']]
        if len(page_dois) != len(set(page_dois)):
            raise ValueError('Duplicate DOI in recent page capture')
        page_html_path = Path(str(page_path) + '.html')
        page_html = BeautifulSoup(page_html_path.read_text(), 'lxml')
        next_link = page_html.select_one('a[rel="next"]')
        expected_url = urljoin(url, next_link['href']) if next_link and next_link.get('href') else None
        if page.get('next_url') != expected_url:
            raise ValueError('Recent pagination conflicts with saved HTML')
        validate_reading_controls(source, page_html_path, provider=provider)
        for row in source['entries']:
            doi = normalize_doi(row['doi'])
            if doi not in targets or row.get('abstract_type') != 'abstract':
                raise ValueError('Recent capture contains a non-target DOI or non-Abstract reading')
            panel = _reading_panel(row, source, page_html_path, page_html)
            sections = panel.select('section.abstract[aria-label="Main abstract"]')
            if not sections:
                sections = panel.select('section.abstract:not(.graphicalAbstract)')
            text = ' '.join(reading_content(section, render_math=render_math, doi=doi, source=str(page_html_path)) for section in sections).strip()
            if not text or (doi in verified and verified[doi] != text):
                raise ValueError(f'Empty or conflicting recent Abstract: {doi}')
            verified[doi] = text
    if set(verified) != targets:
        raise ValueError('Recent pages do not resolve every missing issue Abstract')
    # The manifest is only an index. All accepted text above comes from HTML.
    declared = [normalize_doi(row['doi']) for row in supplement.get('entries', [])]
    if len(declared) != len(set(declared)) or set(declared) != targets:
        raise ValueError('Recent supplement DOI index mismatch')
    return verified

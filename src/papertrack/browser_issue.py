"""Validate a DOM capture from the connected-browser issue collector."""
from __future__ import annotations

import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from .adapter_result import AdapterResult, SUCCESS, PARTIAL
from .journal_entry import JournalEntry
from .metadata_sources import normalize_doi, stamp


def read_browser_issue(path: str, *, provider: str, code: str, volume: str,
                       issue: str, journal_name: str) -> AdapterResult:
    """Reject wrong issues, incomplete card extraction and non-publisher captures.

    Snapshots are explicit local input, never automatically promoted by age or
    used for an unrelated issue. Abstract failures remain visible for enrichment.
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
    rows = data.get('entries', [])
    if not rows or data.get('card_count') != len(rows):
        raise ValueError('Browser capture has no complete card inventory')
    entries = {}
    seen = set()
    no_abstract_control = []
    for row in rows:
        doi = normalize_doi(row.get('doi', ''))
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
        if row.get('abstract_type') not in ('abstract', 'extract'):
            no_abstract_control.append(doi)
        stamp(entry, 'official_browser')
        if entry.abstract and row.get('abstract_type') == 'extract':
            entry.field_sources['abstract'] = 'official_browser_extract'
        entries[doi] = entry
    if not entries:
        raise ValueError('Browser capture contains no articles')
    missing = [e.doi for e in entries.values() if not e.abstract]
    return AdapterResult(entries, PARTIAL if missing else SUCCESS, provider, 'journal',
                         metadata={'source': 'official', 'transport': 'connected_browser',
                                   'url': data['url'], 'captured_at': data.get('captured_at'),
                                   'membership_authoritative': True, 'missing_abstract_dois': missing,
                                   'browser_errors': data.get('errors', []),
                                   'browser_error_history': data.get('error_history', []),
                                   'abstract_unavailable_dois': no_abstract_control})

"""Parse dated official archive HTML. No HTTP clients or inferred issue members."""
from dataclasses import dataclass
from datetime import date
from pathlib import Path
import re
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from papertrack.journals.task import archive_url, issue_url


@dataclass(frozen=True)
class FormalIssue:
    volume: str
    issue: str
    published: date
    url: str


def canonical_url(url):
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, p.path.rstrip('/'), p.query, ''))


def archive_page_allowed(cfg, url):
    p = urlsplit(url)
    root = urlsplit(archive_url(cfg))
    code = re.escape(cfg.get('acs_code') or cfg['slug'])
    patterns = {
        'acs': rf'/(?:loi/{code}|{code}/issue-covers(?:/year/\d{{4}})?)',
        'aps': rf'/{code}/issues(?:/\d+)?',
        'aip': rf'/(?:aip/)?{code}/issue(?:/browse-by-year(?:/\d{{4}})?|/\d+)?',
    }
    return (p.scheme == 'https' and p.netloc == root.netloc and
            bool(re.fullmatch(patterns[cfg.get('provider') or 'acs'], p.path.rstrip('/'))))


def _dates(text):
    months = 'January February March April May June July August September October November December'.split()
    month_pattern = '|'.join(months)
    found = set()
    for m in re.finditer(rf'\b({month_pattern})\s+(\d{{1,2}}),?\s+(\d{{4}})\b', text):
        found.add(date(int(m[3]), months.index(m[1]) + 1, int(m[2])))
    for m in re.finditer(rf'\b(\d{{1,2}})\s+({month_pattern})\s+(\d{{4}})\b', text):
        found.add(date(int(m[3]), months.index(m[2]) + 1, int(m[1])))
    return found


def parse_archive_page(cfg, path: Path, url: str, start: date, end: date):
    if not archive_page_allowed(cfg, url):
        raise ValueError('Archive URL is not an official journal archive')
    soup = BeautifulSoup(path.read_text(), 'lxml')
    if not soup.title or re.search('just a moment|access denied|attention required|verify you are human',
                                   soup.title.get_text(), re.I):
        raise ValueError('Archive is blocked or unreadable')
    code = re.escape(cfg.get('acs_code') or cfg['slug'])
    provider = cfg.get('provider') or 'acs'
    pattern = {'acs': rf'/(?:{code}/issue|toc/{code})/(\d+)/(\d+)',
               'aps': rf'/{code}/issues/(\d+)/(\d+)',
               'aip': rf'/(?:aip/)?{code}/issue/(\d+)/(\d+)'}[provider]
    heading = soup.find('h1')
    year_heading = re.search(r'^All Issues:\s*(\d{4})\b', heading.get_text(' ', strip=True)) if heading else None
    rows, undated, next_pages = {}, set(), set()
    for link in soup.select('a[href]'):
        target = canonical_url(urljoin(url, link['href']))
        p = urlsplit(target)
        match = re.fullmatch(pattern, p.path)
        if p.netloc != urlsplit(url).netloc or p.scheme != 'https' or not match:
            continue
        identity = match.groups()
        context = link
        dates = set()
        if provider == 'aip' and year_heading:
            label = link.get_text(' ', strip=True)
            if re.fullmatch(r'Volume \d+, Number \d+: \d{1,2} [A-Za-z]+', label):
                dates = _dates(label + ' ' + year_heading[1])
        for _ in range(4):
            if dates:
                break
            dates = _dates(context.get_text(' ', strip=True))
            if len(dates) == 1:
                break
            if len(dates) > 1 or context.parent is None:
                break
            context = context.parent
        if len(dates) != 1:
            undated.add(identity)
            continue
        published = dates.pop()
        row = FormalIssue(*identity, published, issue_url(cfg, *identity))
        if identity in rows and rows[identity] != row:
            raise ValueError('Conflicting issue dates in archive')
        rows[identity] = row
    undated -= rows.keys()
    # Generic navigation links without dates (e.g. Current Issue) are not inventory.
    years = set(range(start.year, end.year + 1))
    for link in soup.select('a[href], select.issue-browse-year-list option[value]'):
        href = link.get('href') or link.get('value')
        target = canonical_url(urljoin(url, href))
        if not archive_page_allowed(cfg, target) or target == canonical_url(url):
            continue
        context = link.find_parent('tr') or link.find_parent('li') or link
        text = context.get_text(' ', strip=True) + ' ' + target
        observed_years = {int(y) for y in re.findall(r'\b(?:19|20)\d{2}\b', text)}
        selected = link.name == 'option' and link.has_attr('selected')
        if selected and rows and all(r.published.year in observed_years for r in rows.values()):
            continue
        if (years & observed_years or (not observed_years and 'present' in text.lower()) or 'next' in link.get('rel', []) or
                ('browse-by-year' in target and not observed_years)):
            next_pages.add(target)
    if rows and undated:
        raise ValueError('Undated formal issue links remain; archive extractor update required')
    if not rows and not next_pages:
        raise ValueError('No dated inventory or archive navigation; extractor update required')
    return sorted((r for r in rows.values() if start <= r.published <= end),
                  key=lambda r: (r.published, int(r.volume), int(r.issue))), sorted(next_pages)


def validate_inventory(issues, start, end):
    if not issues:
        raise ValueError('No dated issues found; an empty archive is not success')
    years = {r.published.year for r in issues}
    if not set(range(start.year, end.year + 1)) <= years:
        raise ValueError('Archive does not cover every requested year')
    if start.month == 1 and start.day == 1:
        for year in years:
            if min(r.published for r in issues if r.published.year == year).month != 1:
                raise ValueError(f'Archive does not reach January {year}')
    # Gaps are a failure, never a license to invent URLs or issue membership.
    volumes = {}
    for row in issues:
        volumes.setdefault(row.volume, set()).add(int(row.issue))
    for volume, numbers in volumes.items():
        first = 1 if start.month == 1 and start.day == 1 else min(numbers)
        if numbers != set(range(first, max(numbers) + 1)):
            raise ValueError(f'Incomplete archive inventory in volume {volume}')

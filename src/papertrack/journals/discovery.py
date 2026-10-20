"""Local validation of latest-issue discovery; never fetch publisher metadata."""
from pathlib import Path
from urllib.parse import urljoin, urlsplit
import re

from bs4 import BeautifulSoup

from papertrack.journals.task import archive_url, issue_url


def validate_latest_capture(cfg: dict, capture: dict, evidence: Path) -> tuple[str, str]:
    """Require the captured issue to be the highest observed formal archive link.

    This validates the saved rendered archive, not independent completeness of
    the publisher archive. Missing links or access evidence fail visibly.
    """
    archive = archive_url(cfg)
    code = cfg.get('acs_code') or cfg['slug']
    provider = cfg.get('provider') or 'acs'
    patterns = {
        'acs': rf'^/(?:{re.escape(code)}/issue|toc/{re.escape(code)})/(\d+)/(\d+)/?$',
        'aip': rf'^/(?:aip/)?{re.escape(code)}/issue/(\d+)/(\d+)/?$',
        'aps': rf'^/{re.escape(code)}/issues/(\d+)/(\d+)/?$',
    }
    soup = BeautifulSoup(evidence.read_text(encoding='utf-8'), 'html.parser')
    if not soup.title or re.search(
            'just a moment|attention required|access denied|verify you are human',
            soup.title.get_text(), re.I):
        raise ValueError('Readable archive evidence is missing')
    candidates = set()
    for link in soup.select('a[href]'):
        url = urlsplit(urljoin(archive, link['href']))
        match = re.fullmatch(patterns[provider], url.path)
        if (url.scheme == 'https' and url.netloc == urlsplit(archive).netloc and match):
            candidates.add(tuple(int(n) for n in match.groups()))
    if not candidates:
        raise ValueError('No formal issue links in saved archive; extractor update or access required')
    volume, issue = map(str, max(candidates))
    if (str(capture.get('volume')), str(capture.get('issue'))) != (volume, issue):
        raise ValueError('Capture is not the latest observed formal issue')
    if capture.get('url', '').rstrip('/') != issue_url(cfg, volume, issue):
        raise ValueError('Discovered issue URL does not match the capture')
    return volume, issue

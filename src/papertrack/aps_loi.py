"""APS list-of-issues discovery."""

from __future__ import annotations

from datetime import datetime
import logging
import re
import time

from bs4 import BeautifulSoup
from curl_cffi import requests

logger = logging.getLogger(__name__)

APS_BASE = "https://journals.aps.org"
APS_ISSUES_URL = APS_BASE + "/{journal_slug}/issues"
APS_VOLUME_URL = APS_BASE + "/{journal_slug}/issues/{volume}"
APS_IMPERSONATIONS = (
    "firefox",
    "chrome136",
    "safari",
    "chrome131",
    "chrome124",
)
APS_FETCH_ROUNDS = 3
APS_ROUND_BACKOFF = (2, 5)


def _is_blocked(html: str) -> bool:
    head = html[:5000]
    return "Just a moment" in head or "cf-chl" in head or "Cloudflare" in head


def _scrape(url: str) -> str:
    for round_idx in range(APS_FETCH_ROUNDS):
        for impersonate in APS_IMPERSONATIONS:
            try:
                resp = requests.get(url, impersonate=impersonate, timeout=30, allow_redirects=True)
                if resp.status_code == 200 and not _is_blocked(resp.text):
                    logger.info("APS LOI fetch succeeded via %s: %s", impersonate, url)
                    return resp.text
            except Exception as exc:
                logger.debug("APS LOI fetch via %s failed for %s: %s", impersonate, url, exc)
                time.sleep(1)
        if round_idx < APS_FETCH_ROUNDS - 1:
            delay = APS_ROUND_BACKOFF[min(round_idx, len(APS_ROUND_BACKOFF) - 1)]
            logger.info("Retrying APS LOI fetch round %d/%d in %ds: %s",
                        round_idx + 2, APS_FETCH_ROUNDS, delay, url)
            time.sleep(delay)
    return ""


def discover_issues_aps(
    journal_slug: str,
    from_year: int = 0,
    include_partial: bool = False,
) -> list[tuple[int, int, int]]:
    """Discover (year, volume, issue) triples from APS official issue pages.

    Returns newest-first. Partial issues are skipped by default because the
    reading ledger should track stable formal issue membership.
    """
    volumes = _discover_volumes(journal_slug)
    result: list[tuple[int, int, int]] = []
    for volume in volumes:
        issues = _discover_volume_issues(journal_slug, volume, include_partial)
        if from_year and issues and max(year for year, _, _ in issues) < from_year:
            break
        for year, vol, issue in issues:
            if from_year and year < from_year:
                continue
            result.append((year, vol, issue))

    result = sorted(set(result), key=lambda item: (item[0], item[1], item[2]), reverse=True)
    logger.info("APS discovered %d issues for %s", len(result), journal_slug)
    return result


def get_latest_issue_aps(journal_slug: str) -> tuple[int, int, int] | None:
    issues = discover_issues_aps(journal_slug)
    return issues[0] if issues else None


def _discover_volumes(journal_slug: str) -> list[int]:
    html = _scrape(APS_ISSUES_URL.format(journal_slug=journal_slug))
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    volumes = []
    for a in soup.select(f'a[href*="/{journal_slug}/issues/"]'):
        href = a.get("href", "")
        match = re.fullmatch(rf"/{re.escape(journal_slug)}/issues/(\d+)(?:#v\d+)?", href)
        if match:
            volumes.append(int(match.group(1)))
    return sorted(set(volumes), reverse=True)


def _discover_volume_issues(
    journal_slug: str,
    volume: int,
    include_partial: bool = False,
) -> list[tuple[int, int, int]]:
    html = _scrape(APS_VOLUME_URL.format(journal_slug=journal_slug, volume=volume))
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    issues: list[tuple[int, int, int]] = []
    for a in soup.select(f'a[href*="/{journal_slug}/issues/{volume}/"]'):
        href = a.get("href", "")
        match = re.fullmatch(rf"/{re.escape(journal_slug)}/issues/{volume}/(\d+)", href)
        if not match:
            continue
        text = a.get_text(" ", strip=True)
        if not include_partial and "partial" in text.lower():
            continue
        year = _issue_year(text)
        if year:
            issues.append((year, volume, int(match.group(1))))
    return issues


def _issue_year(text: str) -> int:
    match = re.search(r"\b((?:19|20)\d{2})\b", text)
    if match:
        return int(match.group(1))
    for fmt in ("%d %B %Y", "%d %b %Y"):
        date_match = re.search(r":\s*([0-9]{1,2}\s+[A-Za-z]+\s+\d{4})", text)
        if not date_match:
            break
        try:
            return datetime.strptime(date_match.group(1), fmt).year
        except ValueError:
            pass
    return 0

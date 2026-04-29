"""ACS List-of-Issues scraper: discover issue URLs for an ACS journal."""

import logging
import re
import time

import cloudscraper
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

ACS_BASE = "https://pubs.acs.org"
ACS_LOI_URL = ACS_BASE + "/loi/{acs_code}"

MAX_RETRIES = 3
RETRY_BACKOFF = (1, 2, 4)


def _scrape(url: str) -> str:
    """Fetch a page via cloudscraper with retries, return HTML text."""
    scraper = cloudscraper.create_scraper(browser={"browser": "chrome", "platform": "linux"})
    last_exc = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = scraper.get(url, timeout=30)
            if "Just a moment" in resp.text:
                logger.warning("Cloudflare block on %s attempt %d/3", url, attempt + 1)
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_BACKOFF[attempt])
                continue
            return resp.text
        except Exception as e:
            last_exc = e
            logger.warning("Fetch %s attempt %d/3 failed: %s", url, attempt + 1, e)
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_BACKOFF[attempt])
    raise RuntimeError(f"Failed to fetch {url} after {MAX_RETRIES} attempts: {last_exc}")


def _extract_year(url: str) -> int:
    """Extract the publication year from a year-group URL.

    e.g. '/loi/jctcce/group/d2020.y2025' → 2025
         '/loi/jctcce' → current year
    """
    m = re.search(r"\.y(\d{4})", url)
    if m:
        return int(m.group(1))
    return time.localtime().tm_year


def discover_issues(acs_code: str, from_year: int = 0) -> list[tuple[int, int, str]]:
    """Discover all issue (volume, issue, url) tuples for an ACS journal.

    Scrapes the LOI landing page for year-group links, then each year-group
    page for individual issue URLs.  Issues are returned sorted newest-first.

    Args:
        acs_code: ACS journal code (e.g. 'jctcce').
        from_year: If > 0, only scrape years >= from_year.

    Returns:
        List of (volume, issue, toc_url) tuples, newest first.
    """
    landing_url = ACS_LOI_URL.format(acs_code=acs_code)
    logger.info("Discovering issues for %s (from_year=%d)...", acs_code, from_year)

    html = _scrape(landing_url)
    soup = BeautifulSoup(html, "lxml")

    # Collect year-group URLs from the landing page
    year_urls: list[str] = []
    seen_years = set()
    for a in soup.select('a[href*="/loi/"][href*="/group/d"]'):
        href = a.get("href", "")
        if href not in seen_years:
            seen_years.add(href)
            year_urls.append(href)

    # Filter by from_year
    if from_year:
        year_urls = [u for u in year_urls if _extract_year(u) >= from_year]

    if not year_urls:
        logger.warning("No year-group links found on %s", landing_url)
        return []

    # Also check: the landing page itself lists issues for the current year
    current_year = time.localtime().tm_year
    if not from_year or current_year >= from_year:
        year_urls.insert(0, landing_url)

    all_issues: dict[tuple[int, int], str] = {}

    for yr_url in year_urls:
        full_url = yr_url if yr_url.startswith("http") else ACS_BASE + yr_url
        try:
            yr_html = _scrape(full_url) if yr_url != landing_url else html
            yr_soup = BeautifulSoup(yr_html, "lxml") if yr_url != landing_url else soup
        except Exception:
            logger.exception("Skipping %s", yr_url)
            continue

        for a in yr_soup.select('a[href*="/toc/"]'):
            href = a.get("href", "")
            m = re.match(rf"/toc/{acs_code}/(\d+)/(\d+)", href)
            if not m:
                continue
            vol, iss = int(m.group(1)), int(m.group(2))
            if vol == 0 and iss == 0:
                continue  # ASAPs placeholder
            key = (vol, iss)
            if key not in all_issues:
                all_issues[key] = href

    result = [(v, i, u) for (v, i), u in all_issues.items()]
    result.sort(reverse=True)
    logger.info("Discovered %d issues for %s", len(result), acs_code)
    return result


def get_latest_issue(acs_code: str) -> tuple[int, int, str] | None:
    """Get the latest issue for an ACS journal from its LOI landing page."""
    html = _scrape(ACS_LOI_URL.format(acs_code=acs_code))
    soup = BeautifulSoup(html, "lxml")

    for a in soup.select('a[href*="/toc/"]'):
        href = a.get("href", "")
        m = re.match(rf"/toc/{acs_code}/(\d+)/(\d+)", href)
        if not m:
            continue
        vol, iss = int(m.group(1)), int(m.group(2))
        if vol == 0 and iss == 0:
            continue
        logger.info("Latest issue for %s: Vol %d, Issue %d", acs_code, vol, iss)
        return (vol, iss, href)

    return None

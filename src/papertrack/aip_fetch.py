"""Direct AIP issue scraper using curl_cffi impersonation."""

from html import unescape
import logging
import re
import time

from bs4 import BeautifulSoup
from curl_cffi import requests

from .journal_entry import JournalEntry

logger = logging.getLogger(__name__)

AIP_BASE = "https://pubs.aip.org"
AIP_ABSTRACT_URL = AIP_BASE + "/PlatformArticle/ArticleAbstractAjax"
AIP_ISSUE_URLS = (
    AIP_BASE + "/aip/{journal_slug}/issue/{volume}/{issue}",
    AIP_BASE + "/{journal_slug}/issue/{volume}/{issue}",
)
AIP_IMPERSONATIONS = (
    "firefox",
    "chrome124",
    "chrome131",
    "safari",
    "chrome136",
    "firefox133",
    "safari184",
)
AIP_FETCH_ROUNDS = 3
AIP_ROUND_BACKOFF = (2, 5)

_cache: dict[str, dict[str, JournalEntry]] = {}


def _is_blocked(html: str) -> bool:
    head = html[:5000]
    return "Just a moment" in head or "cf-chl" in head or "Cloudflare" in head


def _fetch_html(url: str, required_selector: str = "") -> str:
    """Fetch AIP HTML, trying several browser fingerprints."""
    last_error = None
    for round_idx in range(AIP_FETCH_ROUNDS):
        for impersonate in AIP_IMPERSONATIONS:
            try:
                resp = requests.get(url, impersonate=impersonate, timeout=30, allow_redirects=True)
                if resp.status_code != 200 or _is_blocked(resp.text):
                    logger.debug("AIP %s via %s returned %s/block", url, impersonate, resp.status_code)
                    continue
                if required_selector:
                    soup = BeautifulSoup(resp.text, "lxml")
                    if not soup.select(required_selector):
                        logger.debug("AIP %s via %s missing %s", url, impersonate, required_selector)
                        continue
                logger.info("AIP fetch succeeded via %s: %s", impersonate, url)
                return resp.text
            except Exception as exc:
                last_error = exc
                logger.debug("AIP fetch via %s failed for %s: %s", impersonate, url, exc)
                time.sleep(1)
        if round_idx < AIP_FETCH_ROUNDS - 1:
            delay = AIP_ROUND_BACKOFF[min(round_idx, len(AIP_ROUND_BACKOFF) - 1)]
            logger.info("Retrying AIP fetch round %d/%d in %ds: %s",
                        round_idx + 2, AIP_FETCH_ROUNDS, delay, url)
            time.sleep(delay)

    if last_error:
        logger.warning("AIP fetch failed for %s: %s", url, last_error)
    return ""


def _clean_abstract_html(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for selector in ("h1", "h2", "h3", ".title"):
        for el in soup.select(selector):
            if el.get_text(" ", strip=True).lower() == "abstract":
                el.decompose()
    text = soup.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", unescape(text)).strip()


def _fetch_aip_abstract(article_id: str, referer_url: str) -> str:
    """Fetch an abstract through AIP's issue-page AJAX endpoint."""
    params = {"articleId": article_id, "layAbstract": "False"}
    headers = {
        "Referer": referer_url,
        "X-Requested-With": "XMLHttpRequest",
    }
    last_error = None
    for round_idx in range(AIP_FETCH_ROUNDS):
        for impersonate in AIP_IMPERSONATIONS:
            try:
                resp = requests.get(
                    AIP_ABSTRACT_URL,
                    params=params,
                    headers=headers,
                    impersonate=impersonate,
                    timeout=30,
                    allow_redirects=True,
                )
                if resp.status_code != 200 or _is_blocked(resp.text):
                    logger.debug("AIP abstract %s via %s returned %s/block",
                                 article_id, impersonate, resp.status_code)
                    continue
                data = resp.json()
                if not data.get("Success"):
                    continue
                abstract = _clean_abstract_html(data.get("Html", "") or "")
                if abstract:
                    return abstract
            except Exception as exc:
                last_error = exc
                logger.debug("AIP abstract fetch via %s failed for %s: %s",
                             impersonate, article_id, exc)
                time.sleep(1)
        if round_idx < AIP_FETCH_ROUNDS - 1:
            delay = AIP_ROUND_BACKOFF[min(round_idx, len(AIP_ROUND_BACKOFF) - 1)]
            logger.debug("Retrying AIP abstract round %d/%d in %ds: %s",
                         round_idx + 2, AIP_FETCH_ROUNDS, delay, article_id)
            time.sleep(delay)

    if last_error:
        logger.warning("AIP abstract fetch failed for %s: %s", article_id, last_error)
    return ""


def _parse_aip_toc(
    html: str, journal_name: str
) -> tuple[dict[str, JournalEntry], dict[str, str]]:
    """Parse an AIP issue page into entries and abstract AJAX article IDs."""
    soup = BeautifulSoup(html, "lxml")
    entries: dict[str, JournalEntry] = {}
    abstract_ids: dict[str, str] = {}

    for item in soup.select(".al-article-items"):
        title_el = item.select_one("h3.item-title a, .item-title a")
        if not title_el:
            continue

        title = title_el.get_text(" ", strip=True)
        article_href = title_el.get("href", "")

        doi = ""
        doi_el = item.select_one('.citation-label a[href*="doi.org/10."]')
        if doi_el:
            doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi_el.get("href", "")).strip()
        if not doi:
            pdf_el = item.select_one("[data-doi]")
            doi = (pdf_el.get("data-doi", "") if pdf_el else "").strip()
        if not doi:
            continue
        if JournalEntry.is_non_article(doi, title):
            continue

        authors = [
            author.get_text(" ", strip=True)
            for author in item.select(".al-authors-list .wi-fullname")
            if author.get_text(" ", strip=True)
        ]

        toc_image_url = ""
        img = item.select_one(".issue-featured-image img")
        if img:
            toc_image_url = img.get("src", "") or img.get("data-src", "")

        citation_text = item.select_one(".ww-citation-primary")
        citation = citation_text.get_text(" ", strip=True) if citation_text else ""
        volume = issue = pages = date = ""
        m = re.search(r"\b(\d+)\s*,\s*([\w.-]+)\s*\((\d{4})\)", citation)
        if m:
            volume, pages, date = m.group(1), m.group(2), m.group(3)
        url_m = re.search(r"/article/(\d+)/(\d+)/([^/]+)/", article_href)
        if url_m:
            volume = volume or url_m.group(1)
            issue = url_m.group(2)
            pages = pages or url_m.group(3)

        entries[doi] = JournalEntry(
            doi=doi,
            title=title,
            authors=authors,
            journal_name=journal_name,
            volume=volume,
            issue=issue,
            pages=pages,
            date=date,
            toc_image_url=toc_image_url,
        )
        abstract_link = item.select_one(".js-show-abstract[data-articleid]")
        if abstract_link:
            abstract_id = abstract_link.get("data-articleid", "").strip()
            if abstract_id:
                abstract_ids[doi] = abstract_id

    return entries, abstract_ids


def query_aip_issue(journal_slug: str, volume: str, issue: str,
                    journal_name: str = "") -> dict[str, JournalEntry]:
    """Scrape a specific AIP issue page without visiting article pages."""
    cache_key = f"{journal_slug}:{volume}:{issue}"
    if cache_key in _cache:
        return _cache[cache_key]

    html = ""
    for template in AIP_ISSUE_URLS:
        url = template.format(journal_slug=journal_slug, volume=volume, issue=issue)
        html = _fetch_html(url, ".al-article-items")
        if html:
            break

    if not html:
        return {}

    entries, abstract_ids = _parse_aip_toc(html, journal_name)
    for doi, article_id in abstract_ids.items():
        abstract = _fetch_aip_abstract(article_id, url)
        if abstract:
            entries[doi].abstract = abstract

    _cache[cache_key] = entries
    logger.info("AIP %s v%s i%s: %d articles scraped",
                journal_slug, volume, issue, len(entries))
    return entries

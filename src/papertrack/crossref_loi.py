"""CrossRef-based issue discovery for non-ACS journals.

Direct AIP issue pages are used for article metadata, but CrossRef is still
used to discover which volume/issue combinations exist for a journal.
"""

import logging
import time

from .journal_fetch import _fetch_year

logger = logging.getLogger(__name__)


def discover_issues_crossref(
    issn: str, from_year: int = 0
) -> list[tuple[int, int, int]]:
    """Discover (year, volume, issue) triples via CrossRef for an ISSN.

    Queries CrossRef one year at a time, then groups results by
    (volume, issue), recording the earliest year each volume/issue
    appears.  Returns a list sorted newest-first so the caller can
    reverse for backfill.
    """
    current_year = time.localtime().tm_year
    start_year = from_year if from_year else current_year - 10
    end_year = current_year

    # Map (vol, iss) -> year (earliest year that pair appears)
    issues_map: dict[tuple[int, int], int] = {}

    for yr in range(start_year, end_year + 1):
        try:
            articles = _fetch_year(issn, yr)
        except Exception:
            logger.exception("CrossRef discovery: failed for %s %d", issn, yr)
            continue

        for item in articles:
            vol = item.get("volume", "") or ""
            iss = item.get("issue", "") or ""
            if not vol or not iss:
                continue
            try:
                key = (int(vol), int(iss))
            except (ValueError, TypeError):
                continue
            if key not in issues_map:
                issues_map[key] = yr

    # Sort newest-first by (year, volume, issue) descending
    result = sorted(
        [(y, v, i) for (v, i), y in issues_map.items()],
        key=lambda x: (x[0], x[1], x[2]),
        reverse=True,
    )
    logger.info("CrossRef discovered %d issues for %s", len(result), issn)
    return result

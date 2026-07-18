"""Bounded Playwright helper for official APS issue pages."""
from __future__ import annotations

import json
import sys
from urllib.parse import urlsplit

from .adapter_result import BLOCKED, FETCH_FAILED, SUCCESS


def _run(issue_url: str, timeout: int = 45) -> dict:
    parsed = urlsplit(issue_url)
    if (parsed.scheme, parsed.netloc) != ("https", "journals.aps.org") or "/issues/" not in parsed.path:
        raise RuntimeError("APS browser requires official issue URL")
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=False)
            context = browser.new_context()
            page = context.new_page()
            try:
                page.goto(issue_url, wait_until="domcontentloaded", timeout=timeout * 1000)
                try:
                    page.wait_for_selector(".browse-result-card.issue", timeout=min(timeout * 1000, 15000))
                except Exception:
                    pass
                html = page.content()
                title = page.title().lower()
                if any(marker in html for marker in ("Just a moment", "cf-chl", "challenge-platform")) or "access denied" in title:
                    return {"html": html, "outcome": BLOCKED, "message": "APS browser challenge"}
                if "browse-result-card issue" not in html:
                    return {"html": html, "outcome": FETCH_FAILED, "message": "APS issue content not ready"}
                return {"html": html, "outcome": SUCCESS}
            finally:
                context.close()
                browser.close()
    except Exception as exc:
        return {"html": "", "outcome": FETCH_FAILED, "message": str(exc)}


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m papertrack.aps_browser ISSUE_URL")
    print(json.dumps(_run(sys.argv[1])))

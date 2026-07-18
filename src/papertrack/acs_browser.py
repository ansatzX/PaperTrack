"""Bounded Playwright helper for official ACS issue pages."""
from __future__ import annotations
import json, sys
from urllib.parse import urlsplit
from .adapter_result import BLOCKED, FETCH_FAILED, SUCCESS

def _run(issue_url: str, timeout: int = 45) -> dict:
    p = urlsplit(issue_url)
    if (p.scheme, p.netloc) != ("https", "pubs.acs.org") or "/toc/" not in p.path:
        raise RuntimeError("ACS browser requires official issue URL")
    from playwright.sync_api import sync_playwright
    browser = context = page = None
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=False)
            context = browser.new_context()
            page = context.new_page()
            page.goto(issue_url, wait_until="domcontentloaded", timeout=timeout * 1000)
            html = page.content()
            status = None
            if "Just a moment" in html or "cf-chl" in html or page.title().lower().find("access denied") >= 0:
                return {"html": html, "outcome": BLOCKED, "status_code": status, "message": "ACS browser challenge"}
            return {"html": html, "outcome": SUCCESS, "status_code": status}
    except Exception as exc:
        return {"html": "", "outcome": FETCH_FAILED, "message": str(exc)}
    finally:
        for obj in (page, context, browser):
            try:
                if obj: obj.close()
            except Exception: pass

if __name__ == "__main__":
    print(json.dumps(_run(sys.argv[1])))

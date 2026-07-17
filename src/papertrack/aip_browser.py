"""One-shot AIP Playwright helper, invoked only through ``xvfb-run``."""

from __future__ import annotations

from dataclasses import asdict
import json
import sys
from urllib.parse import urlencode, urlsplit

from .adapter_result import BLOCKED, FETCH_FAILED, SUCCESS
from .aip_fetch import AipAbstractResult, _clean_abstract_html, aip_abstract_url


def _browser_request_allowed(
    request_url: str,
    resource_type: str,
    issue_url: str,
    abstract_endpoint: str,
) -> bool:
    if resource_type in {"image", "font", "media"}:
        return False
    if resource_type == "document":
        request = urlsplit(request_url)
        issue = urlsplit(issue_url)
        return (request.scheme, request.netloc, request.path) == (
            issue.scheme,
            issue.netloc,
            issue.path,
        )
    if resource_type in {"xhr", "fetch"}:
        return request_url.startswith(abstract_endpoint) or (
            "/cdn-cgi/challenge-platform/" in request_url
        )
    return True


def _run(issue_url: str, journal_slug: str) -> dict:
    from playwright.sync_api import sync_playwright

    endpoint = aip_abstract_url(journal_slug)
    result = {"html": "", "abstracts": {}, "outcome": "", "message": ""}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False)
        context = browser.new_context()

        def route_request(route):
            request = route.request
            if not _browser_request_allowed(
                request.url,
                request.resource_type,
                issue_url,
                endpoint,
            ):
                route.abort()
                return
            route.continue_()

        context.route("**/*", route_request)
        page = context.new_page()
        try:
            response = page.goto(issue_url, wait_until="domcontentloaded", timeout=60000)
            result["status_code"] = response.status if response else None
            page.wait_for_selector(".al-article-items", timeout=60000)
            result["html"] = page.content()
            article_ids = page.locator(".js-show-abstract[data-articleid]").evaluate_all(
                "els => [...new Set(els.map(el => el.dataset.articleid).filter(Boolean))]"
            )
            for article_id in article_ids:
                url = endpoint + "?" + urlencode(
                    {"articleId": article_id, "layAbstract": "False"}
                )
                payload = page.evaluate(
                    """async ({url, issueUrl}) => {
                        const response = await fetch(url, {
                          headers: {'X-Requested-With': 'XMLHttpRequest', 'Referer': issueUrl}
                        });
                        return {status: response.status, text: await response.text()};
                    }""",
                    {"url": url, "issueUrl": issue_url},
                )
                status = payload["status"]
                try:
                    data = json.loads(payload["text"])
                except json.JSONDecodeError:
                    data = {}
                abstract = _clean_abstract_html(data.get("Html", "") or "")
                if status == 200 and data.get("Success") and abstract:
                    abstract_result = AipAbstractResult(
                        abstract=abstract,
                        outcome=SUCCESS,
                        status_code=status,
                        url=endpoint,
                        article_id=article_id,
                    )
                else:
                    abstract_result = AipAbstractResult(
                        outcome=(
                            BLOCKED
                            if status == 403
                            else "official_response_empty"
                            if status == 200
                            else FETCH_FAILED
                        ),
                        status_code=status,
                        message="browser abstract endpoint returned no abstract",
                        url=endpoint,
                        article_id=article_id,
                    )
                result["abstracts"][article_id] = asdict(abstract_result)
        except Exception as exc:
            html = page.content()
            result["html"] = ""
            result["outcome"] = BLOCKED if "Just a moment" in html or "cf-chl" in html else FETCH_FAILED
            result["message"] = repr(exc)
        finally:
            context.close()
            browser.close()
    return result


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: python -m papertrack.aip_browser ISSUE_URL JOURNAL_SLUG")
    print(json.dumps(_run(sys.argv[1], sys.argv[2]), ensure_ascii=False))


if __name__ == "__main__":
    main()

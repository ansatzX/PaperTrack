"""One-shot AIP Playwright helper, invoked only through ``xvfb-run``."""

from __future__ import annotations

from dataclasses import asdict
import json
import math
import sys
import time
from urllib.parse import urlencode, urlsplit

from .adapter_result import BLOCKED, FETCH_FAILED, SUCCESS
from .aip_fetch import (
    AIP_BROWSER_ABSTRACT_RESPONSE_ALLOWANCE,
    AIP_BROWSER_MAX_ABSTRACTS,
    AipAbstractResult,
    _clean_abstract_html,
    aip_abstract_url,
)

AIP_BROWSER_ABSTRACT_FLOOR = 10.0


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


def _wait_for_abstract_interval(
    last_request_at: float | None,
    interval: float,
) -> float:
    interval = _browser_abstract_interval(interval)
    now = time.monotonic()
    if last_request_at is not None:
        delay = max(0.0, last_request_at + interval - now)
        if delay:
            time.sleep(delay)
            now = time.monotonic()
    return now


def _browser_abstract_interval(raw: str | float) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("AIP browser abstract interval must be non-negative") from exc
    if not math.isfinite(value) or value < 0:
        raise RuntimeError("AIP browser abstract interval must be non-negative")
    return max(AIP_BROWSER_ABSTRACT_FLOOR, value)


def _validate_abstract_count(abstract_count: int) -> None:
    if abstract_count > AIP_BROWSER_MAX_ABSTRACTS:
        raise RuntimeError(
            "AIP issue exceeds browser abstract request limit "
            f"({abstract_count} > {AIP_BROWSER_MAX_ABSTRACTS})"
        )


def _run(issue_url: str, journal_slug: str, abstract_interval: str | float) -> dict:
    from playwright.sync_api import sync_playwright

    abstract_interval = _browser_abstract_interval(abstract_interval)
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
            _validate_abstract_count(len(article_ids))
            last_abstract_request_at = None
            for article_id in article_ids:
                url = endpoint + "?" + urlencode(
                    {"articleId": article_id, "layAbstract": "False"}
                )
                last_abstract_request_at = _wait_for_abstract_interval(
                    last_abstract_request_at,
                    abstract_interval,
                )
                payload = page.evaluate(
                    """async ({url, issueUrl, responseTimeoutMs}) => {
                        const controller = new AbortController();
                        const timer = setTimeout(
                          () => controller.abort(), responseTimeoutMs
                        );
                        try {
                          const response = await fetch(url, {
                            headers: {
                              'X-Requested-With': 'XMLHttpRequest',
                              'Referer': issueUrl
                            },
                            signal: controller.signal
                          });
                          return {status: response.status, text: await response.text()};
                        } finally {
                          clearTimeout(timer);
                        }
                    }""",
                    {
                        "url": url,
                        "issueUrl": issue_url,
                        "responseTimeoutMs": int(
                            AIP_BROWSER_ABSTRACT_RESPONSE_ALLOWANCE * 1000
                        ),
                    },
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
    if len(sys.argv) != 4:
        raise SystemExit(
            "usage: python -m papertrack.aip_browser "
            "ISSUE_URL JOURNAL_SLUG ABSTRACT_INTERVAL"
        )
    print(
        json.dumps(
            _run(sys.argv[1], sys.argv[2], sys.argv[3]),
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()

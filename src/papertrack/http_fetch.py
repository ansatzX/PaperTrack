"""HTTP-only fetch helpers for publisher issue pages."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import importlib
import logging
import math
import os
import time
from typing import Mapping, Sequence

from bs4 import BeautifulSoup

from .adapter_result import BLOCKED, FETCH_FAILED, RATE_LIMITED

logger = logging.getLogger(__name__)

DEFAULT_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "User-Agent": "PaperTrack/0.1 (mailto:ansatzMe@outlook.com)",
}

DEFAULT_HTTP_CLIENTS = (
    "curl_cffi",
    "cloudscraper",
    "primp",
    "tls_client",
    "pyreqwest_impersonate",
    "requests",
)

CURL_IMPERSONATIONS = (
    "chrome136",
    "chrome131",
    "chrome124",
    "firefox",
    "safari",
)
PRIMP_IMPERSONATIONS = (
    "chrome_136",
    "chrome_131",
    "chrome_124",
    "firefox_133",
    "safari_18",
)
TLS_CLIENT_IDENTIFIERS = (
    "chrome_120",
    "chrome112",
    "firefox_120",
    "safari_16_0",
)
PYREQWEST_IMPERSONATIONS = (
    "chrome_136",
    "chrome_131",
    "chrome_124",
    "firefox_133",
    "safari_18",
)

BLOCK_MARKERS = (
    "Just a moment",
    "cf-chl",
    "challenge-platform",
    "Attention Required!",
    "/cdn-cgi/challenge-platform/",
)


@dataclass
class HttpFetchResult:
    html: str = ""
    outcome: str = ""
    status_code: int | None = None
    client: str = ""
    variant: str = ""
    error: str = ""
    history: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.html) and not self.outcome


@dataclass(frozen=True)
class HttpRequestPolicy:
    min_interval: float
    max_attempts: int = 3
    backoff: tuple[float, ...] = (2.0, 5.0)

    def __post_init__(self) -> None:
        if self.min_interval < 0:
            raise ValueError("min_interval must be non-negative")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least one")


DEFAULT_STRATEGIES = (
    ("curl_cffi", "chrome136"),
    ("requests", "default"),
)

_last_request_at: dict[str, float] = {}


def configured_interval(
    name: str,
    floor: float,
    env: Mapping[str, str] | None = None,
) -> float:
    """Read a request interval without allowing configuration below ``floor``."""
    values = os.environ if env is None else env
    raw = values.get(name, "").strip()
    if not raw:
        return float(floor)
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a non-negative number") from exc
    if not math.isfinite(value) or value < 0:
        raise RuntimeError(f"{name} must be a non-negative number")
    return max(float(floor), value)


def clear_provider_throttles() -> None:
    """Clear process-local provider timing state (primarily for tests)."""
    _last_request_at.clear()


def fetch_html_resilient(
    url: str,
    *,
    required_selector: str = "",
    clients: Sequence[str] | None = None,
    curl_impersonations: Sequence[str] = CURL_IMPERSONATIONS,
    rounds: int | None = None,
    backoff: Sequence[float] | None = None,
    strategies: Sequence[tuple[str, str]] | None = None,
    request_policy: HttpRequestPolicy | None = None,
    provider: str = "",
    timeout: int = 30,
    headers: Mapping[str, str] | None = None,
    log_label: str = "source",
) -> HttpFetchResult:
    """Fetch HTML with a bounded, sequential list of HTTP strategies."""
    merged_headers = {**DEFAULT_HEADERS, **(dict(headers) if headers else {})}
    if strategies is None:
        strategies = _legacy_strategies(clients, curl_impersonations)
    if request_policy is None:
        request_policy = HttpRequestPolicy(
            min_interval=0,
            max_attempts=rounds or 3,
            backoff=tuple(backoff) if backoff is not None else (2.0, 5.0),
        )
    provider_key = (provider or log_label).strip().lower() or "source"
    history: list[dict] = []
    last_html = ""
    last_status = None
    last_error = ""
    last_client = ""
    last_variant = ""
    last_outcome = FETCH_FAILED

    attempts = tuple(strategies)[:request_policy.max_attempts]
    for attempt_idx, (client, variant) in enumerate(attempts):
        last_client = client
        last_variant = variant
        event = {
            "client": client,
            "variant": variant,
            "attempt": attempt_idx + 1,
            "throttle_delay": _throttle(provider_key, request_policy.min_interval),
        }
        try:
            response = _fetch_once(client, variant, url, merged_headers, timeout)
            html = _response_text(response)
            status = _response_status(response)
            retry_after = _response_retry_after(response)
            event["status_code"] = status
            if retry_after is not None:
                event["retry_after"] = retry_after
            history.append(event)
            last_html = html
            last_status = status

            blocked = status == 403 or is_blocked_response(html)
            if blocked:
                event["blocked"] = True
                last_outcome = BLOCKED
                if retry_after is not None and attempt_idx + 1 < len(attempts):
                    _sleep_for_retry(event, retry_after)
                    continue
                break

            if status in (None, 200):
                if required_selector and not _has_selector(html, required_selector):
                    event["missing_selector"] = required_selector
                    break
                logger.info(
                    "%s fetch succeeded via %s/%s: %s",
                    log_label,
                    client,
                    variant,
                    url,
                )
                return HttpFetchResult(
                    html=html,
                    status_code=status,
                    client=client,
                    variant=variant,
                    history=history,
                )

            if status == 429:
                last_outcome = RATE_LIMITED
            if not _retryable_status(status) or attempt_idx + 1 >= len(attempts):
                break
            delay = retry_after
            if delay is None:
                delay = _backoff_delay(request_policy.backoff, attempt_idx)
            _sleep_for_retry(event, delay)
        except ImportError as exc:
            event["missing_client"] = str(exc)
            history.append(event)
            logger.debug("%s fetch client unavailable: %s", client, exc)
        except Exception as exc:
            last_error = repr(exc)
            event["exception"] = last_error
            history.append(event)
            logger.debug(
                "%s fetch via %s/%s failed for %s: %s",
                log_label,
                client,
                variant,
                url,
                exc,
            )
            if attempt_idx + 1 < len(attempts):
                _sleep_for_retry(
                    event,
                    _backoff_delay(request_policy.backoff, attempt_idx),
                )

    return HttpFetchResult(
        html=last_html,
        outcome=last_outcome,
        status_code=last_status,
        client=last_client,
        variant=last_variant,
        error=last_error,
        history=history,
    )


def is_blocked_response(html: str) -> bool:
    head = html[:10000]
    return any(marker in head for marker in BLOCK_MARKERS)


def _legacy_strategies(
    clients: Sequence[str] | None,
    curl_impersonations: Sequence[str],
) -> tuple[tuple[str, str], ...]:
    if clients is None:
        return DEFAULT_STRATEGIES
    strategies = []
    for client in clients:
        variants = _variants_for_client(client, curl_impersonations)
        strategies.append((client, variants[0]))
    return tuple(strategies)


def _throttle(provider: str, min_interval: float) -> float:
    now = time.monotonic()
    previous = _last_request_at.get(provider)
    delay = 0.0 if previous is None else max(0.0, previous + min_interval - now)
    if delay:
        time.sleep(delay)
        now = time.monotonic()
    _last_request_at[provider] = now
    return delay

def provider_throttle(provider: str, min_interval: float) -> float:
    """Apply shared provider throttle for non-HTTP requests."""
    return _throttle(provider, min_interval)


def _backoff_delay(backoff: Sequence[float], attempt_idx: int) -> float:
    if not backoff:
        return 0.0
    return float(backoff[min(attempt_idx, len(backoff) - 1)])


def _sleep_for_retry(event: dict, delay: float) -> None:
    event["retry_delay"] = float(delay)
    if delay:
        time.sleep(delay)


def _retryable_status(status: int | None) -> bool:
    return status in {408, 425, 429} or bool(status and 500 <= status <= 599)


def _variants_for_client(client: str, curl_impersonations: Sequence[str]) -> tuple[str, ...]:
    if client == "curl_cffi":
        return tuple(curl_impersonations)
    if client == "cloudscraper":
        return ("chrome", "firefox")
    if client == "primp":
        return PRIMP_IMPERSONATIONS
    if client == "tls_client":
        return TLS_CLIENT_IDENTIFIERS
    if client == "pyreqwest_impersonate":
        return PYREQWEST_IMPERSONATIONS
    if client == "requests":
        return ("default",)
    return ("unsupported",)


def _fetch_once(client: str, variant: str, url: str, headers: Mapping[str, str], timeout: int):
    if client == "curl_cffi":
        curl_requests = importlib.import_module("curl_cffi.requests")
        return curl_requests.get(
            url,
            headers=dict(headers),
            impersonate=variant,
            timeout=timeout,
            allow_redirects=True,
        )
    if client == "cloudscraper":
        cloudscraper = importlib.import_module("cloudscraper")
        scraper = cloudscraper.create_scraper(
            browser={"browser": variant, "platform": "linux", "desktop": True},
            delay=5,
        )
        return scraper.get(url, headers=dict(headers), timeout=timeout, allow_redirects=True)
    if client == "primp":
        primp = importlib.import_module("primp")
        client_obj = primp.Client(impersonate=variant)
        try:
            return client_obj.get(
                url,
                headers=dict(headers),
                timeout=timeout,
                follow_redirects=True,
            )
        except TypeError:
            return client_obj.get(url, headers=dict(headers), timeout=timeout)
    if client == "tls_client":
        tls_client = importlib.import_module("tls_client")
        session = tls_client.Session(
            client_identifier=variant,
            random_tls_extension_order=True,
        )
        try:
            return session.get(
                url,
                headers=dict(headers),
                timeout_seconds=timeout,
                allow_redirects=True,
            )
        except TypeError:
            return session.get(url, headers=dict(headers), timeout_seconds=timeout)
    if client == "pyreqwest_impersonate":
        pyreqwest = importlib.import_module("pyreqwest_impersonate")
        if hasattr(pyreqwest, "Client"):
            client_obj = pyreqwest.Client(impersonate=variant)
            return client_obj.get(url, headers=dict(headers), timeout=timeout)
        return pyreqwest.get(url, headers=dict(headers), timeout=timeout, impersonate=variant)

    if client == "requests":
        requests = importlib.import_module("requests")
        return requests.get(url, headers=dict(headers), timeout=timeout, allow_redirects=True)
    raise ImportError(f"unsupported HTTP client: {client}")


def _response_status(response) -> int | None:
    status = getattr(response, "status_code", None)
    if status is None:
        status = getattr(response, "status", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def _response_retry_after(response) -> float | None:
    headers = getattr(response, "headers", {}) or {}
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        pass
    try:
        retry_at = parsedate_to_datetime(str(raw))
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return max(
            0.0,
            (retry_at - datetime.now(timezone.utc)).total_seconds(),
        )
    except (TypeError, ValueError, OverflowError):
        return None


def _response_text(response) -> str:
    text = getattr(response, "text", "")
    if callable(text):
        text = text()
    if text:
        return str(text)

    content = getattr(response, "content", b"")
    if callable(content):
        content = content()
    if isinstance(content, bytes):
        return content.decode("utf-8", errors="replace")
    return str(content or "")


def _has_selector(html: str, selector: str) -> bool:
    if not html:
        return False
    soup = BeautifulSoup(html, "lxml")
    return bool(soup.select(selector))

"""Persistent, policy-bounded browser runtime for official issue pages."""

from __future__ import annotations

import fcntl
import hashlib
import math
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from .adapter_result import BLOCKED, CIRCUIT_OPEN, FETCH_FAILED, SUCCESS
from .diagnostics import record_fetch_attempt
from .official_snapshot import OfficialSnapshotStore


OFFICIAL_HOSTS = {
    "acs": frozenset({"pubs.acs.org"}),
    "aip": frozenset({"pubs.aip.org"}),
    "aps": frozenset({"journals.aps.org"}),
}

_ISSUE_PATHS = {
    "acs": re.compile(r"^/toc/[^/]+/[^/]+/[^/]+/?$"),
    "aip": re.compile(r"^/(?:aip/)?[^/]+/issue/[^/]+/[^/]+/?$"),
    "aps": re.compile(r"^/[^/]+/issues/[^/]+/[^/]+/?$"),
}
_CAPTURE_MEDIA_TYPES = frozenset(
    {"text/html", "application/json", "application/xml"}
)
# Playwright's sync API materializes body() before its size is known. This bound
# controls which already-read response bodies this runtime retains for parsing.
_MAX_RETAINED_CAPTURE_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True)
class IssueRequest:
    canonical_url: str
    required_selector: str
    challenge_markers: tuple[str, ...]
    capture_url_patterns: tuple[re.Pattern[str], ...] = ()


@dataclass(frozen=True)
class RecentRequest:
    """A canonical, paginated publisher recent-results page.

    Recent pages are auxiliary metadata sources only.  They never establish
    formal issue membership, so they intentionally have a separate request
    type from :class:`IssueRequest`.
    """

    canonical_url: str
    required_selector: str
    challenge_markers: tuple[str, ...]


@dataclass(frozen=True)
class CapturedResponse:
    url: str
    status_code: int
    media_type: str
    body: bytes


@dataclass(frozen=True)
class OfficialBrowserResult:
    outcome: str
    canonical_url: str
    final_url: str
    status_code: int | None
    html: str
    captured: tuple[CapturedResponse, ...]
    message: str = ""
    attempt_count: int = 1


@dataclass
class ProviderRunStats:
    attempted: int = 0
    live_success: int = 0
    snapshot_hit: int = 0
    blocked: int = 0
    rate_limited: int = 0
    fetch_failed: int = 0
    circuit_skipped: int = 0
    # Observational only: rejected cache material may still be repaired live.
    cache_rejected: int = 0
    candidate_attempted: int = 0
    terminal_resolved: int = 0
    unresolved_metadata: int = 0
    terminal_blocked: int = 0
    terminal_circuit_skipped: int = 0
    navigation_failure: int = 0
    parse_validation_failure: int = 0
    cache_failure: int = 0
    endpoint_success: int = 0
    endpoint_failure: int = 0
    terminal_endpoint_failure: int = 0
    terminal_other_failure: int = 0

    @property
    def terminal_accounted(self) -> int:
        """Number of candidates assigned exactly one terminal category."""
        return sum(
            (
                self.terminal_resolved,
                self.unresolved_metadata,
                self.terminal_blocked,
                self.terminal_circuit_skipped,
                self.navigation_failure,
                self.parse_validation_failure,
                self.cache_failure,
                self.terminal_endpoint_failure,
                self.terminal_other_failure,
            )
        )


class ProviderRuntime:
    """Own one persistent publisher profile and enforce its contact policy."""

    def __init__(
        self,
        provider: str,
        profile_root: str | os.PathLike,
        snapshot_root: str | os.PathLike,
        *,
        browser_user_data_dir: str | os.PathLike | None = None,
        browser_cdp_url: str = "",
        playwright_factory: Callable | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        issue_interval: float = 30.0,
        challenge_retry_delay: float = 60.0,
        max_consecutive_blocks: int = 3,
    ):
        if provider not in OFFICIAL_HOSTS:
            raise ValueError(f"unsupported official provider: {provider}")
        self._validate_interval("issue_interval", issue_interval)
        self._validate_interval("challenge_retry_delay", challenge_retry_delay)
        if issue_interval < 30:
            raise ValueError("issue_interval must be at least 30 seconds")
        if challenge_retry_delay < 60:
            raise ValueError(
                "challenge_retry_delay must be at least 60 seconds"
            )
        if max_consecutive_blocks < 1:
            raise ValueError("max_consecutive_blocks must be positive")

        self.provider = provider
        # ``profile_dir`` is PaperTrack-owned and holds only the exclusive
        # ownership lock.  An explicitly supplied browser profile may be a
        # user's normal Chrome data directory and must never be chmod'ed or
        # used as the lock location.
        self.profile_dir = Path(profile_root) / provider
        self.browser_user_data_dir = (
            Path(browser_user_data_dir)
            if browser_user_data_dir is not None
            else self.profile_dir
        )
        self.browser_cdp_url = browser_cdp_url.rstrip("/")
        self.snapshot_store = OfficialSnapshotStore(snapshot_root)
        self._playwright_factory = playwright_factory or _sync_playwright_factory
        self._clock = clock
        self._sleep = sleep
        self.issue_interval = float(issue_interval)
        self.challenge_retry_delay = float(challenge_retry_delay)
        self.max_consecutive_blocks = max_consecutive_blocks

        self.stats = ProviderRunStats()
        self.consecutive_blocks = 0
        self.consecutive_endpoint_denials = 0
        self._circuit_open = False
        self._owner_fd: int | None = None
        self._playwright_manager = None
        self._playwright = None
        self._context = None
        self._attached_over_cdp = False
        self._page = None
        self._last_issue_navigation_at: float | None = None
        self._same_origin_request_at: dict[tuple[str, str], float] = {}
        self._canonical_origin: tuple[str, str, int | None] | None = None
        self._capture_origin: tuple[str, str, int | None] | None = None
        self._capture_patterns: tuple[re.Pattern[str], ...] = ()
        self._captured_responses: list[CapturedResponse] = []
        self._response_evidence: list[dict] = []
        self._challenge_indicators: list[str] = []

    @property
    def captured_responses(self) -> tuple[CapturedResponse, ...]:
        return tuple(self._captured_responses)

    @property
    def circuit_open(self) -> bool:
        """Whether publisher contact is disabled for the rest of this run."""
        return self._circuit_open

    def __enter__(self):
        return self.open()

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def open(self):
        if self._context is not None:
            return self
        self._acquire_profile()
        try:
            manager = self._playwright_factory()
            self._playwright_manager = manager
            if hasattr(manager, "__enter__"):
                self._playwright = manager.__enter__()
            elif hasattr(manager, "start"):
                self._playwright = manager.start()
            else:
                self._playwright = manager
            if self.browser_cdp_url:
                browser = self._playwright.chromium.connect_over_cdp(
                    self.browser_cdp_url
                )
                if not browser.contexts:
                    raise RuntimeError("CDP browser has no usable context")
                self._context = browser.contexts[0]
                self._attached_over_cdp = True
                self._page = next(iter(self._context.pages), None)
                if self._page is None:
                    self._page = self._context.new_page()
            else:
                self._context = self._playwright.chromium.launch_persistent_context(
                    user_data_dir=str(self.browser_user_data_dir),
                    channel="chrome",
                    headless=False,
                )
                self._page = self._context.new_page()
            self._page.on("response", self._capture_response)
        except Exception:
            self.close()
            raise
        return self

    def close(self) -> None:
        context, self._context = self._context, None
        manager, self._playwright_manager = self._playwright_manager, None
        playwright, self._playwright = self._playwright, None
        self._page = None
        try:
            if context is not None and not self._attached_over_cdp:
                context.close()
        finally:
            try:
                if manager is not None and hasattr(manager, "__exit__"):
                    manager.__exit__(None, None, None)
                elif playwright is not None and hasattr(playwright, "stop"):
                    playwright.stop()
            finally:
                self._attached_over_cdp = False
                self._release_profile()

    def fetch_issue(self, request: IssueRequest) -> OfficialBrowserResult:
        self._validate_issue_url(request.canonical_url)
        if self._context is None or self._page is None:
            raise RuntimeError("provider runtime is not open")
        self._canonical_origin = None
        self._capture_origin = None
        self._capture_patterns = ()
        self._captured_responses = []
        self._response_evidence = []
        self._challenge_indicators = []
        if self._circuit_open:
            self.consecutive_blocks = 0
            self.stats.circuit_skipped += 1
            result = OfficialBrowserResult(
                outcome=CIRCUIT_OPEN,
                canonical_url=request.canonical_url,
                final_url=request.canonical_url,
                status_code=None,
                html="",
                captured=(),
                message="provider circuit is open after consecutive blocks",
                attempt_count=0,
            )
            self._record_result(result)
            return result

        self.stats.attempted += 1
        expected_origin = _origin(request.canonical_url)
        self._capture_origin = expected_origin
        self._capture_patterns = request.capture_url_patterns
        self._wait_for_issue_interval()

        status_code = None
        html = ""
        final_url = request.canonical_url
        attempt_count = 1
        selector_error = ""
        try:
            response = self._page.goto(
                request.canonical_url, wait_until="domcontentloaded"
            )
            status_code = getattr(response, "status", None)
            selector_error = self._wait_for_selector(request.required_selector)
            html, final_url = self._page.content(), self._page.url

            if not self._is_exact_final_issue_url(
                final_url, request.canonical_url
            ):
                self._captured_responses = []
                result = self._failed_result(
                    request,
                    final_url,
                    status_code,
                    "",
                    "issue navigation changed the canonical issue location",
                    attempt_count,
                )
                self._record_result(result, diagnostic_html=html)
                return result

            if self._is_challenge(request, html):
                self._sleep(self.challenge_retry_delay)
                attempt_count = 2
                self._wait_for_issue_interval()
                response = self._page.reload(wait_until="domcontentloaded")
                status_code = getattr(response, "status", status_code)
                selector_error = self._wait_for_selector(request.required_selector)
                html, final_url = self._page.content(), self._page.url

                if not self._is_exact_final_issue_url(
                    final_url, request.canonical_url
                ):
                    self._captured_responses = []
                    result = self._failed_result(
                        request,
                        final_url,
                        status_code,
                        "",
                        "issue navigation changed the canonical issue location",
                        attempt_count,
                    )
                    self._record_result(result, diagnostic_html=html)
                    return result

            if self._is_challenge(request, html):
                result = self._blocked_result(
                    request, final_url, status_code, html, attempt_count
                )
            elif status_code != 200:
                self._captured_responses = []
                result = self._failed_result(
                    request,
                    final_url,
                    status_code,
                    "",
                    f"official issue navigation returned HTTP {status_code}",
                    attempt_count,
                )
            elif selector_error:
                result = self._failed_result(
                    request,
                    final_url,
                    status_code,
                    html,
                    selector_error,
                    attempt_count,
                )
            else:
                self.consecutive_blocks = 0
                self._canonical_origin = expected_origin
                result = OfficialBrowserResult(
                    outcome=SUCCESS,
                    canonical_url=request.canonical_url,
                    final_url=final_url,
                    status_code=status_code,
                    html=html,
                    captured=tuple(self._captured_responses),
                    attempt_count=attempt_count,
                )
        except Exception as exc:
            try:
                html, final_url = self._page.content(), self._page.url
            except Exception:
                pass
            result = self._failed_result(
                request,
                final_url,
                status_code,
                html,
                str(exc),
                attempt_count,
            )

        self._record_result(result, diagnostic_html=html)
        return result

    def fetch_recent(self, request: RecentRequest) -> OfficialBrowserResult:
        """Fetch one APS recent-results page in the existing browser context.

        This deliberately shares the provider-wide navigation floor and
        circuit breaker with issue pages.  A recent page is never allowed to
        redirect to a different URL, which prevents a challenge/login page
        from being cached as metadata.
        """
        self._validate_recent_url(request.canonical_url)
        if self._context is None or self._page is None:
            raise RuntimeError("provider runtime is not open")
        self._canonical_origin = None
        self._capture_origin = None
        self._capture_patterns = ()
        self._captured_responses = []
        self._response_evidence = []
        self._challenge_indicators = []
        if self._circuit_open:
            self.stats.circuit_skipped += 1
            result = OfficialBrowserResult(
                outcome=CIRCUIT_OPEN, canonical_url=request.canonical_url,
                final_url=request.canonical_url, status_code=None, html="",
                captured=(), message="provider circuit is open after consecutive blocks",
                attempt_count=0,
            )
            self._record_recent_result(result)
            return result

        self.stats.attempted += 1
        self._wait_for_issue_interval()
        status_code = None
        html = ""
        final_url = request.canonical_url
        attempt_count = 1
        selector_error = ""
        try:
            response = self._page.goto(request.canonical_url, wait_until="domcontentloaded")
            status_code = getattr(response, "status", None)
            selector_error = self._wait_for_selector(request.required_selector)
            html, final_url = self._page.content(), self._page.url
            if final_url != request.canonical_url:
                result = self._failed_result(
                    request, final_url, status_code, "",
                    "recent navigation changed the canonical page location", attempt_count,
                )
            elif self._is_challenge(request, html):
                self._sleep(self.challenge_retry_delay)
                attempt_count = 2
                self._wait_for_issue_interval()
                response = self._page.reload(wait_until="domcontentloaded")
                status_code = getattr(response, "status", status_code)
                selector_error = self._wait_for_selector(request.required_selector)
                html, final_url = self._page.content(), self._page.url
                if final_url != request.canonical_url:
                    result = self._failed_result(
                        request, final_url, status_code, "",
                        "recent navigation changed the canonical page location", attempt_count,
                    )
                elif self._is_challenge(request, html):
                    result = self._blocked_result(
                        request, final_url, status_code, html, attempt_count
                    )
                elif status_code != 200:
                    result = self._failed_result(
                        request, final_url, status_code, "",
                        f"official recent navigation returned HTTP {status_code}", attempt_count,
                    )
                elif selector_error:
                    result = self._failed_result(
                        request, final_url, status_code, html, selector_error, attempt_count
                    )
                else:
                    self.consecutive_blocks = 0
                    result = OfficialBrowserResult(
                        outcome=SUCCESS, canonical_url=request.canonical_url,
                        final_url=final_url, status_code=status_code, html=html,
                        captured=(), attempt_count=attempt_count,
                    )
            elif status_code != 200:
                result = self._failed_result(
                    request, final_url, status_code, "",
                    f"official recent navigation returned HTTP {status_code}", attempt_count,
                )
            elif selector_error:
                result = self._failed_result(
                    request, final_url, status_code, html, selector_error, attempt_count
                )
            else:
                self.consecutive_blocks = 0
                result = OfficialBrowserResult(
                    outcome=SUCCESS, canonical_url=request.canonical_url,
                    final_url=final_url, status_code=status_code, html=html,
                    captured=(), attempt_count=attempt_count,
                )
        except Exception as exc:
            try:
                html, final_url = self._page.content(), self._page.url
            except Exception:
                pass
            result = self._failed_result(
                request, final_url, status_code, html, str(exc), attempt_count
            )
        self._record_recent_result(result, diagnostic_html=html)
        return result

    def request_same_origin(
        self,
        url: str,
        method: str,
        form: dict | None,
        min_interval: float,
    ) -> CapturedResponse:
        if self._context is None:
            raise RuntimeError("provider runtime is not open")
        if self._canonical_origin is None:
            raise RuntimeError("canonical issue origin has not been established")
        if self._circuit_open:
            raise RuntimeError("provider circuit is open; contact is disabled")
        self._validate_interval("min_interval", min_interval)
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or _origin(url) != self._canonical_origin
        ):
            raise ValueError("request URL must share the same canonical issue origin")

        normalized_method = method.upper()
        role = (normalized_method, parsed.path)
        now = self._clock()
        previous = self._same_origin_request_at.get(role)
        if previous is not None:
            delay = max(0.0, previous + min_interval - now)
            if delay:
                self._sleep(delay)
                now = self._clock()
        self._same_origin_request_at[role] = now

        kwargs = {"method": normalized_method}
        if form is not None:
            kwargs["form"] = form
        response = self._context.request.fetch(url, **kwargs)
        response_url = getattr(response, "url", url)
        response_parts = urlsplit(response_url)
        if (
            response_parts.scheme != "https"
            or response_parts.username is not None
            or response_parts.password is not None
            or _origin(response_url) != self._canonical_origin
        ):
            raise ValueError("response must remain on the exact origin over HTTPS")
        status_code = getattr(response, "status", 0)
        self._observe_same_origin_status(status_code)
        try:
            body = response.body()
        except Exception:
            if status_code not in {403, 429}:
                raise
            body = b""
        media_type = _media_type(getattr(response, "headers", {}))
        captured = CapturedResponse(
            url=response_url,
            status_code=status_code,
            media_type=media_type,
            body=body,
        )
        return captured

    def _observe_same_origin_status(self, status_code: int) -> None:
        if status_code == 200:
            self.consecutive_endpoint_denials = 0
            return
        if status_code not in {403, 429}:
            return
        self.consecutive_endpoint_denials += 1
        if status_code == 403:
            self.stats.blocked += 1
        else:
            self.stats.rate_limited += 1
        if self.consecutive_endpoint_denials >= self.max_consecutive_blocks:
            self._circuit_open = True

    def _wait_for_issue_interval(self) -> None:
        now = self._clock()
        if self._last_issue_navigation_at is not None:
            delay = max(
                0.0,
                self._last_issue_navigation_at + self.issue_interval - now,
            )
            if delay:
                self._sleep(delay)
                now = self._clock()
        self._last_issue_navigation_at = now

    def _wait_for_selector(self, selector: str) -> str:
        try:
            self._page.wait_for_selector(selector)
            return ""
        except Exception as exc:
            return str(exc)

    def _is_challenge(self, request: IssueRequest, html: str) -> bool:
        title = self._page.title()
        material = f"{title}\n{html}".lower()
        self._challenge_indicators = [
            marker
            for marker in request.challenge_markers
            if marker.lower() in material
        ]
        return bool(self._challenge_indicators)

    def _blocked_result(
        self, request, final_url, status_code, html, attempt_count
    ) -> OfficialBrowserResult:
        self.consecutive_blocks += 1
        self.stats.blocked += 1
        if self.consecutive_blocks >= self.max_consecutive_blocks:
            self._circuit_open = True
        return OfficialBrowserResult(
            outcome=BLOCKED,
            canonical_url=request.canonical_url,
            final_url=final_url,
            status_code=status_code,
            html="",
            captured=(),
            message="publisher challenge remained after one recovery reload",
            attempt_count=attempt_count,
        )

    def _failed_result(
        self, request, final_url, status_code, html, message, attempt_count
    ) -> OfficialBrowserResult:
        self.consecutive_blocks = 0
        self.stats.fetch_failed += 1
        return OfficialBrowserResult(
            outcome=FETCH_FAILED,
            canonical_url=request.canonical_url,
            final_url=final_url,
            status_code=status_code,
            html="",
            captured=(),
            message=message or "official issue browser fetch failed",
            attempt_count=attempt_count,
        )

    def _capture_response(self, response) -> None:
        try:
            resource_type = response.request.resource_type
            url = response.url
            if resource_type not in {"xhr", "fetch"}:
                return
            if self._capture_origin is None or _origin(url) != self._capture_origin:
                return
            selected = any(
                pattern.search(url) for pattern in self._capture_patterns
            )
            if selected:
                self._observe_same_origin_status(response.status)
            media_type = _media_type(response.headers)
            if media_type not in _CAPTURE_MEDIA_TYPES:
                return
            body = response.body()
            if len(body) > _MAX_RETAINED_CAPTURE_BYTES:
                return
            self._response_evidence.append(
                {
                    "url": url,
                    "status": response.status,
                    "media_type": media_type,
                    "content_hash": "sha256:" + hashlib.sha256(body).hexdigest(),
                }
            )
            if selected:
                self._captured_responses.append(
                    CapturedResponse(url, response.status, media_type, body)
                )
        except Exception:
            # Browser response events must never abort the authoritative page fetch.
            return

    def _validate_issue_url(self, url: str) -> None:
        parsed = urlsplit(url)
        exact_url = f"https://{parsed.hostname}{parsed.path}"
        valid = (
            parsed.scheme == "https"
            and parsed.hostname in OFFICIAL_HOSTS[self.provider]
            and parsed.port is None
            and parsed.username is None
            and parsed.password is None
            and not parsed.fragment
            and not parsed.query
            and bool(_ISSUE_PATHS[self.provider].fullmatch(parsed.path))
            and url == exact_url
        )
        if not valid:
            raise ValueError(f"{self.provider} requires an official issue URL")

    def _validate_recent_url(self, url: str) -> None:
        parsed = urlsplit(url)
        query = parsed.query
        valid = (
            self.provider == "aps"
            and parsed.scheme == "https"
            and parsed.hostname == "journals.aps.org"
            and parsed.port is None
            and parsed.username is None
            and parsed.password is None
            and not parsed.fragment
            and bool(re.fullmatch(r"/[^/]+/recent", parsed.path))
            and (not query or re.fullmatch(r"page=[1-9][0-9]*", query))
        )
        if not valid:
            raise ValueError("aps requires an official canonical recent URL")

    def _is_exact_final_issue_url(
        self, final_url: str, canonical_url: str
    ) -> bool:
        """Accept only the canonical issue location, without URL decorations."""
        return final_url == canonical_url

    def _acquire_profile(self) -> None:
        self.profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.profile_dir, 0o700)
        fd = os.open(self.profile_dir / ".owner.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise RuntimeError("provider profile is already owned") from exc
        self._owner_fd = fd

    def _release_profile(self) -> None:
        fd, self._owner_fd = self._owner_fd, None
        if fd is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _record_result(
        self, result: OfficialBrowserResult, *, diagnostic_html: str = ""
    ) -> None:
        journal, volume, issue = _issue_identity(
            self.provider, result.canonical_url
        )
        try:
            record_fetch_attempt(
                {
                    "source_role": "authoritative",
                    "source": "journal",
                    "provider": self.provider,
                    "journal": journal,
                    "volume": volume,
                    "issue": issue,
                    "url": result.canonical_url,
                    "final_url": result.final_url,
                    "stage": "issue_page_browser",
                    "outcome": result.outcome,
                    "http_status": result.status_code,
                    "client": "playwright",
                    "browser_mode": "headed_chrome",
                    "profile_mode": "persistent",
                    "challenge_indicators": list(self._challenge_indicators),
                    "attempt": result.attempt_count,
                    "attempt_count": result.attempt_count,
                    "circuit_state": "open" if self._circuit_open else "closed",
                    "circuit_open": self._circuit_open,
                    "message": result.message,
                    "captured_response_evidence": self._response_evidence,
                },
                html=diagnostic_html or result.html or None,
            )
        finally:
            if result.outcome != SUCCESS:
                self._captured_responses = []

    def _record_recent_result(
        self, result: OfficialBrowserResult, *, diagnostic_html: str = ""
    ) -> None:
        journal = urlsplit(result.canonical_url).path.strip("/").split("/", 1)[0]
        record_fetch_attempt(
            {
                "source_role": "authoritative_auxiliary",
                "source": "journal",
                "provider": self.provider,
                "journal": journal,
                "volume": "",
                "issue": "",
                "url": result.canonical_url,
                "final_url": result.final_url,
                "stage": "recent_page_browser",
                "outcome": result.outcome,
                "http_status": result.status_code,
                "client": "playwright",
                "browser_mode": "headed_chrome",
                "profile_mode": "persistent",
                "challenge_indicators": list(self._challenge_indicators),
                "attempt": result.attempt_count,
                "attempt_count": result.attempt_count,
                "circuit_state": "open" if self._circuit_open else "closed",
                "circuit_open": self._circuit_open,
                "message": result.message,
            },
            html=diagnostic_html or result.html or None,
        )

    @staticmethod
    def _validate_interval(name: str, value: float) -> None:
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"{name} must be finite and non-negative")


def _origin(url: str) -> tuple[str, str, int | None]:
    parsed = urlsplit(url)
    port = parsed.port
    if parsed.scheme == "https" and port == 443:
        port = None
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), port


def _issue_location(url: str) -> tuple[str, str, int | None, str]:
    parsed = urlsplit(url)
    return (*_origin(url), parsed.path)


def _issue_identity(provider: str, url: str) -> tuple[str, str, str]:
    parts = urlsplit(url).path.strip("/").split("/")
    if provider == "acs":
        _, journal, volume, issue = parts
    elif provider == "aip" and parts[0] == "aip":
        _, journal, _, volume, issue = parts
    elif provider == "aip":
        journal, _, volume, issue = parts
    else:
        journal, _, volume, issue = parts
    return journal, volume, issue


def _media_type(headers) -> str:
    value = ""
    if headers:
        value = headers.get("content-type") or headers.get("Content-Type") or ""
    return value.split(";", 1)[0].strip().lower()


def _sync_playwright_factory():
    from playwright.sync_api import sync_playwright

    return sync_playwright()

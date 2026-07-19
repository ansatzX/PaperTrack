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
_MAX_CAPTURE_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True)
class IssueRequest:
    canonical_url: str
    required_selector: str
    challenge_markers: tuple[str, ...]
    capture_url_patterns: tuple[re.Pattern[str], ...] = ()


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
    fetch_failed: int = 0
    circuit_skipped: int = 0


class ProviderRuntime:
    """Own one persistent publisher profile and enforce its contact policy."""

    def __init__(
        self,
        provider: str,
        profile_root: str | os.PathLike,
        snapshot_root: str | os.PathLike,
        *,
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
        self.profile_dir = Path(profile_root) / provider
        self.snapshot_store = OfficialSnapshotStore(snapshot_root)
        self._playwright_factory = playwright_factory or _sync_playwright_factory
        self._clock = clock
        self._sleep = sleep
        self.issue_interval = float(issue_interval)
        self.challenge_retry_delay = float(challenge_retry_delay)
        self.max_consecutive_blocks = max_consecutive_blocks

        self.stats = ProviderRunStats()
        self.consecutive_blocks = 0
        self._circuit_open = False
        self._owner_fd: int | None = None
        self._playwright_manager = None
        self._playwright = None
        self._context = None
        self._page = None
        self._last_issue_navigation_at: float | None = None
        self._same_origin_request_at: dict[tuple[str, str], float] = {}
        self._canonical_origin: tuple[str, str, int | None] | None = None
        self._capture_origin: tuple[str, str, int | None] | None = None
        self._capture_patterns: tuple[re.Pattern[str], ...] = ()
        self._captured_responses: list[CapturedResponse] = []
        self._response_evidence: list[dict] = []

    @property
    def captured_responses(self) -> tuple[CapturedResponse, ...]:
        return tuple(self._captured_responses)

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
            self._context = self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir),
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
            if context is not None:
                context.close()
        finally:
            try:
                if manager is not None and hasattr(manager, "__exit__"):
                    manager.__exit__(None, None, None)
                elif playwright is not None and hasattr(playwright, "stop"):
                    playwright.stop()
            finally:
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
            response = self._page.goto(request.canonical_url)
            status_code = getattr(response, "status", None)
            selector_error = self._wait_for_selector(request.required_selector)
            html, final_url = self._page.content(), self._page.url

            if self._is_challenge(request, html):
                self._sleep(self.challenge_retry_delay)
                attempt_count = 2
                self._wait_for_issue_interval()
                response = self._page.reload()
                status_code = getattr(response, "status", status_code)
                selector_error = self._wait_for_selector(request.required_selector)
                html, final_url = self._page.content(), self._page.url

            if self._is_challenge(request, html):
                result = self._blocked_result(
                    request, final_url, status_code, html, attempt_count
                )
            elif _origin(final_url) != expected_origin:
                result = self._failed_result(
                    request,
                    final_url,
                    status_code,
                    html,
                    "issue navigation left the canonical origin",
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
                self.stats.live_success += 1
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

        self._record_result(result)
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
        body = response.body()
        media_type = _media_type(getattr(response, "headers", {}))
        return CapturedResponse(
            url=response_url,
            status_code=getattr(response, "status", 0),
            media_type=media_type,
            body=body,
        )

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
        return any(marker.lower() in material for marker in request.challenge_markers)

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
            html=html,
            captured=tuple(self._captured_responses),
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
            html=html,
            captured=tuple(self._captured_responses),
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
            media_type = _media_type(response.headers)
            if media_type not in _CAPTURE_MEDIA_TYPES:
                return
            body = response.body()
            if len(body) > _MAX_CAPTURE_BYTES:
                return
            self._response_evidence.append(
                {
                    "url": url,
                    "status": response.status,
                    "media_type": media_type,
                    "content_hash": "sha256:" + hashlib.sha256(body).hexdigest(),
                }
            )
            if any(pattern.search(url) for pattern in self._capture_patterns):
                self._captured_responses.append(
                    CapturedResponse(url, response.status, media_type, body)
                )
        except Exception:
            # Browser response events must never abort the authoritative page fetch.
            return

    def _validate_issue_url(self, url: str) -> None:
        parsed = urlsplit(url)
        valid = (
            parsed.scheme == "https"
            and parsed.hostname in OFFICIAL_HOSTS[self.provider]
            and parsed.port in {None, 443}
            and parsed.username is None
            and parsed.password is None
            and not parsed.fragment
            and not parsed.query
            and bool(_ISSUE_PATHS[self.provider].fullmatch(parsed.path))
        )
        if not valid:
            raise ValueError(f"{self.provider} requires an official issue URL")

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

    def _record_result(self, result: OfficialBrowserResult) -> None:
        record_fetch_attempt(
            {
                "source_role": "authoritative",
                "source": "journal",
                "provider": self.provider,
                "url": result.canonical_url,
                "final_url": result.final_url,
                "stage": "issue_page_browser",
                "outcome": result.outcome,
                "http_status": result.status_code,
                "attempt_count": result.attempt_count,
                "message": result.message,
                "captured_response_evidence": self._response_evidence,
            },
            html=result.html or None,
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


def _media_type(headers) -> str:
    value = ""
    if headers:
        value = headers.get("content-type") or headers.get("Content-Type") or ""
    return value.split(";", 1)[0].strip().lower()


def _sync_playwright_factory():
    from playwright.sync_api import sync_playwright

    return sync_playwright()

"""Launch and attach to a user-visible local Chrome CDP session."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import time
from urllib.error import URLError
from urllib.request import urlopen


def cdp_is_available(url: str, timeout: float = 1.0) -> bool:
    """Return whether a local DevTools endpoint is accepting connections."""
    try:
        with urlopen(f"{url.rstrip('/')}/json/version", timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, ValueError, UnicodeError):
        return False
    return isinstance(payload.get("webSocketDebuggerUrl"), str)


def launch_chrome_for_cdp(
    chrome_bin: str,
    user_data_dir: Path,
    cdp_url: str,
    *,
    wait_seconds: float = 10.0,
) -> None:
    """Start an isolated visible Chrome profile with loopback-only DevTools.

    The profile must not be Chrome's default user-data directory: current Chrome
    intentionally disables remote debugging for that directory.
    """
    if cdp_is_available(cdp_url):
        return
    if user_data_dir == Path.home() / ".config" / "google-chrome":
        raise ValueError("Chrome CDP requires a non-default user-data directory")
    port = _cdp_port(cdp_url)
    user_data_dir.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(
        [
            chrome_bin,
            f"--user-data-dir={user_data_dir}",
            "--remote-debugging-address=127.0.0.1",
            f"--remote-debugging-port={port}",
        ],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if cdp_is_available(cdp_url):
            return
        time.sleep(0.2)
    raise RuntimeError(f"Chrome CDP endpoint did not become ready: {cdp_url}")


def _cdp_port(url: str) -> int:
    prefix = "http://127.0.0.1:"
    if not url.startswith(prefix):
        raise ValueError("Chrome CDP URL must be loopback http://127.0.0.1:<port>")
    try:
        port = int(url[len(prefix):].rstrip("/"))
    except ValueError as exc:
        raise ValueError("Chrome CDP URL requires a numeric port") from exc
    if not 1 <= port <= 65535:
        raise ValueError("Chrome CDP port is out of range")
    return port

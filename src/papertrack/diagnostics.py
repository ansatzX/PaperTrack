"""Structured production diagnostics for source fetches and maintenance flows."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ATTEMPTS_FILE = "attempts.jsonl"
MIGRATIONS_FILE = "migrations.jsonl"
REPAIRS_FILE = "repairs.jsonl"

ABNORMAL_OUTCOMES = {
    "blocked",
    "rate_limited",
    "fetch_failed",
    "parse_failed",
    "official_empty_suspicious",
    "official_field_absent",
    "partial",
}


@dataclass
class DiagnosticsConfig:
    diagnostics_dir: str | None = None
    all_attempts: bool = False
    failure_snapshots: bool = True


_config = DiagnosticsConfig()


def configure_diagnostics(
    diagnostics_dir: str | None = None,
    all_attempts: bool | None = None,
    failure_snapshots: bool | None = None,
) -> None:
    """Configure process-wide diagnostics behavior."""
    _config.diagnostics_dir = diagnostics_dir
    _config.all_attempts = bool(all_attempts)
    _config.failure_snapshots = True if failure_snapshots is None else failure_snapshots


def diagnostics_dir() -> Path:
    configured = _config.diagnostics_dir or os.environ.get("PAPERTRACK_DIAGNOSTICS_DIR")
    return Path(configured or ".papertrack_diagnostics")


def record_all_attempts() -> bool:
    if _config.all_attempts:
        return True
    return os.environ.get("PAPERTRACK_DIAGNOSTICS_ALL_ATTEMPTS", "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def save_failure_snapshots() -> bool:
    env = os.environ.get("PAPERTRACK_NO_FAILURE_SNAPSHOTS", "").lower()
    if env in {"1", "true", "yes", "on"}:
        return False
    return _config.failure_snapshots


def record_fetch_attempt(event: dict[str, Any], html: str | None = None) -> None:
    """Record a source-contact attempt if policy says it is useful."""
    event = dict(event)
    event.setdefault("event_type", "fetch_attempt")
    event.setdefault("time", _now_timestamp())

    outcome = event.get("outcome", "")
    fallback_used = bool(event.get("fallback_used"))
    should_record = record_all_attempts() or outcome in ABNORMAL_OUTCOMES or fallback_used
    if not should_record:
        return

    if html is not None and save_failure_snapshots() and outcome in ABNORMAL_OUTCOMES:
        snapshot_rel = _write_snapshot(event, html)
        event["snapshot_path"] = snapshot_rel
        event["content_hash"] = _content_hash(html)

    _append_jsonl(ATTEMPTS_FILE, event)


def record_migration_event(event: dict[str, Any]) -> None:
    event = dict(event)
    event.setdefault("event_type", "migration")
    event.setdefault("time", _now_timestamp())
    _append_jsonl(MIGRATIONS_FILE, event)


def record_repair_event(event: dict[str, Any]) -> None:
    event = dict(event)
    event.setdefault("event_type", "repair")
    event.setdefault("time", _now_timestamp())
    _append_jsonl(REPAIRS_FILE, event)


def _append_jsonl(filename: str, event: dict[str, Any]) -> None:
    root = diagnostics_dir()
    root.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(event, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    fd = os.open(root / filename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        written = os.write(fd, payload)
        if written != len(payload):
            raise OSError(
                f"short diagnostics append: wrote {written} of {len(payload)} bytes"
            )
    finally:
        os.close(fd)


def _write_snapshot(event: dict[str, Any], html: str) -> str:
    root = diagnostics_dir()
    provider = _safe_part(str(event.get("provider") or event.get("source") or "unknown"))
    snapshot_dir = root / "snapshots" / provider
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    parts = [
        _now_compact(),
        event.get("journal") or event.get("source") or "source",
        event.get("volume") or "",
        event.get("issue") or "",
        event.get("stage") or event.get("outcome") or "attempt",
    ]
    filename = "-".join(_safe_part(str(p)) for p in parts if p) + ".html"
    path = snapshot_dir / filename
    path.write_text(html, encoding="utf-8")
    return str(path.relative_to(root))


def _content_hash(content: str) -> str:
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _safe_part(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9_.-]+", "-", value)
    return value.strip("-") or "unknown"


def _now_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _now_compact() -> str:
    return time.strftime("%Y%m%d-%H%M%S")

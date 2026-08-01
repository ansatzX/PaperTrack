#!/usr/bin/env python3
"""Rebuild APS reports in separate issue and recent browser phases.

This is a deliberate migration tool, not the ordinary APS latest workflow.
Historical APS reports stored issue-page teasers in their Abstract fields.  The
tool first refreshes formal issue snapshots for every existing APS report, then
in a second browser phase reuses the cached issue snapshots while recent pages
fill only exact DOI matches.  Markdown is not replaced until that second phase.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import tempfile
from typing import Any

from papertrack.adapter_result import SUCCESS
from papertrack.aps_fetch import query_aps_issue_result
from papertrack.codex import journal_configs
from papertrack.journal_rebuild import _existing_collected_dois
from papertrack.provider_runtime import ProviderRuntime
from papertrack.report import filter_journal_to_md
from papertrack.source_policy import SourcePolicy


LOGGER = logging.getLogger(__name__)
SCHEMA_VERSION = 1


def _candidates(data_dir: Path) -> list[tuple[str, str]]:
    root = data_dir / "aps" / "prl"
    candidates = []
    for report in sorted(root.glob("*/*.md"), key=lambda path: (
        int(path.parent.name), int(path.stem)
    )):
        candidates.append((report.parent.name, report.stem))
    return candidates


def _state_path(diagnostics_dir: Path) -> Path:
    return diagnostics_dir / "aps_full_repair_state.json"


def _load_state(path: Path, candidates: list[tuple[str, str]]) -> dict[str, Any]:
    keys = [f"{volume}/{issue}" for volume, issue in candidates]
    if not path.exists():
        return {
            "schema_version": SCHEMA_VERSION,
            "provider": "aps",
            "issues": {key: {"issue": "pending", "render": "pending"} for key in keys},
        }
    state = json.loads(path.read_text(encoding="utf-8"))
    if (
        state.get("schema_version") != SCHEMA_VERSION
        or state.get("provider") != "aps"
        or not isinstance(state.get("issues"), dict)
    ):
        raise ValueError(f"invalid APS repair state: {path}")
    for key in keys:
        state["issues"].setdefault(key, {"issue": "pending", "render": "pending"})
    return state


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(state, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _issue_stage(
    candidates: list[tuple[str, str]], state: dict[str, Any], state_path: Path,
    profile_root: Path, snapshot_root: Path,
) -> None:
    config = journal_configs["prl"]
    with ProviderRuntime("aps", profile_root, snapshot_root) as runtime:
        for volume, issue in candidates:
            record = state["issues"][f"{volume}/{issue}"]
            if record["issue"] == "ready":
                continue
            result = query_aps_issue_result(
                "prl", volume, issue, config["name"], runtime,
            )
            record["issue"] = "ready" if result.outcome == SUCCESS else result.outcome
            record["issue_message"] = result.message
            _write_state(state_path, state)


def _render_stage(
    candidates: list[tuple[str, str]], state: dict[str, Any], state_path: Path,
    data_dir: Path, profile_root: Path, snapshot_root: Path,
) -> None:
    config = journal_configs["prl"]
    output_root = data_dir / "aps"
    with ProviderRuntime("aps", profile_root, snapshot_root) as runtime:
        for volume, issue in candidates:
            record = state["issues"][f"{volume}/{issue}"]
            if record["issue"] != "ready" or record["render"] == "complete":
                continue
            report = output_root / "prl" / volume / f"{issue}.md"
            try:
                old = report.read_text(encoding="utf-8") if report.exists() else ""
                collected = _existing_collected_dois(old)
                success = filter_journal_to_md(
                    journal_name=config["name"], journal_slug="prl",
                    volume=volume, issue=issue, issn=config["issn"],
                    md_folder=str(output_root), zotero=None, provider="aps",
                    source_policy=SourcePolicy.from_environment(),
                    existing_collected_dois=collected, provider_runtime=runtime,
                )
            except Exception as exc:
                record["render"] = "failed"
                record["render_message"] = str(exc)
            else:
                coverage = getattr(runtime, "last_source_metadata", {}).get(
                    "official_recent_abstract_fill", {}
                )
                remaining = coverage.get("remaining", 0)
                record["render"] = (
                    "complete" if success and not remaining
                    else "unresolved" if success
                    else "failed"
                )
                record["render_message"] = getattr(runtime, "last_source_message", "")
                record["remaining_abstracts"] = remaining
            _write_state(state_path, state)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--diagnostics-dir", required=True, type=Path)
    parser.add_argument("--profile-root", required=True, type=Path)
    parser.add_argument("--snapshot-root", required=True, type=Path)
    parser.add_argument("--phase", choices=("all", "issue", "render"), default="all")
    args = parser.parse_args()
    candidates = _candidates(args.data_dir)
    state_path = _state_path(args.diagnostics_dir)
    state = _load_state(state_path, candidates)
    _write_state(state_path, state)
    if args.phase in {"all", "issue"}:
        _issue_stage(candidates, state, state_path, args.profile_root, args.snapshot_root)
    if args.phase == "all":
        state = _load_state(state_path, candidates)
    if args.phase in {"all", "render"}:
        _render_stage(candidates, state, state_path, args.data_dir, args.profile_root, args.snapshot_root)
    print(json.dumps(state, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

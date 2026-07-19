import json
from pathlib import Path

import pytest

from papertrack import acs_fetch, report
from papertrack.adapter_result import (
    AdapterResult,
    BLOCKED,
    CIRCUIT_OPEN,
    SUCCESS,
)
from papertrack.journal_rebuild import (
    FETCH_FAILED,
    RebuildCandidate,
    _load_progress,
    _progress_path,
    run_provider_rebuild,
)
from papertrack.official_snapshot import (
    OfficialSnapshotStore,
    SnapshotKey,
    SnapshotLoadResult,
    SnapshotValidation,
)
from papertrack.provider_runtime import OfficialBrowserResult, ProviderRunStats
from scripts import verify_official_issue_runtime as verifier


ACS_HTML = """
<html><body>
  <div class="issue-item clearfix">
    <h3 class="issue-item_title">
      <a href="/doi/10.1021/acs.jcim.6c00001">Research article</a>
    </h3>
    <span class="hlFld-ContribAuthor"><given-names>Ada</given-names><surname>Lovelace</surname></span>
    <span class="hlFld-Abstract">Official abstract.</span>
    <div class="issue-item_info">
      <span class="issue-item_vol-num">66</span>, 12, 100-110 Publication Date: June 1, 2026
    </div>
  </div>
</body></html>
"""


def _candidate(issue: str) -> RebuildCandidate:
    return RebuildCandidate(
        provider="acs",
        journal="jcim",
        volume="66",
        issue=issue,
        relative_path=f"acs/jcim/66/{issue}.md",
        classification="must_rebuild",
        priority=10,
        reasons=("ordinary_article_missing_abstract",),
    )


def _seed_snapshot(store: OfficialSnapshotStore):
    return store.commit(
        SnapshotKey("acs", "jcisd8", "66", "12"),
        "https://pubs.acs.org/toc/jcisd8/66/12",
        "https://pubs.acs.org/toc/jcisd8/66/12",
        "text/html",
        ACS_HTML.encode(),
        SnapshotValidation(
            selector_counts={"div.issue-item.clearfix": 1},
            membership_dois=("10.1021/acs.jcim.6c00001",),
            parser_version="acs-toc-v1",
        ),
    )


def _seed_report(path: Path) -> None:
    path.parent.mkdir(parents=True)
    path.write_text(
        """---
papertrack_format: journal-v2
papertrack_source: journal
---
## collected

### 10.1021/acs.jcim.6c00001

- [x] [10.1021/acs.jcim.6c00001](https://doi.org/10.1021/acs.jcim.6c00001) keep

Title: Old title

Authors: Old author

Abstract:
> [!quote]- Abstract
> Old abstract

## not collected
""",
        encoding="utf-8",
    )


class _BlockedRuntime:
    instance = None

    def __init__(self, provider, profile_root, snapshot_root):
        self.provider = provider
        self.snapshot_store = OfficialSnapshotStore(snapshot_root)
        self.stats = ProviderRunStats()
        self.calls = 0
        self.__class__.instance = self

        # Simulate a cache lookup miss without deleting the prior durable bytes.
        self.snapshot_store.load_result = lambda key: SnapshotLoadResult(
            snapshot=None,
            status="absent",
            reason="manifest_absent",
            cache_snapshot_path=self.snapshot_store._manifest_path(key),
        )

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def fetch_issue(self, request):
        self.calls += 1
        if self.calls <= 3:
            outcome = BLOCKED
            self.stats.attempted += 1
            self.stats.blocked += 1
        else:
            outcome = CIRCUIT_OPEN
            self.stats.circuit_skipped += 1
        return OfficialBrowserResult(
            outcome=outcome,
            canonical_url=request.canonical_url,
            final_url=request.canonical_url,
            status_code=403 if outcome == BLOCKED else None,
            html="challenge" if outcome == BLOCKED else "",
            captured=(),
            message="publisher challenge" if outcome == BLOCKED else "circuit open",
            attempt_count=2 if outcome == BLOCKED else 0,
        )


def test_blocked_live_fetch_preserves_snapshot_report_and_progress(tmp_path):
    acs_fetch._cache.clear()
    data_dir = tmp_path / "data"
    diagnostics_dir = tmp_path / "diagnostics"
    snapshot_root = tmp_path / "snapshots"
    report_path = data_dir / "acs/jcim/66/12.md"
    _seed_report(report_path)
    snapshot = _seed_snapshot(OfficialSnapshotStore(snapshot_root))
    before_report = report_path.read_bytes()
    before_snapshot = snapshot.body_path.read_bytes()
    candidates = tuple(_candidate(issue) for issue in ("12", "13", "14", "15"))

    summary = run_provider_rebuild(
        "acs",
        candidates,
        data_dir=data_dir,
        diagnostics_dir=diagnostics_dir,
        profile_root=tmp_path / "profiles",
        snapshot_root=snapshot_root,
        runtime_factory=_BlockedRuntime,
    )

    assert report_path.read_bytes() == before_report
    assert snapshot.body_path.read_bytes() == before_snapshot
    assert summary.runtime_stats.blocked == 3
    assert summary.runtime_stats.circuit_skipped == 1
    progress = _load_progress(_progress_path(diagnostics_dir, "acs"), "acs")
    assert progress.issues[candidates[0].key].outcome == FETCH_FAILED
    assert progress.issues[candidates[-1].key].outcome == FETCH_FAILED


class _SuccessRuntime:
    events = None

    def __init__(self, provider, profile_root, snapshot_root):
        self.snapshot_store = OfficialSnapshotStore(snapshot_root)
        self.stats = ProviderRunStats(attempted=1)
        original_commit = self.snapshot_store.commit

        def commit(*args, **kwargs):
            stored = original_commit(*args, **kwargs)
            self.events.append("snapshot_committed")
            return stored

        self.snapshot_store.commit = commit

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def fetch_issue(self, request):
        return OfficialBrowserResult(
            outcome=SUCCESS,
            canonical_url=request.canonical_url,
            final_url=request.canonical_url,
            status_code=200,
            html=ACS_HTML,
            captured=(),
        )


def test_validated_snapshot_commit_precedes_report_replacement_and_keeps_checkbox(
    tmp_path, monkeypatch
):
    acs_fetch._cache.clear()
    data_dir = tmp_path / "data"
    report_path = data_dir / "acs/jcim/66/12.md"
    _seed_report(report_path)
    events = []
    _SuccessRuntime.events = events
    original_write = report.write_report_file

    def observed_write(path, content, source):
        events.append("markdown_replaced")
        return original_write(path, content, source)

    monkeypatch.setattr(report, "write_report_file", observed_write)

    summary = run_provider_rebuild(
        "acs",
        (_candidate("12"),),
        data_dir=data_dir,
        diagnostics_dir=tmp_path / "diagnostics",
        profile_root=tmp_path / "profiles",
        snapshot_root=tmp_path / "snapshots",
        runtime_factory=_SuccessRuntime,
    )

    assert summary.ok
    assert events == ["snapshot_committed", "markdown_replaced"]
    content = report_path.read_text(encoding="utf-8")
    assert "- [x] [10.1021/acs.jcim.6c00001]" in content
    assert "Official abstract." in content


class _VerifierRuntime:
    def __init__(self, provider, profile_root, snapshot_root):
        self.provider = provider
        self.profile_root = Path(profile_root)
        self.snapshot_root = Path(snapshot_root)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


def test_verifier_checks_one_official_issue_and_does_not_write_markdown_by_default(
    tmp_path, capsys
):
    calls = []

    def fetch(scope, runtime):
        calls.append((scope, runtime))
        return AdapterResult(
            entries={"10.1021/example": object()},
            outcome=SUCCESS,
            provider="acs",
            source="journal",
            metadata={
                "official_transport": "live",
                "selector_counts": {"div.issue-item.clearfix": 1},
                "snapshot_path": str(tmp_path / "snapshots/body"),
            },
        )

    report_calls = []
    exit_code = verifier.main(
        [
            "--provider", "acs",
            "--journal", "jcim",
            "--volume", "66",
            "--issue", "12",
            "--profile-root", str(tmp_path / "profiles"),
            "--snapshot-root", str(tmp_path / "snapshots"),
            "--diagnostics-dir", str(tmp_path / "diagnostics"),
        ],
        runtime_factory=_VerifierRuntime,
        fetch_issue=fetch,
        write_report=lambda *args, **kwargs: report_calls.append((args, kwargs)),
    )

    assert exit_code == 0
    assert len(calls) == 1
    assert report_calls == []
    assert calls[0][0].key == "acs/jcim/66/12"
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "cache_commit_status": "committed",
        "diagnostic_path": str(tmp_path / "diagnostics/attempts.jsonl"),
        "issue_key": "acs/jcim/66/12",
        "membership_count": 1,
        "outcome": "success",
        "provider": "acs",
        "selector_counts": {"div.issue-item.clearfix": 1},
        "transport": "live",
    }


def test_verifier_rejects_repeated_or_provider_mismatched_scope(tmp_path):
    common = [
        "--journal", "jcim",
        "--volume", "66",
        "--issue", "12",
        "--profile-root", str(tmp_path / "profiles"),
        "--snapshot-root", str(tmp_path / "snapshots"),
        "--diagnostics-dir", str(tmp_path / "diagnostics"),
    ]
    with pytest.raises(SystemExit):
        verifier.main(["--provider", "acs", "--provider", "aps", *common])
    with pytest.raises(SystemExit):
        verifier.main(["--provider", "aps", *common])

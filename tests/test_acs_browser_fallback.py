from dataclasses import replace

import pytest

from papertrack import acs_fetch
from papertrack.adapter_result import (
    BLOCKED,
    FETCH_FAILED,
    OFFICIAL_EMPTY_SUSPICIOUS,
    PARSE_FAILED,
    SUCCESS,
)
from papertrack.official_snapshot import (
    OfficialSnapshotStore,
    SnapshotKey,
    SnapshotValidation,
)
from papertrack.provider_runtime import OfficialBrowserResult


ACS_FIXTURE = b"""
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


class Runtime:
    def __init__(self, snapshot_store):
        self.snapshot_store = snapshot_store
        self.fetch_calls = []
        self.results = []

    def queue_html(self, html, *, outcome=SUCCESS, message=""):
        if isinstance(html, bytes):
            html = html.decode("utf-8")
        self.results.append(
            OfficialBrowserResult(
                outcome=outcome,
                canonical_url="https://pubs.acs.org/toc/jcisd8/66/12",
                final_url="https://pubs.acs.org/toc/jcisd8/66/12",
                status_code=200,
                html=html,
                captured=(),
                message=message,
            )
        )

    def fetch_issue(self, request):
        self.fetch_calls.append(request)
        return self.results.pop(0)


@pytest.fixture
def snapshot_store(tmp_path):
    return OfficialSnapshotStore(tmp_path / "snapshots")


@pytest.fixture
def runtime(snapshot_store):
    return Runtime(snapshot_store)


def seed_acs_snapshot(snapshot_store, body=ACS_FIXTURE):
    key = SnapshotKey("acs", "jcisd8", "66", "12")
    return snapshot_store.commit(
        key,
        "https://pubs.acs.org/toc/jcisd8/66/12",
        "https://pubs.acs.org/toc/jcisd8/66/12",
        "text/html",
        body,
        SnapshotValidation(
            selector_counts={"div.issue-item.clearfix": 1},
            membership_dois=("10.1021/acs.jcim.6c00001",),
            parser_version="acs-toc-v1",
        ),
    )


def test_acs_uses_valid_snapshot_without_network(runtime, snapshot_store):
    seed_acs_snapshot(snapshot_store)

    result = acs_fetch.query_acs_issue_result(
        "jcisd8", "66", "12", "JCIM", runtime
    )

    assert result.outcome == SUCCESS
    assert result.metadata["official_transport"] == "snapshot"
    assert runtime.fetch_calls == []


def test_acs_reparses_snapshot_and_rejects_changed_membership(
    runtime, snapshot_store
):
    stored = seed_acs_snapshot(snapshot_store)
    mismatched = replace(
        stored,
        membership_hash="0" * 64,
    )
    original_load = snapshot_store.load
    loads = iter((mismatched,))
    snapshot_store.load = lambda key: next(loads, original_load(key))
    runtime.queue_html(ACS_FIXTURE)

    result = acs_fetch.query_acs_issue_result(
        "jcisd8", "66", "12", "JCIM", runtime
    )

    assert result.outcome == SUCCESS
    assert result.metadata["official_transport"] == "live"
    assert len(runtime.fetch_calls) == 1


def test_acs_live_success_commits_validated_snapshot(runtime, snapshot_store):
    runtime.queue_html(ACS_FIXTURE)

    result = acs_fetch.query_acs_issue_result(
        "jcisd8", "66", "12", "JCIM", runtime
    )

    stored = snapshot_store.load(SnapshotKey("acs", "jcisd8", "66", "12"))
    assert result.outcome == SUCCESS
    assert stored is not None
    assert stored.validation.membership_dois == tuple(sorted(result.entries))
    assert result.metadata["official_transport"] == "live"
    assert result.metadata["snapshot_path"] == str(stored.body_path)
    assert result.metadata["selector_counts"] == {
        "div.issue-item.clearfix": 1
    }
    assert result.metadata["membership_hash"] == stored.membership_hash
    request = runtime.fetch_calls[0]
    assert request.canonical_url == "https://pubs.acs.org/toc/jcisd8/66/12"
    assert request.required_selector == "div.issue-item.clearfix"
    assert request.challenge_markers == (
        "Just a moment",
        "cf-chl",
        "Access denied",
    )


@pytest.mark.parametrize(
    ("html", "runtime_outcome", "expected_outcome"),
    [
        ("<title>Just a moment</title>", BLOCKED, BLOCKED),
        ("<html>No cards</html>", SUCCESS, OFFICIAL_EMPTY_SUSPICIOUS),
        (
            '<div class="issue-item clearfix"><h3 class="issue-item_title"><a href="/article/no-doi">Title</a></h3></div>',
            SUCCESS,
            PARSE_FAILED,
        ),
    ],
)
def test_acs_invalid_live_result_never_replaces_existing_snapshot(
    snapshot_store, html, runtime_outcome, expected_outcome
):
    old = seed_acs_snapshot(snapshot_store)
    runtime = Runtime(snapshot_store)
    runtime.queue_html(html, outcome=runtime_outcome)
    original_load = snapshot_store.load
    first = True

    def miss_once(key):
        nonlocal first
        if first:
            first = False
            return None
        return original_load(key)

    snapshot_store.load = miss_once

    result = acs_fetch.query_acs_issue_result(
        "jcisd8", "66", "12", "JCIM", runtime
    )

    snapshot_store.load = original_load
    preserved = snapshot_store.load(SnapshotKey("acs", "jcisd8", "66", "12"))
    assert result.outcome == expected_outcome
    assert preserved is not None
    assert preserved.content_hash == old.content_hash


def test_acs_parser_exception_never_replaces_existing_snapshot(
    monkeypatch, snapshot_store
):
    old = seed_acs_snapshot(snapshot_store)
    runtime = Runtime(snapshot_store)
    runtime.queue_html(ACS_FIXTURE)
    original_load = snapshot_store.load
    snapshot_store.load = lambda key: None
    monkeypatch.setattr(
        acs_fetch,
        "_parse_acs_toc_result",
        lambda *args: (_ for _ in ()).throw(ValueError("broken parser")),
    )

    result = acs_fetch.query_acs_issue_result(
        "jcisd8", "66", "12", "JCIM", runtime
    )

    snapshot_store.load = original_load
    preserved = snapshot_store.load(SnapshotKey("acs", "jcisd8", "66", "12"))
    assert result.outcome == PARSE_FAILED
    assert preserved is not None
    assert preserved.content_hash == old.content_hash


def test_acs_fetch_failure_is_reported(runtime):
    runtime.queue_html("", outcome=FETCH_FAILED, message="browser failed")

    result = acs_fetch.query_acs_issue_result(
        "jcisd8", "66", "12", "JCIM", runtime
    )

    assert result.outcome == FETCH_FAILED
    assert result.message == "browser failed"

import json

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
    SnapshotAttachment,
    SnapshotKey,
    SnapshotLoadResult,
    SnapshotValidation,
)
from papertrack.provider_runtime import OfficialBrowserResult, ProviderRunStats


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
        self.stats = ProviderRunStats()

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


def seed_acs_snapshot(
    snapshot_store,
    body=ACS_FIXTURE,
    *,
    canonical_url="https://pubs.acs.org/toc/jcisd8/66/12",
    final_url="https://pubs.acs.org/toc/jcisd8/66/12",
    media_type="text/html",
    attachments=(),
):
    key = SnapshotKey("acs", "jcisd8", "66", "12")
    return snapshot_store.commit(
        key,
        canonical_url,
        final_url,
        media_type,
        body,
        SnapshotValidation(
            selector_counts={"div.issue-item.clearfix": 1},
            membership_dois=("10.1021/acs.jcim.6c00001",),
            parser_version="acs-toc-v1",
        ),
        attachments,
    )


def test_acs_uses_valid_snapshot_without_network(runtime, snapshot_store):
    seed_acs_snapshot(
        snapshot_store,
        final_url="https://pubs.acs.org/toc/jcisd8/66/12?view=expanded",
    )

    result = acs_fetch.query_acs_issue_result(
        "jcisd8", "66", "12", "JCIM", runtime
    )

    assert result.outcome == SUCCESS
    assert result.metadata["official_transport"] == "snapshot"
    assert runtime.fetch_calls == []
    assert runtime.stats.snapshot_hit == 1


def test_acs_reparses_snapshot_and_rejects_changed_membership(
    monkeypatch, runtime, snapshot_store
):
    seed_acs_snapshot(snapshot_store)
    manifest_path = (
        snapshot_store.root
        / "acs" / "jcisd8" / "66" / "12" / "manifest.json"
    )
    manifest = json.loads(manifest_path.read_text())
    manifest["membership_hash"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest))
    runtime.queue_html(ACS_FIXTURE)
    events = []
    monkeypatch.setattr(
        acs_fetch,
        "record_fetch_attempt",
        lambda event, html=None: events.append(event),
    )

    result = acs_fetch.query_acs_issue_result(
        "jcisd8", "66", "12", "JCIM", runtime
    )

    assert result.outcome == SUCCESS
    assert result.metadata["official_transport"] == "live"
    assert len(runtime.fetch_calls) == 1
    assert runtime.stats.snapshot_hit == 0
    rejection = [
        event for event in events
        if event["stage"] == "snapshot_validation"
    ]
    assert len(rejection) == 1
    assert rejection[0]["rejection_reason"] == "membership_hash_mismatch"
    assert rejection[0]["cache_snapshot_path"] == str(manifest_path)


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
    assert runtime.stats.live_success == 1
    request = runtime.fetch_calls[0]
    assert request.canonical_url == "https://pubs.acs.org/toc/jcisd8/66/12"
    assert request.required_selector == "div.issue-item.clearfix"
    assert request.challenge_markers == (
        "Just a moment",
        "cf-chl",
        "Access denied",
    )


def test_acs_absent_snapshot_emits_no_cache_rejection(monkeypatch, runtime):
    runtime.queue_html(ACS_FIXTURE)
    events = []
    monkeypatch.setattr(
        acs_fetch,
        "record_fetch_attempt",
        lambda event, html=None: events.append(event),
    )

    result = acs_fetch.query_acs_issue_result(
        "jcisd8", "66", "12", "JCIM", runtime
    )

    assert result.outcome == SUCCESS
    assert not [
        event for event in events
        if event["stage"] == "snapshot_validation"
    ]


@pytest.mark.parametrize(
    ("corruption", "rejection_reason"),
    [
        ("manifest", "invalid_schema"),
        ("body", "content_hash_failure"),
        ("attachment", "attachment_hash_failure"),
    ],
)
def test_acs_store_corruption_emits_cache_rejection_then_live_retry(
    monkeypatch, snapshot_store, corruption, rejection_reason
):
    stored = seed_acs_snapshot(
        snapshot_store,
        attachments=(
            SnapshotAttachment(
                "abstract.json", "application/json", b'{}'
            ),
        ),
    )
    manifest_path = (
        snapshot_store.root
        / "acs" / "jcisd8" / "66" / "12" / "manifest.json"
    )
    if corruption == "manifest":
        manifest = json.loads(manifest_path.read_text())
        manifest["unexpected"] = True
        manifest_path.write_text(json.dumps(manifest))
        corrupted_path = manifest_path
    elif corruption == "body":
        stored.body_path.write_bytes(b"corrupt")
        corrupted_path = stored.body_path
    else:
        corrupted_path = stored.attachment_paths["abstract.json"]
        corrupted_path.write_bytes(b"corrupt")

    runtime = Runtime(snapshot_store)
    runtime.queue_html(ACS_FIXTURE)
    events = []
    monkeypatch.setattr(
        acs_fetch,
        "record_fetch_attempt",
        lambda event, html=None: events.append(event),
    )

    result = acs_fetch.query_acs_issue_result(
        "jcisd8", "66", "12", "JCIM", runtime
    )

    assert result.outcome == SUCCESS
    assert result.metadata["official_transport"] == "live"
    assert len(runtime.fetch_calls) == 1
    assert runtime.stats.snapshot_hit == 0
    rejection = [
        event for event in events
        if event["stage"] == "snapshot_validation"
    ]
    assert len(rejection) == 1
    assert rejection[0]["rejection_reason"] == rejection_reason
    assert rejection[0]["cache_snapshot_path"] == str(corrupted_path)
    assert "snapshot_path" not in rejection[0]


def test_acs_malformed_live_html_does_not_increment_live_success(
    runtime, snapshot_store
):
    runtime.queue_html(
        '<div class="issue-item clearfix"><h3 class="issue-item_title">'
        '<a href="/article/no-doi">Title</a></h3></div>'
    )

    result = acs_fetch.query_acs_issue_result(
        "jcisd8", "66", "12", "JCIM", runtime
    )

    assert result.outcome == PARSE_FAILED
    assert runtime.stats.live_success == 0
    assert snapshot_store.load(SnapshotKey("acs", "jcisd8", "66", "12")) is None


@pytest.mark.parametrize(
    ("snapshot_kwargs", "rejection_reason"),
    [
        (
            {"canonical_url": "https://pubs.acs.org/toc/jcisd8/66/13"},
            "canonical_url",
        ),
        (
            {"final_url": "https://pubs.acs.org/doi/10.1021/example"},
            "final_url",
        ),
        ({"media_type": "application/json"}, "media_type"),
    ],
)
def test_acs_rejects_snapshot_with_invalid_provenance(
    monkeypatch, snapshot_store, snapshot_kwargs, rejection_reason
):
    seed_acs_snapshot(snapshot_store, **snapshot_kwargs)
    runtime = Runtime(snapshot_store)
    runtime.queue_html(ACS_FIXTURE)
    events = []
    monkeypatch.setattr(
        acs_fetch,
        "record_fetch_attempt",
        lambda event, html=None: events.append(event),
    )

    result = acs_fetch.query_acs_issue_result(
        "jcisd8", "66", "12", "JCIM", runtime
    )

    assert result.outcome == SUCCESS
    assert result.metadata["official_transport"] == "live"
    assert len(runtime.fetch_calls) == 1
    assert runtime.stats.snapshot_hit == 0
    rejection = [
        event for event in events
        if event["stage"] == "snapshot_validation"
    ]
    assert len(rejection) == 1
    assert rejection[0]["cache_validation"] == "rejected"
    assert rejection[0]["rejection_reason"] == rejection_reason


def test_acs_records_snapshot_parse_rejection(snapshot_store):
    stored = seed_acs_snapshot(
        snapshot_store, body=b"<html>No issue cards</html>"
    )
    runtime = Runtime(snapshot_store)
    runtime.queue_html(ACS_FIXTURE)

    result = acs_fetch.query_acs_issue_result(
        "jcisd8", "66", "12", "JCIM", runtime
    )

    assert result.outcome == SUCCESS
    assert runtime.stats.snapshot_hit == 0
    attempts_path = (
        snapshot_store.root.parent / "diagnostics" / "attempts.jsonl"
    )
    events = [
        json.loads(line)
        for line in attempts_path.read_text().splitlines()
    ]
    rejection = [
        event for event in events
        if event["stage"] == "snapshot_validation"
    ]
    assert len(rejection) == 1
    assert rejection[0]["rejection_reason"] == "parse"
    assert rejection[0]["cache_snapshot_path"] == str(stored.body_path)
    assert rejection[0]["snapshot_path"] != str(stored.body_path)


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
    original_load_result = snapshot_store.load_result
    first = True

    def miss_once(key):
        nonlocal first
        if first:
            first = False
            return SnapshotLoadResult(
                None, "absent", "manifest_absent", old.body_path
            )
        return original_load_result(key)

    snapshot_store.load_result = miss_once

    result = acs_fetch.query_acs_issue_result(
        "jcisd8", "66", "12", "JCIM", runtime
    )

    snapshot_store.load_result = original_load_result
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
    original_load_result = snapshot_store.load_result
    snapshot_store.load_result = lambda key: SnapshotLoadResult(
        None, "absent", "manifest_absent", old.body_path
    )
    monkeypatch.setattr(
        acs_fetch,
        "_parse_acs_toc_result",
        lambda *args: (_ for _ in ()).throw(ValueError("broken parser")),
    )

    result = acs_fetch.query_acs_issue_result(
        "jcisd8", "66", "12", "JCIM", runtime
    )

    snapshot_store.load_result = original_load_result
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

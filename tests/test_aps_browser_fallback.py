import json

import pytest

from papertrack import aps_fetch
from papertrack.adapter_result import (
    BLOCKED,
    CIRCUIT_OPEN,
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


APS_FIXTURE = """
<html><body>
  <div class="browse-result-card issue">
    <section class="headline">
      <h4 class="title">
        <a href="/prl/abstract/10.1103/PhysRevLett.127.270401">
          Quantum Vacuum Sagnac Effect
        </a>
      </h4>
    </section>
    <section class="citation-container">
      <p>Guilherme C. Matos, Reinaldo de Melo e Souza, Paulo A. Maia Neto, and François Impens</p>
      <p>Phys. Rev. Lett. <b>127</b>, 270401 (2021) - Published 29 December, 2021</p>
    </section>
    <section class="summary"><p>Official historical issue abstract.</p></section>
  </div>
</body></html>
""".encode("utf-8")

APS_RECENT_FIXTURE = """
<html><body>
  <div class="browse-result-card">
    <section class="headline"><h4 class="title"></h4>
      <a href="/prl/abstract/10.1103/PhysRevLett.127.270401">Quantum Vacuum Sagnac Effect</a>
    </section>
    <div class="summary hidden"><p>Official recent-page abstract.</p></div>
    <form data-published-date="2021-12-29"></form>
  </div>
</body></html>
""".encode("utf-8")


class Runtime:
    def __init__(self, snapshot_store):
        self.snapshot_store = snapshot_store
        self.fetch_calls = []
        self.recent_calls = []
        self.results = []
        self.recent_results = []
        self.stats = ProviderRunStats()

    def queue_html(
        self,
        html,
        *,
        outcome=SUCCESS,
        message="",
        status_code=200,
        attempt_count=1,
    ):
        if isinstance(html, bytes):
            html = html.decode("utf-8")
        canonical_url = "https://journals.aps.org/prl/issues/127/27"
        self.results.append(
            OfficialBrowserResult(
                outcome=outcome,
                canonical_url=canonical_url,
                final_url=canonical_url,
                status_code=status_code,
                html=html,
                captured=(),
                message=message,
                attempt_count=attempt_count,
            )
        )

    def fetch_issue(self, request):
        self.fetch_calls.append(request)
        return self.results.pop(0)

    def queue_recent_html(self, html, *, outcome=SUCCESS, status_code=200,
                          message="", attempt_count=1):
        if isinstance(html, bytes):
            html = html.decode("utf-8")
        self.recent_results.append(
            OfficialBrowserResult(
                outcome=outcome,
                canonical_url="https://journals.aps.org/prl/recent",
                final_url="https://journals.aps.org/prl/recent",
                status_code=status_code,
                html=html,
                captured=(),
                message=message,
                attempt_count=attempt_count,
            )
        )

    def fetch_recent(self, request):
        self.recent_calls.append(request)
        return self.recent_results.pop(0)


@pytest.fixture
def snapshot_store(tmp_path):
    return OfficialSnapshotStore(tmp_path / "snapshots")


@pytest.fixture(autouse=True)
def forbid_legacy_fetch(monkeypatch):
    monkeypatch.setattr(
        aps_fetch,
        "_fetch_html",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("runtime flow must not use legacy APS HTTP fetching")
        ),
    )


@pytest.fixture
def runtime(snapshot_store):
    return Runtime(snapshot_store)


def seed_aps_snapshot(
    snapshot_store,
    body=APS_FIXTURE,
    *,
    canonical_url="https://journals.aps.org/prl/issues/127/27",
    final_url="https://journals.aps.org/prl/issues/127/27",
    media_type="text/html",
    attachments=(),
):
    key = SnapshotKey("aps", "prl", "127", "27")
    return snapshot_store.commit(
        key,
        canonical_url,
        final_url,
        media_type,
        body,
        SnapshotValidation(
            selector_counts={".browse-result-card.issue": 1},
            membership_dois=("10.1103/PhysRevLett.127.270401",),
            parser_version="aps-issue-v1",
        ),
        attachments,
    )


def test_aps_snapshot_rebuild_preserves_official_doi_case(
    runtime, snapshot_store
):
    seed_aps_snapshot(
        snapshot_store,
        final_url="https://journals.aps.org/prl/issues/127/27",
    )

    result = aps_fetch.query_aps_issue_result(
        "prl", "127", "27", "PRL", runtime
    )
    assert "10.1103/PhysRevLett.127.270401" in result.entries
    assert result.metadata["official_transport"] == "snapshot"
    assert runtime.fetch_calls == []
    assert runtime.stats.snapshot_hit == 1


def test_aps_force_live_bypasses_but_preserves_valid_snapshot(
    runtime, snapshot_store
):
    stored = seed_aps_snapshot(snapshot_store)
    before = stored.body_path.read_bytes()
    runtime.queue_html(
        "challenge", outcome=BLOCKED, message="blocked", status_code=403
    )

    result = aps_fetch.query_aps_issue_result(
        "prl", "127", "27", "PRL", runtime, force_live=True
    )

    assert result.outcome == BLOCKED
    assert len(runtime.fetch_calls) == 1
    assert runtime.stats.snapshot_hit == 0
    assert stored.body_path.read_bytes() == before


def test_aps_force_live_rejects_membership_regression_without_replacing_manifest(
    runtime, snapshot_store
):
    second = APS_FIXTURE.replace(
        b"10.1103/PhysRevLett.127.270401",
        b"10.1103/PhysRevLett.127.270402",
    ).replace(b"Quantum Vacuum Sagnac Effect", b"Second article")
    key = SnapshotKey("aps", "prl", "127", "27")
    second_card = second.split(b"<body>", 1)[1].split(b"</body>", 1)[0]
    snapshot_store.commit(
        key,
        "https://journals.aps.org/prl/issues/127/27",
        "https://journals.aps.org/prl/issues/127/27",
        "text/html",
        APS_FIXTURE.replace(b"</body>", second_card + b"</body>"),
        SnapshotValidation(
            selector_counts={".browse-result-card.issue": 2},
            membership_dois=(
                "10.1103/PhysRevLett.127.270401",
                "10.1103/PhysRevLett.127.270402",
            ),
            parser_version="aps-issue-v1",
        ),
    )
    manifest = snapshot_store.root / "aps/prl/127/27/manifest.json"
    before = manifest.read_bytes()
    runtime.queue_html(APS_FIXTURE)

    result = aps_fetch.query_aps_issue_result(
        "prl", "127", "27", "PRL", runtime, force_live=True
    )

    assert result.outcome == FETCH_FAILED
    assert "membership regressed" in result.message
    assert manifest.read_bytes() == before
    assert snapshot_store.load(key).validation.membership_dois == (
        "10.1103/PhysRevLett.127.270401",
        "10.1103/PhysRevLett.127.270402",
    )
    assert runtime.stats.live_success == 0


def test_aps_reparses_snapshot_and_rejects_changed_membership(
    monkeypatch, runtime, snapshot_store
):
    seed_aps_snapshot(snapshot_store)
    manifest_path = (
        snapshot_store.root
        / "aps" / "prl" / "127" / "27" / "manifest.json"
    )
    manifest = json.loads(manifest_path.read_text())
    manifest["membership_hash"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest))
    runtime.queue_html(APS_FIXTURE)
    events = []
    monkeypatch.setattr(
        aps_fetch,
        "record_fetch_attempt",
        lambda event, html=None: events.append(event),
    )

    result = aps_fetch.query_aps_issue_result(
        "prl", "127", "27", "PRL", runtime
    )

    assert result.outcome == SUCCESS
    assert result.metadata["official_transport"] == "live"
    assert len(runtime.fetch_calls) == 1
    assert runtime.stats.snapshot_hit == 0
    assert runtime.stats.cache_rejected == 1
    rejection = [
        event for event in events
        if event["stage"] == "snapshot_validation"
    ]
    assert len(rejection) == 1
    assert rejection[0]["rejection_reason"] == "membership_hash_mismatch"
    assert rejection[0]["cache_snapshot_path"] == str(manifest_path)


def test_aps_live_success_commits_validated_snapshot(runtime, snapshot_store):
    runtime.queue_html(APS_FIXTURE)

    result = aps_fetch.query_aps_issue_result(
        "prl", "127", "27", "PRL", runtime
    )

    stored = snapshot_store.load(SnapshotKey("aps", "prl", "127", "27"))
    assert result.outcome == SUCCESS
    assert stored is not None
    assert stored.validation.membership_dois == (
        "10.1103/PhysRevLett.127.270401",
    )
    assert result.metadata["official_transport"] == "live"
    assert result.metadata["snapshot_path"] == str(stored.body_path)
    assert result.metadata["membership_hash"] == stored.membership_hash
    assert runtime.stats.live_success == 1
    request = runtime.fetch_calls[0]
    assert request.canonical_url == (
        "https://journals.aps.org/prl/issues/127/27"
    )
    assert request.required_selector == ".browse-result-card.issue"
    assert request.challenge_markers == (
        "Just a moment",
        "cf-chl",
        "Access denied",
        "Cloudflare",
    )
    assert all(
        "/abstract/" not in call.canonical_url
        and "/articles/" not in call.canonical_url
        for call in runtime.fetch_calls
    )


def test_aps_recent_cache_fills_only_missing_issue_member(runtime, snapshot_store):
    issue_without_abstract = APS_FIXTURE.replace(
        b"Official historical issue abstract.", b""
    )
    runtime.queue_html(issue_without_abstract)
    runtime.queue_recent_html(APS_RECENT_FIXTURE)

    result = aps_fetch.query_aps_issue_result(
        "prl", "127", "27", "PRL", runtime
    )
    coverage = aps_fetch.enrich_aps_entries_from_recent(
        result.entries, "prl", runtime
    )

    entry = result.entries["10.1103/PhysRevLett.127.270401"]
    assert entry.abstract == "Official recent-page abstract."
    assert len(runtime.fetch_calls) == 1
    assert len(runtime.recent_calls) == 1
    assert runtime.recent_calls[0].canonical_url == (
        "https://journals.aps.org/prl/recent"
    )
    assert coverage == {
        "filled": 1,
        "remaining": 0,
        "pages": 1,
        "outcome": "success",
        "issue_search_from_date": "2021-12-29",
        "recent_last_page_date": "2021-12-29",
    }
    recent = snapshot_store.load(
        SnapshotKey("aps-recent", "prl", "recent", "1")
    )
    assert recent is not None
    assert recent.validation.membership_dois == (
        "10.1103/physrevlett.127.270401",
    )

    second_runtime = Runtime(snapshot_store)
    cached = aps_fetch.query_aps_issue_result(
        "prl", "127", "27", "PRL", second_runtime
    )
    aps_fetch.enrich_aps_entries_from_recent(
        cached.entries, "prl", second_runtime
    )
    assert cached.entries["10.1103/PhysRevLett.127.270401"].abstract == (
        "Official recent-page abstract."
    )
    assert second_runtime.fetch_calls == []
    assert second_runtime.recent_calls == []


def test_aps_issue_teaser_is_not_rendered_as_an_abstract():
    teaser_only = APS_FIXTURE.replace(
        b'class="summary"', b'class="summary-container"'
    )

    parsed = aps_fetch._parse_aps_issue(
        teaser_only.decode("utf-8"), "prl", "PRL", "127", "27"
    )

    assert parsed["10.1103/PhysRevLett.127.270401"].abstract == ""


def test_aps_absent_snapshot_emits_no_cache_rejection(monkeypatch, runtime):
    runtime.queue_html(APS_FIXTURE)
    events = []
    monkeypatch.setattr(
        aps_fetch,
        "record_fetch_attempt",
        lambda event, html=None: events.append(event),
    )

    result = aps_fetch.query_aps_issue_result(
        "prl", "127", "27", "PRL", runtime
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
def test_aps_store_corruption_emits_cache_rejection_then_live_retry(
    monkeypatch, snapshot_store, corruption, rejection_reason
):
    stored = seed_aps_snapshot(
        snapshot_store,
        attachments=(
            SnapshotAttachment(
                "abstract.json", "application/json", b'{}'
            ),
        ),
    )
    manifest_path = (
        snapshot_store.root
        / "aps" / "prl" / "127" / "27" / "manifest.json"
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
    runtime.queue_html(APS_FIXTURE)
    events = []
    monkeypatch.setattr(
        aps_fetch,
        "record_fetch_attempt",
        lambda event, html=None: events.append(event),
    )

    result = aps_fetch.query_aps_issue_result(
        "prl", "127", "27", "PRL", runtime
    )

    assert result.outcome == SUCCESS
    assert result.metadata["official_transport"] == "live"
    assert len(runtime.fetch_calls) == 1
    assert runtime.stats.snapshot_hit == 0
    assert runtime.stats.cache_rejected == 1
    rejection = [
        event for event in events
        if event["stage"] == "snapshot_validation"
    ]
    assert len(rejection) == 1
    assert rejection[0]["rejection_reason"] == rejection_reason
    assert rejection[0]["cache_snapshot_path"] == str(corrupted_path)
    assert "snapshot_path" not in rejection[0]


def test_aps_malformed_live_html_does_not_increment_live_success(
    runtime, snapshot_store
):
    runtime.queue_html(
        '<div class="browse-result-card issue"><div class="title">'
        '<a href="/prl/abstract/not-a-doi">Title</a></div></div>'
    )

    result = aps_fetch.query_aps_issue_result(
        "prl", "127", "27", "PRL", runtime
    )

    assert result.outcome == PARSE_FAILED
    assert runtime.stats.live_success == 0
    assert snapshot_store.load(SnapshotKey("aps", "prl", "127", "27")) is None


@pytest.mark.parametrize(
    ("snapshot_kwargs", "rejection_reason"),
    [
        (
            {
                "canonical_url": (
                    "https://journals.aps.org/prl/issues/127/26"
                )
            },
            "canonical_url",
        ),
        (
            {
                "final_url": (
                    "https://journals.aps.org/prl/abstract/10.1103/example"
                )
            },
            "final_url",
        ),
        ({"media_type": "application/json"}, "media_type"),
    ],
)
def test_aps_rejects_snapshot_with_invalid_provenance(
    monkeypatch, snapshot_store, snapshot_kwargs, rejection_reason
):
    seed_aps_snapshot(snapshot_store, **snapshot_kwargs)
    runtime = Runtime(snapshot_store)
    runtime.queue_html(APS_FIXTURE)
    events = []
    monkeypatch.setattr(
        aps_fetch,
        "record_fetch_attempt",
        lambda event, html=None: events.append(event),
    )

    result = aps_fetch.query_aps_issue_result(
        "prl", "127", "27", "PRL", runtime
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


def test_aps_records_snapshot_parse_rejection(monkeypatch, snapshot_store):
    seed_aps_snapshot(snapshot_store, body=b"<html>No issue cards</html>")
    runtime = Runtime(snapshot_store)
    runtime.queue_html(APS_FIXTURE)
    events = []
    monkeypatch.setattr(
        aps_fetch,
        "record_fetch_attempt",
        lambda event, html=None: events.append(event),
    )

    result = aps_fetch.query_aps_issue_result(
        "prl", "127", "27", "PRL", runtime
    )

    assert result.outcome == SUCCESS
    assert runtime.stats.snapshot_hit == 0
    assert runtime.stats.cache_rejected == 1
    rejection = [
        event for event in events
        if event["stage"] == "snapshot_validation"
    ]
    assert len(rejection) == 1
    assert rejection[0]["rejection_reason"] == "parse"


def test_aps_requires_issue_cards_before_snapshot_commit(runtime, snapshot_store):
    runtime.queue_html("<html><title>Physical Review Letters</title></html>")

    result = aps_fetch.query_aps_issue_result(
        "prl", "127", "27", "PRL", runtime
    )

    assert result.outcome == OFFICIAL_EMPTY_SUSPICIOUS
    assert snapshot_store.load(SnapshotKey("aps", "prl", "127", "27")) is None


@pytest.mark.parametrize(
    ("html", "runtime_outcome", "expected_outcome"),
    [
        ("<title>Just a moment</title>", BLOCKED, BLOCKED),
        ("<html>No cards</html>", SUCCESS, OFFICIAL_EMPTY_SUSPICIOUS),
        (
            '<div class="browse-result-card issue"><div class="title"><a href="/prl/abstract/not-a-doi">Title</a></div></div>',
            SUCCESS,
            PARSE_FAILED,
        ),
    ],
)
def test_aps_invalid_live_result_never_replaces_existing_snapshot(
    snapshot_store, html, runtime_outcome, expected_outcome
):
    old = seed_aps_snapshot(snapshot_store)
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

    result = aps_fetch.query_aps_issue_result(
        "prl", "127", "27", "PRL", runtime
    )

    snapshot_store.load_result = original_load_result
    preserved = snapshot_store.load(SnapshotKey("aps", "prl", "127", "27"))
    assert result.outcome == expected_outcome
    assert preserved is not None
    assert preserved.content_hash == old.content_hash


def test_aps_parser_exception_never_replaces_existing_snapshot(
    monkeypatch, snapshot_store
):
    old = seed_aps_snapshot(snapshot_store)
    runtime = Runtime(snapshot_store)
    runtime.queue_html(APS_FIXTURE)
    original_load_result = snapshot_store.load_result
    snapshot_store.load_result = lambda key: SnapshotLoadResult(
        None, "absent", "manifest_absent", old.body_path
    )
    monkeypatch.setattr(
        aps_fetch,
        "_parse_aps_issue_result",
        lambda *args: (_ for _ in ()).throw(ValueError("broken parser")),
    )

    result = aps_fetch.query_aps_issue_result(
        "prl", "127", "27", "PRL", runtime
    )

    snapshot_store.load_result = original_load_result
    preserved = snapshot_store.load(SnapshotKey("aps", "prl", "127", "27"))
    assert result.outcome == PARSE_FAILED
    assert preserved is not None
    assert preserved.content_hash == old.content_hash


def test_aps_circuit_open_records_complete_fetch_diagnostic(
    monkeypatch, runtime
):
    runtime.queue_html(
        "",
        outcome=CIRCUIT_OPEN,
        message="provider circuit is open",
        status_code=None,
        attempt_count=0,
    )
    events = []
    monkeypatch.setattr(
        aps_fetch,
        "record_fetch_attempt",
        lambda event, html=None: events.append(event),
    )

    result = aps_fetch.query_aps_issue_result(
        "prl", "127", "27", "PRL", runtime
    )

    assert result.outcome == CIRCUIT_OPEN
    assert result.entries == {}
    assert events == [
        {
            "source_role": "authoritative",
            "source": "journal",
            "provider": "aps",
            "journal": "prl",
            "volume": "127",
            "issue": "27",
            "url": "https://journals.aps.org/prl/issues/127/27",
            "stage": "issue_page_fetch",
            "outcome": CIRCUIT_OPEN,
            "http_status": None,
            "attempt_count": 0,
            "message": "provider circuit is open",
        }
    ]


def test_aps_fetch_failure_is_reported(runtime):
    runtime.queue_html("", outcome=FETCH_FAILED, message="browser failed")

    result = aps_fetch.query_aps_issue_result(
        "prl", "127", "27", "PRL", runtime
    )

    assert result.outcome == FETCH_FAILED
    assert result.message == "browser failed"

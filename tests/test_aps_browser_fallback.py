from dataclasses import replace

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
    SnapshotKey,
    SnapshotValidation,
)
from papertrack.provider_runtime import OfficialBrowserResult


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
    <section class="summary-container"><p>Official historical issue summary.</p></section>
  </div>
</body></html>
""".encode("utf-8")


class Runtime:
    def __init__(self, snapshot_store):
        self.snapshot_store = snapshot_store
        self.fetch_calls = []
        self.results = []

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


def seed_aps_snapshot(snapshot_store, body=APS_FIXTURE):
    key = SnapshotKey("aps", "prl", "127", "27")
    return snapshot_store.commit(
        key,
        "https://journals.aps.org/prl/issues/127/27",
        "https://journals.aps.org/prl/issues/127/27",
        "text/html",
        body,
        SnapshotValidation(
            selector_counts={".browse-result-card.issue": 1},
            membership_dois=("10.1103/PhysRevLett.127.270401",),
            parser_version="aps-issue-v1",
        ),
    )


def test_aps_snapshot_rebuild_preserves_official_doi_case(
    runtime, snapshot_store
):
    seed_aps_snapshot(snapshot_store)

    result = aps_fetch.query_aps_issue_result(
        "prl", "127", "27", "PRL", runtime
    )

    assert "10.1103/PhysRevLett.127.270401" in result.entries
    assert result.metadata["official_transport"] == "snapshot"
    assert runtime.fetch_calls == []


def test_aps_reparses_snapshot_and_rejects_changed_membership(
    runtime, snapshot_store
):
    stored = seed_aps_snapshot(snapshot_store)
    mismatched = replace(stored, membership_hash="0" * 64)
    original_load = snapshot_store.load
    loads = iter((mismatched,))
    snapshot_store.load = lambda key: next(loads, original_load(key))
    runtime.queue_html(APS_FIXTURE)

    result = aps_fetch.query_aps_issue_result(
        "prl", "127", "27", "PRL", runtime
    )

    assert result.outcome == SUCCESS
    assert result.metadata["official_transport"] == "live"
    assert len(runtime.fetch_calls) == 1


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
    original_load = snapshot_store.load
    first = True

    def miss_once(key):
        nonlocal first
        if first:
            first = False
            return None
        return original_load(key)

    snapshot_store.load = miss_once

    result = aps_fetch.query_aps_issue_result(
        "prl", "127", "27", "PRL", runtime
    )

    snapshot_store.load = original_load
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
    original_load = snapshot_store.load
    snapshot_store.load = lambda key: None
    monkeypatch.setattr(
        aps_fetch,
        "_parse_aps_issue_result",
        lambda *args: (_ for _ in ()).throw(ValueError("broken parser")),
    )

    result = aps_fetch.query_aps_issue_result(
        "prl", "127", "27", "PRL", runtime
    )

    snapshot_store.load = original_load
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

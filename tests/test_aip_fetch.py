import json

import pytest

from papertrack import aip_fetch
from papertrack.adapter_result import (
    BLOCKED,
    FETCH_FAILED,
    PARSE_FAILED,
    RATE_LIMITED,
    SUCCESS,
)
from papertrack.official_snapshot import (
    OfficialSnapshotStore,
    SnapshotKey,
    SnapshotValidation,
)
from papertrack.provider_runtime import (
    CapturedResponse,
    OfficialBrowserResult,
    ProviderRunStats,
)


AIP_ISSUE_FIXTURE = """
<html><body>
  <div class="al-article-items">
    <h3 class="item-title">
      <a href="/aip/jcp/article/160/4/041101/12345/Title">Official article</a>
    </h3>
    <div class="ww-citation-primary">
      160, 041101 (2024)
      <span class="citation-label">
        <a href="https://doi.org/10.1063/example">10.1063/example</a>
      </span>
    </div>
    <a class="js-show-abstract" data-articleid="12345">Abstract</a>
  </div>
</body></html>
"""


class Runtime:
    def __init__(self, snapshot_store):
        self.snapshot_store = snapshot_store
        self.issue_results = []
        self.abstract_results = []
        self.fetch_calls = []
        self.request_calls = []
        self.stats = ProviderRunStats()
        self.context_count = 1

    def queue_issue_html(self, html, *, outcome=SUCCESS, captured=()):
        self.issue_results.append(
            OfficialBrowserResult(
                outcome=outcome,
                canonical_url="https://pubs.aip.org/aip/jcp/issue/160/4",
                final_url="https://pubs.aip.org/aip/jcp/issue/160/4",
                status_code=200 if outcome == SUCCESS else 403,
                html=html,
                captured=tuple(captured),
            )
        )

    def queue_response(self, *, status=200, body=b"{}", media_type="application/json"):
        self.abstract_results.append(
            CapturedResponse(
                url=aip_fetch.aip_abstract_url("jcp"),
                status_code=status,
                media_type=media_type,
                body=body,
            )
        )

    def queue_json(self, payload, *, status=200):
        self.queue_response(status=status, body=json.dumps(payload).encode())

    def queue_exception(self, exception):
        self.abstract_results.append(exception)

    def fetch_issue(self, request):
        self.fetch_calls.append(request)
        return self.issue_results.pop(0)

    def request_same_origin(self, url, method, form, min_interval):
        self.request_calls.append(
            {
                "url": url,
                "method": method,
                "form": form,
                "min_interval": min_interval,
                "same_origin": url.startswith("https://pubs.aip.org/"),
            }
        )
        result = self.abstract_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _aip_issue_fixture(article_count):
    return "<html><body>" + "".join(
        f"""
        <div class="al-article-items">
          <h3 class="item-title">
            <a href="/aip/jcp/article/160/4/04110{index}/{10000 + index}/Title">
              Official article {index}
            </a>
          </h3>
          <div class="ww-citation-primary">
            160, 04110{index} (2024)
            <span class="citation-label">
              <a href="https://doi.org/10.1063/example{index}">
                10.1063/example{index}
              </a>
            </span>
          </div>
          <a class="js-show-abstract" data-articleid="{10000 + index}">
            Abstract
          </a>
        </div>
        """
        for index in range(article_count)
    ) + "</body></html>"


@pytest.fixture
def snapshot_store(tmp_path):
    return OfficialSnapshotStore(tmp_path / "snapshots")


@pytest.fixture
def runtime(snapshot_store):
    return Runtime(snapshot_store)


@pytest.fixture(autouse=True)
def forbid_legacy_fetch(monkeypatch):
    monkeypatch.setattr(
        aip_fetch,
        "_fetch_issue_http",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("runtime flow must not use legacy AIP HTTP fetching")
        ),
    )


def test_aip_fetches_issue_and_abstracts_in_same_runtime(runtime):
    runtime.queue_issue_html(AIP_ISSUE_FIXTURE)
    runtime.queue_json({"Success": True, "Html": "<p>Official abstract.</p>"})

    result = aip_fetch.query_aip_issue_result("jcp", "160", "4", "JCP", runtime)

    assert result.outcome == SUCCESS
    assert next(iter(result.entries.values())).abstract == "Official abstract."
    assert runtime.context_count == 1
    assert all(call["same_origin"] for call in runtime.request_calls)
    assert runtime.request_calls == [
        {
            "url": "https://pubs.aip.org/Handlers/ArticleAbstractHandler.ashx",
            "method": "POST",
            "form": {"articleId": "12345", "layAbstract": "False"},
            "min_interval": 10,
            "same_origin": True,
        }
    ]
    request = runtime.fetch_calls[0]
    assert request.required_selector == ".al-article-items"
    assert any(
        pattern.search("https://pubs.aip.org/Handlers/ArticleAbstractHandler.ashx")
        for pattern in request.capture_url_patterns
    )


@pytest.mark.parametrize(
    ("status", "body", "outcome"),
    [
        (403, b"forbidden", BLOCKED),
        (200, b"not-json", FETCH_FAILED),
        (200, b'{"Success": false, "Html": ""}', "official_response_empty"),
    ],
)
def test_aip_abstract_failures_remain_distinguishable(
    runtime, status, body, outcome
):
    runtime.queue_issue_html(AIP_ISSUE_FIXTURE)
    runtime.queue_response(status=status, body=body)

    result = aip_fetch.query_aip_issue_result("jcp", "160", "4", "JCP", runtime)

    assert result.outcome == (BLOCKED if outcome == BLOCKED else FETCH_FAILED)
    assert result.entries["10.1063/example"].abstract == ""
    assert result.metadata["missing_fields"] == [
        {"doi": "10.1063/example", "field": "abstract", "outcome": outcome}
    ]


def test_aip_blocked_abstract_remains_retryable_and_is_not_snapshotted(
    runtime, snapshot_store
):
    runtime.queue_issue_html(AIP_ISSUE_FIXTURE)
    runtime.queue_response(status=403, body=b"forbidden", media_type="text/html")
    live = aip_fetch.query_aip_issue_result("jcp", "160", "4", "JCP", runtime)
    assert live.outcome == BLOCKED
    assert live.metadata["missing_fields"][0]["outcome"] == BLOCKED
    assert snapshot_store.load(SnapshotKey("aip", "jcp", "160", "4")) is None
    assert runtime.stats.live_success == 0


@pytest.mark.parametrize(
    ("status", "expected_outcome"),
    [(403, BLOCKED), (429, RATE_LIMITED)],
)
def test_aip_stops_abstract_requests_when_provider_circuit_opens(
    snapshot_store, status, expected_outcome
):
    class CircuitRuntime(Runtime):
        def __init__(self, store):
            super().__init__(store)
            self.circuit_open = False
            self._endpoint_denials = 0

        def request_same_origin(self, url, method, form, min_interval):
            response = super().request_same_origin(url, method, form, min_interval)
            if response.status_code == 200:
                self._endpoint_denials = 0
            elif response.status_code in {403, 429}:
                self._endpoint_denials += 1
                if self._endpoint_denials >= 3:
                    self.circuit_open = True
            return response

    runtime = CircuitRuntime(snapshot_store)
    runtime.queue_issue_html(_aip_issue_fixture(6))
    for _ in range(6):
        runtime.queue_response(status=status, body=b"", media_type="text/html")

    result = aip_fetch.query_aip_issue_result("jcp", "160", "4", "JCP", runtime)

    assert result.outcome == expected_outcome
    assert len(runtime.request_calls) == 3
    assert len(runtime.abstract_results) == 3
    assert result.metadata["snapshot_committed"] is False
    assert result.metadata["endpoint_circuit_open"] is True
    assert snapshot_store.load(SnapshotKey("aip", "jcp", "160", "4")) is None
    assert runtime.stats.live_success == 0


@pytest.mark.parametrize(
    ("status", "expected_outcome"),
    [(403, BLOCKED), (429, RATE_LIMITED)],
)
def test_aip_captured_denials_open_circuit_without_explicit_rerequests(
    snapshot_store, status, expected_outcome
):
    captured = tuple(
        CapturedResponse(
            url=(
                "https://pubs.aip.org/Handlers/ArticleAbstractHandler.ashx"
                f"?articleId={10000 + index}"
            ),
            status_code=status,
            media_type="text/html",
            body=b"denied",
        )
        for index in range(3)
    )

    class CapturedCircuitRuntime(Runtime):
        circuit_open = False

        def fetch_issue(self, request):
            result = super().fetch_issue(request)
            self.circuit_open = True
            return result

    runtime = CapturedCircuitRuntime(snapshot_store)
    runtime.queue_issue_html(_aip_issue_fixture(6), captured=captured)

    result = aip_fetch.query_aip_issue_result("jcp", "160", "4", "JCP", runtime)

    assert result.outcome == expected_outcome
    assert runtime.request_calls == []
    assert result.metadata["snapshot_committed"] is False
    assert result.metadata["endpoint_circuit_open"] is True
    assert snapshot_store.load(SnapshotKey("aip", "jcp", "160", "4")) is None
    assert runtime.stats.live_success == 0


def test_aip_force_live_bypasses_but_preserves_valid_snapshot(
    runtime, snapshot_store
):
    runtime.queue_issue_html(AIP_ISSUE_FIXTURE)
    runtime.queue_json({"Success": True, "Html": "<p>Official abstract.</p>"})
    live = aip_fetch.query_aip_issue_result(
        "jcp", "160", "4", "JCP", runtime
    )
    stored = snapshot_store.load(SnapshotKey("aip", "jcp", "160", "4"))
    assert live.outcome == SUCCESS
    assert stored is not None
    manifest = snapshot_store.root / "aip/jcp/160/4/manifest.json"
    before = manifest.read_bytes()

    blocked_runtime = Runtime(snapshot_store)
    blocked_runtime.queue_issue_html(AIP_ISSUE_FIXTURE)
    blocked_runtime.queue_response(status=403, body=b"forbidden", media_type="text/html")
    result = aip_fetch.query_aip_issue_result(
        "jcp", "160", "4", "JCP", blocked_runtime, force_live=True
    )

    assert result.outcome == BLOCKED
    assert len(blocked_runtime.fetch_calls) == 1
    assert blocked_runtime.stats.snapshot_hit == 0
    assert blocked_runtime.stats.live_success == 0
    assert manifest.read_bytes() == before

    reload_runtime = Runtime(snapshot_store)
    reloaded = aip_fetch.query_aip_issue_result(
        "jcp", "160", "4", "JCP", reload_runtime
    )
    assert reloaded.outcome == SUCCESS
    assert reloaded.entries["10.1063/example"].abstract == "Official abstract."
    assert reload_runtime.fetch_calls == []


def test_aip_absent_official_abstract_control_is_distinguishable(runtime):
    runtime.queue_issue_html(
        AIP_ISSUE_FIXTURE.replace(
            '<a class="js-show-abstract" data-articleid="12345">Abstract</a>', ""
        )
    )

    result = aip_fetch.query_aip_issue_result("jcp", "160", "4", "JCP", runtime)

    assert result.outcome == SUCCESS
    assert result.metadata["missing_fields"] == [
        {
            "doi": "10.1063/example",
            "field": "abstract",
            "outcome": "official_control_absent",
        }
    ]
    assert runtime.request_calls == []


def test_aip_live_success_snapshots_membership_and_abstract(runtime, snapshot_store):
    runtime.queue_issue_html(AIP_ISSUE_FIXTURE)
    runtime.queue_json({"Success": True, "Html": "<p>Official abstract.</p>"})

    result = aip_fetch.query_aip_issue_result("jcp", "160", "4", "JCP", runtime)

    stored = snapshot_store.load(SnapshotKey("aip", "jcp", "160", "4"))
    assert result.outcome == SUCCESS
    assert stored is not None
    assert stored.validation.membership_dois == ("10.1063/example",)
    assert set(stored.attachment_paths) == {"abstract-12345.json"}
    assert result.metadata["official_transport"] == "live"
    assert runtime.stats.live_success == 1


def test_aip_reuses_captured_official_abstract_response(runtime, snapshot_store):
    captured = CapturedResponse(
        url=(
            "https://pubs.aip.org/Handlers/ArticleAbstractHandler.ashx"
            "?articleId=12345"
        ),
        status_code=200,
        media_type="application/json",
        body=b'{"Success": true, "Html": "<p>Captured abstract.</p>"}',
    )
    runtime.queue_issue_html(AIP_ISSUE_FIXTURE, captured=(captured,))

    result = aip_fetch.query_aip_issue_result("jcp", "160", "4", "JCP", runtime)

    assert result.entries["10.1063/example"].abstract == "Captured abstract."
    assert runtime.request_calls == []
    stored = snapshot_store.load(SnapshotKey("aip", "jcp", "160", "4"))
    assert set(stored.attachment_paths) == {"abstract-12345.json"}


def test_aip_never_guesses_unlabelled_captured_abstract(runtime):
    runtime.queue_issue_html(
        AIP_ISSUE_FIXTURE,
        captured=(
            CapturedResponse(
                url="https://pubs.aip.org/Handlers/ArticleAbstractHandler.ashx",
                status_code=200,
                media_type="application/json",
                body=b'{"Success": true, "Html": "<p>Unlabelled.</p>"}',
            ),
        ),
    )
    runtime.queue_json({"Success": True, "Html": "<p>Explicit response.</p>"})

    result = aip_fetch.query_aip_issue_result("jcp", "160", "4", "JCP", runtime)

    assert result.entries["10.1063/example"].abstract == "Explicit response."
    assert len(runtime.request_calls) == 1


def test_aip_abstract_pacing_never_drops_below_ten_seconds(monkeypatch, runtime):
    monkeypatch.setattr(aip_fetch, "AIP_ABSTRACT_INTERVAL", 2)
    runtime.queue_issue_html(AIP_ISSUE_FIXTURE)
    runtime.queue_json({"Success": True, "Html": "<p>Official abstract.</p>"})

    aip_fetch.query_aip_issue_result("jcp", "160", "4", "JCP", runtime)

    assert runtime.request_calls[0]["min_interval"] == 10


def test_aip_reloads_snapshot_without_network(runtime, snapshot_store):
    snapshot_store.commit(
        SnapshotKey("aip", "jcp", "160", "4"),
        "https://pubs.aip.org/aip/jcp/issue/160/4",
        "https://pubs.aip.org/aip/jcp/issue/160/4",
        "text/html",
        AIP_ISSUE_FIXTURE.encode(),
        SnapshotValidation(
            selector_counts={".al-article-items": 1},
            membership_dois=("10.1063/example",),
            parser_version="aip-issue-v1",
        ),
        (
            aip_fetch.SnapshotAttachment(
                "abstract-12345.json",
                "application/json",
                _attachment_envelope(
                    body=b'{"Success": true, "Html": "<p>Cached abstract.</p>"}'
                ),
            ),
        ),
    )

    result = aip_fetch.query_aip_issue_result("jcp", "160", "4", "JCP", runtime)

    assert result.outcome == SUCCESS
    assert result.entries["10.1063/example"].abstract == "Cached abstract."
    assert result.metadata["official_transport"] == "snapshot"
    assert runtime.fetch_calls == []
    assert runtime.request_calls == []
    assert runtime.stats.snapshot_hit == 1


def test_aip_snapshot_missing_expected_attachment_refetches(runtime, snapshot_store):
    snapshot_store.commit(
        SnapshotKey("aip", "jcp", "160", "4"),
        "https://pubs.aip.org/aip/jcp/issue/160/4",
        "https://pubs.aip.org/aip/jcp/issue/160/4",
        "text/html",
        AIP_ISSUE_FIXTURE.encode(),
        SnapshotValidation(
            selector_counts={".al-article-items": 1},
            membership_dois=("10.1063/example",),
            parser_version="aip-issue-v1",
        ),
    )
    runtime.queue_issue_html(AIP_ISSUE_FIXTURE)
    runtime.queue_json({"Success": True, "Html": "<p>Fresh abstract.</p>"})

    result = aip_fetch.query_aip_issue_result("jcp", "160", "4", "JCP", runtime)

    assert result.entries["10.1063/example"].abstract == "Fresh abstract."
    assert len(runtime.fetch_calls) == 1
    assert runtime.stats.cache_rejected == 1


def test_aip_force_live_repairs_snapshot_rejected_by_current_parser(
    monkeypatch, runtime, snapshot_store
):
    snapshot_store.commit(
        SnapshotKey("aip", "jcp", "160", "4"),
        "https://pubs.aip.org/aip/jcp/issue/160/4",
        "https://pubs.aip.org/aip/jcp/issue/160/4",
        "text/html",
        AIP_ISSUE_FIXTURE.encode(),
        SnapshotValidation(
            selector_counts={".al-article-items": 1},
            membership_dois=("10.1063/example",),
            parser_version="aip-issue-v1",
        ),
        (
            aip_fetch.SnapshotAttachment(
                "abstract-12345.json", "application/json", _attachment_envelope()
            ),
        ),
    )
    runtime.queue_issue_html(AIP_ISSUE_FIXTURE)
    runtime.queue_json({"Success": True, "Html": "<p>Repaired.</p>"})
    original = aip_fetch._result_from_official_material
    events = []

    def fail_snapshot_current_parser(*args, **kwargs):
        if kwargs["transport"] == "snapshot":
            raise RuntimeError("current parser rejected trusted baseline")
        return original(*args, **kwargs)

    monkeypatch.setattr(
        aip_fetch, "_result_from_official_material", fail_snapshot_current_parser
    )
    monkeypatch.setattr(
        aip_fetch,
        "record_fetch_attempt",
        lambda event, html=None: events.append((event, html)),
    )

    result = aip_fetch.query_aip_issue_result(
        "jcp", "160", "4", "JCP", runtime, force_live=True
    )

    assert result.outcome == SUCCESS
    assert result.entries["10.1063/example"].abstract == "Repaired."
    assert len(runtime.fetch_calls) == 1
    assert runtime.stats.cache_rejected == 1
    rejection = next(
        event for event, _ in events if event["stage"] == "snapshot_validation"
    )
    assert rejection["message"] == "current parser rejected trusted baseline"


def test_aip_snapshot_validation_does_not_catch_base_exception(
    monkeypatch, runtime, snapshot_store
):
    snapshot_store.commit(
        SnapshotKey("aip", "jcp", "160", "4"),
        "https://pubs.aip.org/aip/jcp/issue/160/4",
        "https://pubs.aip.org/aip/jcp/issue/160/4",
        "text/html",
        AIP_ISSUE_FIXTURE.encode(),
        SnapshotValidation(
            selector_counts={".al-article-items": 1},
            membership_dois=("10.1063/example",),
            parser_version="aip-issue-v1",
        ),
        (
            aip_fetch.SnapshotAttachment(
                "abstract-12345.json", "application/json", _attachment_envelope()
            ),
        ),
    )

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt("stop baseline validation")

    monkeypatch.setattr(aip_fetch, "_result_from_official_material", interrupt)

    with pytest.raises(KeyboardInterrupt, match="stop baseline validation"):
        aip_fetch.query_aip_issue_result(
            "jcp", "160", "4", "JCP", runtime, force_live=True
        )

    assert runtime.fetch_calls == []


def test_aip_baseline_recovery_does_not_catch_live_parser_error(
    monkeypatch, runtime, snapshot_store
):
    snapshot_store.commit(
        SnapshotKey("aip", "jcp", "160", "4"),
        "https://pubs.aip.org/aip/jcp/issue/160/4",
        "https://pubs.aip.org/aip/jcp/issue/160/4",
        "text/html",
        AIP_ISSUE_FIXTURE.encode(),
        SnapshotValidation(
            selector_counts={".al-article-items": 1},
            membership_dois=("10.1063/example",),
            parser_version="aip-issue-v1",
        ),
        (
            aip_fetch.SnapshotAttachment(
                "abstract-12345.json", "application/json", _attachment_envelope()
            ),
        ),
    )
    runtime.queue_issue_html(AIP_ISSUE_FIXTURE)
    original = aip_fetch._result_from_official_material

    def fail_live_parser(*args, **kwargs):
        if kwargs["transport"] == "live":
            raise RuntimeError("live parser failed")
        return original(*args, **kwargs)

    monkeypatch.setattr(aip_fetch, "_result_from_official_material", fail_live_parser)

    with pytest.raises(RuntimeError, match="live parser failed"):
        aip_fetch.query_aip_issue_result(
            "jcp", "160", "4", "JCP", runtime, force_live=True
        )

    assert len(runtime.fetch_calls) == 1


def _attachment_envelope(
    *,
    url="https://pubs.aip.org/Handlers/ArticleAbstractHandler.ashx",
    status=200,
    media_type="application/json",
    body=b'{"Success": true, "Html": "<p>Cached.</p>"}',
):
    import base64

    return json.dumps(
        {
            "schema": "papertrack-aip-abstract-response-v1",
            "url": url,
            "status_code": status,
            "media_type": media_type,
            "body_base64": base64.b64encode(body).decode(),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


@pytest.mark.parametrize(
    "invalid_body",
    [
        b'{"Success": true, "Html": "<p>Legacy raw.</p>"}',
        _attachment_envelope(url="https://evil.example/Handlers/ArticleAbstractHandler.ashx"),
        _attachment_envelope(url="http://pubs.aip.org/Handlers/ArticleAbstractHandler.ashx"),
        _attachment_envelope(url="https://pubs.aip.org/not-approved"),
        _attachment_envelope(status=302),
        _attachment_envelope(status=403, body=b"forbidden"),
        _attachment_envelope(media_type="text/plain"),
        _attachment_envelope(body=b"not-json"),
        _attachment_envelope(body=b'{"Success": false, "Html": ""}'),
    ],
)
def test_aip_rejects_untrusted_attachment_envelope(
    invalid_body, runtime, snapshot_store
):
    snapshot_store.commit(
        SnapshotKey("aip", "jcp", "160", "4"),
        "https://pubs.aip.org/aip/jcp/issue/160/4",
        "https://pubs.aip.org/aip/jcp/issue/160/4",
        "text/html",
        AIP_ISSUE_FIXTURE.encode(),
        SnapshotValidation(
            selector_counts={".al-article-items": 1},
            membership_dois=("10.1063/example",),
            parser_version="aip-issue-v1",
        ),
        (
            aip_fetch.SnapshotAttachment(
                "abstract-12345.json", "application/json", invalid_body
            ),
        ),
    )
    runtime.queue_issue_html(AIP_ISSUE_FIXTURE)
    runtime.queue_json({"Success": True, "Html": "<p>Fresh.</p>"})

    result = aip_fetch.query_aip_issue_result("jcp", "160", "4", "JCP", runtime)

    assert result.entries["10.1063/example"].abstract == "Fresh."
    assert len(runtime.fetch_calls) == 1


def test_aip_rejects_extra_snapshot_attachment(runtime, snapshot_store):
    snapshot_store.commit(
        SnapshotKey("aip", "jcp", "160", "4"),
        "https://pubs.aip.org/aip/jcp/issue/160/4",
        "https://pubs.aip.org/aip/jcp/issue/160/4",
        "text/html",
        AIP_ISSUE_FIXTURE.encode(),
        SnapshotValidation(
            selector_counts={".al-article-items": 1},
            membership_dois=("10.1063/example",),
            parser_version="aip-issue-v1",
        ),
        (
            aip_fetch.SnapshotAttachment(
                "abstract-12345.json", "application/json", _attachment_envelope()
            ),
            aip_fetch.SnapshotAttachment(
                "abstract-extra.json", "application/json", _attachment_envelope()
            ),
        ),
    )
    runtime.queue_issue_html(AIP_ISSUE_FIXTURE)
    runtime.queue_json({"Success": True, "Html": "<p>Fresh.</p>"})

    result = aip_fetch.query_aip_issue_result("jcp", "160", "4", "JCP", runtime)

    assert result.entries["10.1063/example"].abstract == "Fresh."
    assert len(runtime.fetch_calls) == 1


def test_aip_corrupt_attachment_is_rejected_then_refetched(
    monkeypatch, runtime, snapshot_store
):
    runtime.queue_issue_html(AIP_ISSUE_FIXTURE)
    runtime.queue_json({"Success": True, "Html": "<p>Fresh abstract.</p>"})
    stored = snapshot_store.commit(
        SnapshotKey("aip", "jcp", "160", "4"),
        "https://pubs.aip.org/aip/jcp/issue/160/4",
        "https://pubs.aip.org/aip/jcp/issue/160/4",
        "text/html",
        AIP_ISSUE_FIXTURE.encode(),
        SnapshotValidation(
            selector_counts={".al-article-items": 1},
            membership_dois=("10.1063/example",),
            parser_version="aip-issue-v1",
        ),
        (
            aip_fetch.SnapshotAttachment(
                "abstract-12345.json", "application/json", b"{}"
            ),
        ),
    )
    corrupted = stored.attachment_paths["abstract-12345.json"]
    corrupted.write_bytes(b"corrupt")
    events = []
    monkeypatch.setattr(
        aip_fetch,
        "record_fetch_attempt",
        lambda event, html=None: events.append(event),
    )

    result = aip_fetch.query_aip_issue_result("jcp", "160", "4", "JCP", runtime)

    assert result.entries["10.1063/example"].abstract == "Fresh abstract."
    assert len(runtime.fetch_calls) == 1
    rejection = [event for event in events if event["stage"] == "snapshot_validation"]
    assert rejection[0]["rejection_reason"] == "attachment_hash_failure"
    assert rejection[0]["cache_snapshot_path"] == str(corrupted)


def test_aip_failed_live_refetch_does_not_replace_validated_snapshot(runtime, snapshot_store):
    original = snapshot_store.commit(
        SnapshotKey("aip", "jcp", "160", "4"),
        "https://pubs.aip.org/aip/jcp/issue/160/4",
        "https://pubs.aip.org/aip/jcp/issue/160/4",
        "text/html",
        AIP_ISSUE_FIXTURE.encode(),
        SnapshotValidation(
            selector_counts={".al-article-items": 1},
            membership_dois=("10.1063/example",),
            parser_version="aip-issue-v1",
        ),
    )
    original.body_path.write_bytes(b"corrupt")
    manifest = snapshot_store.root / "aip/jcp/160/4/manifest.json"
    before = manifest.read_bytes()
    runtime.queue_issue_html("", outcome=BLOCKED)

    result = aip_fetch.query_aip_issue_result("jcp", "160", "4", "JCP", runtime)

    assert result.outcome == BLOCKED
    assert manifest.read_bytes() == before


def test_aip_force_live_rejects_disappearing_satisfied_abstract_control(
    runtime, snapshot_store
):
    key = SnapshotKey("aip", "jcp", "160", "4")
    snapshot_store.commit(
        key,
        "https://pubs.aip.org/aip/jcp/issue/160/4",
        "https://pubs.aip.org/aip/jcp/issue/160/4",
        "text/html",
        AIP_ISSUE_FIXTURE.encode(),
        SnapshotValidation(
            selector_counts={".al-article-items": 1},
            membership_dois=("10.1063/example",),
            parser_version="aip-issue-v1",
        ),
        (
            aip_fetch.SnapshotAttachment(
                "abstract-12345.json", "application/json", _attachment_envelope()
            ),
        ),
    )
    manifest = snapshot_store.root / "aip/jcp/160/4/manifest.json"
    before = manifest.read_bytes()
    live_without_control = AIP_ISSUE_FIXTURE.replace(
        '<a class="js-show-abstract" data-articleid="12345">Abstract</a>', ""
    )
    runtime.queue_issue_html(live_without_control)

    result = aip_fetch.query_aip_issue_result(
        "jcp", "160", "4", "JCP", runtime, force_live=True
    )

    assert result.outcome == FETCH_FAILED
    assert "abstract controls regressed" in result.message
    assert runtime.request_calls == []
    assert manifest.read_bytes() == before
    reloaded = snapshot_store.load(key)
    assert set(reloaded.attachment_paths) == {"abstract-12345.json"}
    assert runtime.stats.live_success == 0
    cached = aip_fetch.query_aip_issue_result(
        "jcp", "160", "4", "JCP", Runtime(snapshot_store)
    )
    assert cached.outcome == SUCCESS
    assert cached.entries["10.1063/example"].abstract == "Cached."


def test_aip_rejects_unsafe_article_id_without_abstract_request(monkeypatch, runtime):
    runtime.queue_issue_html(AIP_ISSUE_FIXTURE.replace("12345", "../bad"))
    events = []
    monkeypatch.setattr(
        aip_fetch, "record_fetch_attempt", lambda event, html=None: events.append(event)
    )

    result = aip_fetch.query_aip_issue_result("jcp", "160", "4", "JCP", runtime)

    assert result.outcome != SUCCESS
    assert runtime.request_calls == []
    event = next(event for event in events if event["stage"] == "issue_page_parse")
    assert event["journal"] == "jcp"
    assert event["volume"] == "160"
    assert event["issue"] == "4"
    assert event["url"] == "https://pubs.aip.org/aip/jcp/issue/160/4"
    assert event["critical_field_failures"][0]["field"] == "article_id"


def test_aip_rejects_invalid_official_membership_before_abstract_requests(
    monkeypatch, runtime
):
    runtime.queue_issue_html(AIP_ISSUE_FIXTURE.replace("10.1063/example", "10.9999/wrong"))
    events = []
    monkeypatch.setattr(
        aip_fetch, "record_fetch_attempt", lambda event, html=None: events.append(event)
    )

    result = aip_fetch.query_aip_issue_result("jcp", "160", "4", "JCP", runtime)

    assert result.outcome != SUCCESS
    assert runtime.request_calls == []
    event = next(event for event in events if event["stage"] == "issue_page_parse")
    assert event["outcome"] == PARSE_FAILED
    assert event["critical_field_failures"] == [
        {"field": "doi_membership", "doi": "10.9999/wrong"}
    ]


def test_aip_suspicious_empty_preflight_records_full_diagnostic(
    monkeypatch, runtime
):
    runtime.queue_issue_html("<html><body>No issue cards</body></html>")
    events = []
    monkeypatch.setattr(
        aip_fetch, "record_fetch_attempt", lambda event, html=None: events.append(event)
    )

    result = aip_fetch.query_aip_issue_result("jcp", "160", "4", "JCP", runtime)

    assert result.outcome == aip_fetch.OFFICIAL_EMPTY_SUSPICIOUS
    event = next(event for event in events if event["stage"] == "issue_page_parse")
    assert event["selector_counts"] == {".al-article-items": 0}
    assert event["card_count"] == 0
    assert event["journal"] == "jcp"
    assert event["volume"] == "160"
    assert event["issue"] == "4"
    assert event["url"] == "https://pubs.aip.org/aip/jcp/issue/160/4"


def test_aip_malformed_card_fails_entire_multi_card_issue(
    monkeypatch, runtime, snapshot_store
):
    malformed = """
    <div class="al-article-items">
      <h3 class="item-title"><a href="/aip/jcp/article/160/4/x/2/Bad">Bad card</a></h3>
      <a class="js-show-abstract" data-articleid="broken">Abstract</a>
    </div>
    """
    runtime.queue_issue_html(AIP_ISSUE_FIXTURE.replace("</body>", malformed + "</body>"))
    events = []
    monkeypatch.setattr(
        aip_fetch, "record_fetch_attempt", lambda event, html=None: events.append(event)
    )

    result = aip_fetch.query_aip_issue_result("jcp", "160", "4", "JCP", runtime)

    assert result.outcome == PARSE_FAILED
    assert result.entries == {}
    assert runtime.request_calls == []
    assert snapshot_store.load(SnapshotKey("aip", "jcp", "160", "4")) is None
    event = next(event for event in events if event["stage"] == "issue_page_parse")
    assert event["selector_counts"] == {".al-article-items": 2}
    assert event["card_count"] == 2
    assert event["critical_field_failures"] == [{"card": 2, "field": "doi"}]


def test_aip_duplicate_article_id_for_distinct_dois_fails_closed(
    monkeypatch, runtime, snapshot_store
):
    second = AIP_ISSUE_FIXTURE.replace(
        "10.1063/example", "10.1063/second"
    ).replace("Official article", "Second article")
    html = AIP_ISSUE_FIXTURE.replace("</body>", second + "</body>")
    runtime.queue_issue_html(html)
    events = []
    monkeypatch.setattr(
        aip_fetch, "record_fetch_attempt", lambda event, html=None: events.append(event)
    )

    result = aip_fetch.query_aip_issue_result("jcp", "160", "4", "JCP", runtime)

    assert result.outcome == PARSE_FAILED
    assert runtime.request_calls == []
    assert snapshot_store.load(SnapshotKey("aip", "jcp", "160", "4")) is None
    event = next(event for event in events if event["stage"] == "issue_page_parse")
    assert event["critical_field_failures"] == [
        {
            "field": "duplicate_article_id",
            "article_id": "12345",
            "dois": ["10.1063/example", "10.1063/second"],
        }
    ]


def test_aip_deliberately_excludes_known_non_article_card(runtime):
    excluded = """
    <div class="al-article-items">
      <h3 class="item-title"><a href="/aip/jcp/article/160/4/x/2/Info">Issue Publication Information</a></h3>
      <span class="citation-label"><a href="https://doi.org/10.1063/ctv160i004">10.1063/ctv160i004</a></span>
    </div>
    """
    runtime.queue_issue_html(AIP_ISSUE_FIXTURE.replace("</body>", excluded + "</body>"))
    runtime.queue_json({"Success": True, "Html": "<p>Official.</p>"})

    result = aip_fetch.query_aip_issue_result("jcp", "160", "4", "JCP", runtime)

    assert result.outcome == SUCCESS
    assert result.metadata["card_count"] == 2
    assert result.metadata["excluded_card_count"] == 1


def test_aip_abstract_transport_exception_fails_and_is_not_snapshotted(
    runtime, snapshot_store
):
    runtime.queue_issue_html(AIP_ISSUE_FIXTURE)
    runtime.queue_exception(OSError("offline"))

    result = aip_fetch.query_aip_issue_result("jcp", "160", "4", "JCP", runtime)

    assert result.outcome == FETCH_FAILED
    assert result.entries["10.1063/example"].abstract == ""
    assert result.metadata["missing_fields"][0]["outcome"] == FETCH_FAILED
    assert snapshot_store.load(SnapshotKey("aip", "jcp", "160", "4")) is None
    assert runtime.stats.live_success == 0


def test_aip_missing_abstract_response_fails_without_manifest(runtime, snapshot_store):
    runtime.queue_issue_html(AIP_ISSUE_FIXTURE)
    runtime.abstract_results.append(None)

    result = aip_fetch.query_aip_issue_result("jcp", "160", "4", "JCP", runtime)

    assert result.outcome == FETCH_FAILED
    assert result.metadata["snapshot_committed"] is False
    assert snapshot_store.load(SnapshotKey("aip", "jcp", "160", "4")) is None
    assert runtime.stats.live_success == 0


def test_aip_transport_retry_then_snapshot_reload_is_consistent(snapshot_store):
    failed_runtime = Runtime(snapshot_store)
    failed_runtime.queue_issue_html(AIP_ISSUE_FIXTURE)
    failed_runtime.queue_exception(OSError("offline"))
    failed = aip_fetch.query_aip_issue_result(
        "jcp", "160", "4", "JCP", failed_runtime
    )
    assert failed.outcome == FETCH_FAILED

    live_runtime = Runtime(snapshot_store)
    live_runtime.queue_issue_html(AIP_ISSUE_FIXTURE)
    live_runtime.queue_json({"Success": True, "Html": "<p>Stable.</p>"})
    live = aip_fetch.query_aip_issue_result("jcp", "160", "4", "JCP", live_runtime)

    reload_runtime = Runtime(snapshot_store)
    cached = aip_fetch.query_aip_issue_result(
        "jcp", "160", "4", "JCP", reload_runtime
    )
    assert live.outcome == cached.outcome == SUCCESS
    assert live.entries["10.1063/example"].abstract == "Stable."
    assert cached.entries["10.1063/example"].abstract == "Stable."
    assert reload_runtime.fetch_calls == []


def test_aip_abstract_transport_failure_preserves_prior_snapshot_manifest(
    snapshot_store,
):
    stored = snapshot_store.commit(
        SnapshotKey("aip", "jcp", "160", "4"),
        "https://pubs.aip.org/aip/jcp/issue/160/4",
        "https://pubs.aip.org/aip/jcp/issue/160/4",
        "text/html",
        AIP_ISSUE_FIXTURE.encode(),
        SnapshotValidation(
            selector_counts={".al-article-items": 1},
            membership_dois=("10.1063/example",),
            parser_version="aip-issue-v1",
        ),
        (
            aip_fetch.SnapshotAttachment(
                "abstract-12345.json", "application/json", _attachment_envelope()
            ),
        ),
    )
    stored.body_path.write_bytes(b"corrupt")
    manifest_path = snapshot_store.root / "aip/jcp/160/4/manifest.json"
    before = manifest_path.read_bytes()
    runtime = Runtime(snapshot_store)
    runtime.queue_issue_html(AIP_ISSUE_FIXTURE)
    runtime.queue_exception(OSError("offline"))

    result = aip_fetch.query_aip_issue_result("jcp", "160", "4", "JCP", runtime)

    assert result.outcome == FETCH_FAILED
    assert manifest_path.read_bytes() == before


@pytest.mark.parametrize("media_type", ["text/plain", ""])
def test_aip_non_200_error_envelope_is_not_durable(
    media_type, runtime, snapshot_store
):
    runtime.queue_issue_html(AIP_ISSUE_FIXTURE)
    runtime.queue_response(status=403, body=b"forbidden", media_type=media_type)

    live = aip_fetch.query_aip_issue_result("jcp", "160", "4", "JCP", runtime)
    assert live.outcome == BLOCKED
    assert live.metadata["missing_fields"][0]["outcome"] == BLOCKED
    assert snapshot_store.load(SnapshotKey("aip", "jcp", "160", "4")) is None
    assert runtime.stats.live_success == 0


@pytest.mark.parametrize(
    ("status", "expected_outcome"),
    [(429, RATE_LIMITED), (500, FETCH_FAILED)],
)
def test_aip_retryable_error_response_is_not_durable_and_later_run_is_live(
    status, expected_outcome, runtime, snapshot_store
):
    runtime.queue_issue_html(AIP_ISSUE_FIXTURE)
    runtime.queue_response(status=status, body=b"retry later", media_type="text/plain")

    failed = aip_fetch.query_aip_issue_result("jcp", "160", "4", "JCP", runtime)

    assert failed.outcome == expected_outcome
    assert snapshot_store.load(SnapshotKey("aip", "jcp", "160", "4")) is None
    assert runtime.stats.live_success == 0

    retry_runtime = Runtime(snapshot_store)
    retry_runtime.queue_issue_html(AIP_ISSUE_FIXTURE)
    retry_runtime.queue_json({"Success": True, "Html": "<p>Recovered.</p>"})
    recovered = aip_fetch.query_aip_issue_result(
        "jcp", "160", "4", "JCP", retry_runtime
    )

    assert recovered.outcome == SUCCESS
    assert recovered.entries["10.1063/example"].abstract == "Recovered."
    assert retry_runtime.stats.snapshot_hit == 0


def test_aip_records_rejected_capture_then_explicit_request_failure(
    monkeypatch, runtime
):
    runtime.queue_issue_html(
        AIP_ISSUE_FIXTURE,
        captured=(
            CapturedResponse(
                url=(
                    "https://pubs.aip.org/Handlers/ArticleAbstractHandler.ashx"
                    "?articleId=12345"
                ),
                status_code=200,
                media_type="text/plain",
                body=b"not supported",
            ),
        ),
    )
    runtime.queue_exception(OSError("retry offline"))
    events = []
    monkeypatch.setattr(
        aip_fetch, "record_fetch_attempt", lambda event, html=None: events.append(event)
    )

    result = aip_fetch.query_aip_issue_result("jcp", "160", "4", "JCP", runtime)

    assert result.outcome == FETCH_FAILED
    capture = next(
        event for event in events
        if event["stage"] == "abstract_capture_validation"
    )
    assert capture["article_id"] == "12345"
    assert capture["outcome"] == FETCH_FAILED
    assert "media type" in capture["message"]
    retry = next(event for event in events if event["stage"] == "abstract_fetch")
    assert retry["article_id"] == "12345"
    assert retry["outcome"] == FETCH_FAILED
    assert "retry offline" in retry["message"]

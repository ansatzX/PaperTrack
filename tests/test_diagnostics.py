import json

from papertrack.diagnostics import (
    configure_diagnostics,
    record_fetch_attempt,
    record_migration_event,
    record_repair_event,
)


def _jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_success_fetch_attempt_is_skipped_by_default(tmp_path):
    configure_diagnostics(diagnostics_dir=str(tmp_path))

    record_fetch_attempt(
        {
            "event_type": "fetch_attempt",
            "source": "journal",
            "provider": "aip",
            "outcome": "success",
            "article_count": 3,
        }
    )

    assert not (tmp_path / "attempts.jsonl").exists()


def test_success_fetch_attempt_is_recorded_when_all_attempts_enabled(tmp_path):
    configure_diagnostics(diagnostics_dir=str(tmp_path), all_attempts=True)

    record_fetch_attempt(
        {
            "event_type": "fetch_attempt",
            "source": "journal",
            "provider": "aip",
            "outcome": "success",
            "article_count": 3,
        }
    )

    events = _jsonl(tmp_path / "attempts.jsonl")
    assert events[0]["outcome"] == "success"
    assert events[0]["article_count"] == 3


def test_configure_diagnostics_resets_all_attempts_between_calls(tmp_path):
    configure_diagnostics(diagnostics_dir=str(tmp_path), all_attempts=True)
    configure_diagnostics(diagnostics_dir=str(tmp_path))

    record_fetch_attempt(
        {
            "event_type": "fetch_attempt",
            "source": "journal",
            "provider": "aip",
            "outcome": "success",
        }
    )

    assert not (tmp_path / "attempts.jsonl").exists()


def test_failed_fetch_attempt_records_jsonl_and_full_html_snapshot(tmp_path):
    configure_diagnostics(diagnostics_dir=str(tmp_path))
    html = "<html><body>Just a moment</body></html>"

    record_fetch_attempt(
        {
            "event_type": "fetch_attempt",
            "source": "journal",
            "provider": "aip",
            "journal": "jcp",
            "volume": "164",
            "issue": "16",
            "outcome": "blocked",
            "stage": "issue_page_fetch",
        },
        html=html,
    )

    events = _jsonl(tmp_path / "attempts.jsonl")
    event = events[0]
    assert event["outcome"] == "blocked"
    assert event["content_hash"].startswith("sha256:")
    snapshot_path = tmp_path / event["snapshot_path"]
    assert snapshot_path.read_text(encoding="utf-8") == html


def test_fallback_used_is_recorded_even_when_outcome_is_success(tmp_path):
    configure_diagnostics(diagnostics_dir=str(tmp_path))

    record_fetch_attempt(
        {
            "event_type": "fetch_attempt",
            "source": "journal",
            "provider": "aip",
            "outcome": "success",
            "fallback_used": ["crossref_abstract_fill"],
        }
    )

    events = _jsonl(tmp_path / "attempts.jsonl")
    assert events[0]["fallback_used"] == ["crossref_abstract_fill"]


def test_migration_and_repair_events_are_written(tmp_path):
    configure_diagnostics(diagnostics_dir=str(tmp_path))

    record_migration_event({"event_type": "migration", "outcome": "migrated"})
    record_repair_event({"event_type": "repair", "outcome": "fixed"})

    assert _jsonl(tmp_path / "migrations.jsonl")[0]["outcome"] == "migrated"
    assert _jsonl(tmp_path / "repairs.jsonl")[0]["outcome"] == "fixed"


def test_jsonl_event_uses_one_atomic_append_write(tmp_path, monkeypatch):
    from papertrack import diagnostics

    configure_diagnostics(diagnostics_dir=str(tmp_path))
    writes = []
    closes = []
    monkeypatch.setattr(diagnostics.os, "open", lambda *args, **kwargs: 17)
    monkeypatch.setattr(
        diagnostics.os,
        "write",
        lambda fd, payload: writes.append((fd, payload)) or len(payload),
    )
    monkeypatch.setattr(diagnostics.os, "close", closes.append)

    record_migration_event({"outcome": "migrated", "journal": "测试"})

    assert len(writes) == 1
    assert writes[0][0] == 17
    assert writes[0][1].endswith(b"\n")
    assert json.loads(writes[0][1].decode("utf-8"))["journal"] == "测试"
    assert closes == [17]

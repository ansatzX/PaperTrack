from papertrack.adapter_result import AdapterResult, CIRCUIT_OPEN, SUCCESS
from papertrack.aps_fetch import ApsRecentParseResult
from papertrack.aps_repair import (
    ABSTRACT_COMPLETE,
    ISSUE_CACHED,
    ISSUE_PENDING,
    ApsRepairPlan,
    cache_issues,
    index_recent_abstracts,
    render_complete_issues,
)
from papertrack.journal_entry import JournalEntry


def _entry(doi: str, date: str) -> JournalEntry:
    return JournalEntry(
        doi=doi, title="title", authors=[], journal_name="PRL", date=date,
        volume="1", issue="1", pages="", abstract="",
    )


def test_circuit_skip_leaves_unvisited_issue_pending(tmp_path, monkeypatch):
    plan = ApsRepairPlan.from_candidates("prl", [("1", "1"), ("1", "2")])
    path = tmp_path / "plan.json"
    calls = []

    def query(*args, **kwargs):
        calls.append(args[2])
        return AdapterResult(entries={}, outcome=CIRCUIT_OPEN, provider="aps", source="journal")

    monkeypatch.setattr("papertrack.aps_repair.query_aps_issue_result", query)
    cache_issues(plan, "PRL", object(), path)

    assert calls == ["2"]
    assert plan.issues["1/2"].issue_status == ISSUE_PENDING
    assert plan.issues["1/1"].issue_status == ISSUE_PENDING


def test_global_recent_scan_exact_doi_joins_multiple_issues(tmp_path, monkeypatch):
    plan = ApsRepairPlan.from_candidates("prl", [("1", "1"), ("1", "2")])
    path = tmp_path / "plan.json"
    entries = {
        ("1", "1"): {"10.1103/One": _entry("10.1103/One", "2020-01-01")},
        ("1", "2"): {"10.1103/Two": _entry("10.1103/Two", "2020-02-01")},
    }

    def query(slug, volume, issue, *args, **kwargs):
        return AdapterResult(entries=entries[(volume, issue)], outcome=SUCCESS, provider="aps", source="journal")

    recent_calls = []
    def recent(slug, page, runtime):
        recent_calls.append(page)
        return ApsRecentParseResult(
            {"10.1103/one": "abstract one", "10.1103/two": "abstract two"},
            False, "2020-01-01",
        ), "snapshot"

    monkeypatch.setattr("papertrack.aps_repair.query_aps_issue_result", query)
    monkeypatch.setattr("papertrack.aps_repair.fetch_aps_recent_page", recent)
    cache_issues(plan, "PRL", object(), path)
    index_recent_abstracts(plan, "PRL", object(), path)

    assert recent_calls == [1]
    assert all(item.issue_status == ISSUE_CACHED for item in plan.issues.values())
    assert all(item.abstract_status == ABSTRACT_COMPLETE for item in plan.issues.values())


def test_render_reads_issue_snapshot_only(tmp_path, monkeypatch):
    plan = ApsRepairPlan.from_candidates("prl", [("1", "1")])
    state = plan.issues["1/1"]
    state.issue_status = ISSUE_CACHED
    state.abstract_status = ABSTRACT_COMPLETE
    state.dois = ["10.1103/one"]
    state.abstracts = {"10.1103/one": "full abstract"}
    path = tmp_path / "plan.json"
    observed = {}

    def query(*args, **kwargs):
        observed["snapshot_only"] = kwargs.get("snapshot_only")
        return AdapterResult(
            {"10.1103/One": _entry("10.1103/One", "2020-01-01")}, SUCCESS, "aps", "journal"
        )

    def render(**kwargs):
        observed["abstract"] = next(iter(kwargs["entries"].values())).abstract
        return True

    monkeypatch.setattr("papertrack.aps_repair.query_aps_issue_result", query)
    monkeypatch.setattr("papertrack.aps_repair.write_journal_entries_to_md", render)
    render_complete_issues(plan, "PRL", "", str(tmp_path), object(), path)

    assert observed == {"snapshot_only": True, "abstract": "full abstract"}

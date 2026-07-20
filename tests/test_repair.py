import json

from papertrack.repair import RepairCandidate, collect_repair_candidates, repair_reports
from papertrack.source_policy import CrossrefMode, SourcePolicy


def _valid_journal_report() -> str:
    return """---
papertrack_format: journal-v2
papertrack_source: journal
papertrack_provider: aip
papertrack_journal: jcp
papertrack_volume: "164"
papertrack_issue: "16"
tags:
  - jcp-v164-i16
---

# Journal of Chemical Physics Vol. 164, Iss. 16 - 2026

```dataview
TASK
from #jcp-v164-i16

WHERE completed

```

## collected

## not collected

### 10.1063/example

Links:

- [ ] [10.1063/example](https://doi.org/10.1063/example)

Title:  Repaired article
"""


def _events(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_collect_repair_candidates_finds_corrupt_journal_report(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    report_file = tmp_path / "data" / "aip" / "jcp" / "164" / "16.md"
    report_file.parent.mkdir(parents=True)
    report_file.write_text("# broken\n", encoding="utf-8")

    candidates = collect_repair_candidates(str(tmp_path / "data"))

    assert candidates == [
        RepairCandidate(
            path=str(report_file),
            source="journal",
            reason="corrupt",
            provider="aip",
            journal_slug="jcp",
            volume="164",
            issue="16",
        )
    ]


def test_repair_reports_no_fetch_records_deferred(tmp_path):
    candidate = RepairCandidate(
        path=str(tmp_path / "data" / "aip" / "jcp" / "164" / "16.md"),
        source="journal",
        reason="corrupt",
        provider="aip",
        journal_slug="jcp",
        volume="164",
        issue="16",
    )

    results = repair_reports(str(tmp_path / "data"), [candidate], no_fetch=True)

    assert results[0].outcome == "deferred"
    events = _events(tmp_path / "diagnostics" / "repairs.jsonl")
    assert events[0]["outcome"] == "deferred"
    assert events[0]["fetch_attempted"] is False


def test_repair_reports_fetches_and_records_fixed_journal(tmp_path, monkeypatch):
    from papertrack import repair, report

    monkeypatch.setattr(
        repair,
        "journal_configs",
        {
            "jcp": {
                "name": "Journal of Chemical Physics",
                "slug": "jcp",
                "issn": "0021-9606",
                "provider": "aip",
            }
        },
    )
    candidate = RepairCandidate(
        path=str(tmp_path / "data" / "aip" / "jcp" / "164" / "16.md"),
        source="journal",
        reason="corrupt",
        provider="aip",
        journal_slug="jcp",
        volume="164",
        issue="16",
    )

    captured = {}

    def fake_filter_journal_to_md(**kwargs):
        captured.update(kwargs)
        target = tmp_path / "data" / "aip" / "jcp" / "164" / "16.md"
        target.parent.mkdir(parents=True)
        target.write_text(_valid_journal_report(), encoding="utf-8")

    monkeypatch.setattr(report, "filter_journal_to_md", fake_filter_journal_to_md)

    results = repair_reports(
        str(tmp_path / "data"),
        [candidate],
        assume_yes=True,
        source_policy=SourcePolicy.official_only(),
    )

    assert results[0].outcome == "fixed"
    assert captured["source_policy"].crossref_mode is CrossrefMode.DISABLED
    events = _events(tmp_path / "diagnostics" / "repairs.jsonl")
    assert events[0]["outcome"] == "fixed"
    assert events[0]["fetch_attempted"] is True


def test_repair_reports_records_unresolved_when_fetch_does_not_fix(tmp_path, monkeypatch):
    from papertrack import repair, report

    monkeypatch.setattr(
        repair,
        "journal_configs",
        {
            "jcp": {
                "name": "Journal of Chemical Physics",
                "slug": "jcp",
                "issn": "0021-9606",
                "provider": "aip",
            }
        },
    )
    monkeypatch.setattr(report, "filter_journal_to_md", lambda **kwargs: None)
    candidate = RepairCandidate(
        path=str(tmp_path / "data" / "aip" / "jcp" / "164" / "16.md"),
        source="journal",
        reason="missing",
        provider="aip",
        journal_slug="jcp",
        volume="164",
        issue="16",
    )

    results = repair_reports(str(tmp_path / "data"), [candidate], assume_yes=True)

    assert results[0].outcome == "unresolved"
    events = _events(tmp_path / "diagnostics" / "repairs.jsonl")
    assert events[0]["outcome"] == "unresolved"
    assert events[0]["fetch_attempted"] is True


def test_repair_dispatches_no_fetch(monkeypatch, tmp_path):
    from papertrack import cli

    candidate = RepairCandidate(
        path=str(tmp_path / "data" / "aip" / "jcp" / "164" / "16.md"),
        source="journal",
        reason="corrupt",
        provider="aip",
        journal_slug="jcp",
        volume="164",
        issue="16",
    )
    captured = {}

    monkeypatch.setattr(cli, "ZoteroQuery", lambda: None)
    monkeypatch.setattr(cli, "collect_repair_candidates", lambda data_dir: [candidate])

    def fake_repair_reports(
        data_dir,
        candidates,
        zotero,
        assume_yes,
        no_fetch,
        source_policy,
    ):
        captured.update({
            "data_dir": data_dir,
            "candidates": candidates,
            "assume_yes": assume_yes,
            "no_fetch": no_fetch,
            "source_policy": source_policy,
        })
        return []

    monkeypatch.setattr(cli, "repair_reports", fake_repair_reports)
    monkeypatch.setattr(
        "sys.argv",
        [
            "papertrack",
            "repair",
            "--data-dir",
            str(tmp_path / "data"),
            "--no-fetch",
        ],
    )

    cli.main()

    assert captured == {
        "data_dir": str(tmp_path / "data"),
        "candidates": [candidate],
        "assume_yes": False,
        "no_fetch": True,
        "source_policy": SourcePolicy.official_only(),
    }

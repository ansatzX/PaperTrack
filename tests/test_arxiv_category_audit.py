from datetime import date
from pathlib import Path

import pytest

from papertrack.adapter_result import AdapterResult, FETCH_FAILED, SUCCESS
from papertrack.arxiv_entry import ArxivEntry
from scripts import audit_arxiv_category_membership as audit_module
from scripts.audit_arxiv_category_membership import audit_category_membership


def _entry(arxiv_id: str) -> ArxivEntry:
    return ArxivEntry(
        arxiv_id=arxiv_id,
        title=arxiv_id,
        authors=["Author"],
        abstract="Abstract",
        subjects=["cs.AI"],
    )


def _write_report(root: Path, day: date, ids: list[str]) -> Path:
    path = root / "arxiv" / "cs.AI" / f"{day.year:04d}" / f"{day.month:02d}" / f"{day.day:02d}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    headings = "\n\n".join(f"### {arxiv_id}\n" for arxiv_id in ids)
    path.write_text(
        f"## collected\n\n## not collected\n\n{headings}",
        encoding="utf-8",
    )
    return path


def test_audit_classifies_differences_without_writing_markdown(tmp_path):
    days = [date(2026, 7, day) for day in range(1, 5)]
    reports = [_write_report(tmp_path, day, ["arXiv:local-a"]) for day in days]
    before = {path: path.read_bytes() for path in reports}
    official = {
        "2026-07-01": ["arXiv:local-a"],
        "2026-07-02": ["arXiv:local-a", "arXiv:official-b"],
        "2026-07-03": ["arXiv:official-b"],
    }
    calls = []

    def fake_query(
        date_from,
        date_to,
        query_args,
        required_category="",
        request_delay=None,
    ):
        calls.append((date_from, date_to, required_category, request_delay))
        if date_from == "2026-07-04":
            return AdapterResult(
                entries={},
                outcome=FETCH_FAILED,
                provider="arxiv",
                source="arxiv",
                message="network down",
            )
        return AdapterResult(
            entries={arxiv_id: _entry(arxiv_id) for arxiv_id in official[date_from]},
            outcome=SUCCESS,
            provider="arxiv",
            source="arxiv",
        )

    manifest = audit_category_membership(
        data_dir=tmp_path,
        categories=["cs.AI"],
        from_date=days[0],
        to_date=days[-1],
        request_interval=7,
        query=fake_query,
    )

    assert manifest == {
        "exact": [
            {
                "category": "cs.AI",
                "date": "2026-07-01",
                "local_ids": ["arXiv:local-a"],
                "official_ids": ["arXiv:local-a"],
            }
        ],
        "missing_official": [
            {
                "category": "cs.AI",
                "date": "2026-07-02",
                "missing_ids": ["arXiv:official-b"],
                "local_ids": ["arXiv:local-a"],
                "official_ids": ["arXiv:local-a", "arXiv:official-b"],
            },
            {
                "category": "cs.AI",
                "date": "2026-07-03",
                "missing_ids": ["arXiv:official-b"],
                "local_ids": ["arXiv:local-a"],
                "official_ids": ["arXiv:official-b"],
            },
        ],
        "non_member_local": [
            {
                "category": "cs.AI",
                "date": "2026-07-03",
                "non_member_ids": ["arXiv:local-a"],
                "local_ids": ["arXiv:local-a"],
                "official_ids": ["arXiv:official-b"],
            }
        ],
        "fetch_failed": [
            {
                "category": "cs.AI",
                "date": "2026-07-04",
                "outcome": FETCH_FAILED,
                "message": "network down",
            }
        ],
    }
    assert calls == [
        ("2026-07-01", "2026-07-02", "cs.AI", 7),
        ("2026-07-02", "2026-07-03", "cs.AI", 7),
        ("2026-07-03", "2026-07-04", "cs.AI", 7),
        ("2026-07-04", "2026-07-05", "cs.AI", 7),
    ]
    assert {path: path.read_bytes() for path in reports} == before


def test_audit_skips_dates_without_local_reports(tmp_path):
    report = _write_report(tmp_path, date(2026, 7, 2), ["arXiv:local-a"])
    calls = []

    def fake_query(date_from, date_to, query_args, **kwargs):
        calls.append(date_from)
        return AdapterResult(
            entries={"arXiv:local-a": _entry("arXiv:local-a")},
            outcome=SUCCESS,
            provider="arxiv",
            source="arxiv",
        )

    audit_category_membership(
        data_dir=tmp_path,
        categories=["cs.AI"],
        from_date=date(2026, 7, 1),
        to_date=date(2026, 7, 3),
        request_interval=0,
        query=fake_query,
    )

    assert calls == ["2026-07-02"]
    assert report.exists()


@pytest.mark.parametrize(
    "relative_path",
    [
        Path("arxiv/cs.AI/2026/07/02/cs.AI/02.md"),
        Path("arxiv/cs.AI/2026/07/cs.AI/02.md"),
    ],
)
def test_audit_discovers_supported_alternate_report_layouts(tmp_path, relative_path):
    report = tmp_path / relative_path
    report.parent.mkdir(parents=True)
    report.write_text(
        "## collected\n\n## not collected\n\n### arXiv:local-a\n",
        encoding="utf-8",
    )
    calls = []

    def fake_query(date_from, date_to, query_args, **kwargs):
        calls.append(date_from)
        return AdapterResult(
            entries={"arXiv:local-a": _entry("arXiv:local-a")},
            outcome=SUCCESS,
            provider="arxiv",
            source="arxiv",
        )

    manifest = audit_category_membership(
        data_dir=tmp_path,
        categories=["cs.AI"],
        from_date=date(2026, 7, 2),
        to_date=date(2026, 7, 2),
        request_interval=0,
        query=fake_query,
    )

    assert calls == ["2026-07-02"]
    assert len(manifest["exact"]) == 1


def test_cli_rejects_manifest_output_inside_arxiv_ledger(tmp_path, monkeypatch):
    report = _write_report(tmp_path, date(2026, 7, 2), ["arXiv:local-a"])
    before = report.read_bytes()
    monkeypatch.setattr(
        audit_module,
        "audit_category_membership",
        lambda **kwargs: pytest.fail("audit must not start for an unsafe output path"),
    )

    with pytest.raises(SystemExit, match="outside the arXiv ledger"):
        audit_module.main([
            "--data-dir", str(tmp_path),
            "--categories", "cs.AI",
            "--from-date", "2026-07-02",
            "--to-date", "2026-07-02",
            "--output", str(report),
        ])

    assert report.read_bytes() == before


def test_cli_rejects_markdown_manifest_output(tmp_path, monkeypatch):
    output = tmp_path / "membership.md"
    monkeypatch.setattr(
        audit_module,
        "audit_category_membership",
        lambda **kwargs: pytest.fail("audit must not start for a Markdown output path"),
    )

    with pytest.raises(SystemExit, match="must not be Markdown"):
        audit_module.main([
            "--data-dir", str(tmp_path / "data"),
            "--categories", "cs.AI",
            "--from-date", "2026-07-02",
            "--to-date", "2026-07-02",
            "--output", str(output),
        ])

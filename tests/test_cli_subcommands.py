from datetime import date

import pytest

from papertrack.source_policy import CrossrefMode


def test_arxiv_refresh_dispatches_rolling_days(monkeypatch, tmp_path):
    from papertrack import cli

    captured = {}

    class FakeZotero:
        pass

    monkeypatch.setattr(cli, "ZoteroQuery", lambda: FakeZotero())
    monkeypatch.setattr(cli, "query_args", {"quant-ph": {"size": "200"}})
    monkeypatch.setattr(
        cli,
        "filter_arxiv_days_to_md",
        lambda **kwargs: captured.update(kwargs),
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "papertrack",
            "arxiv",
            "refresh",
            "--category",
            "quant-ph",
            "--anchor-date",
            "2026-06-09",
            "--refresh-days",
            "2",
            "--data-dir",
            str(tmp_path),
        ],
    )

    cli.main()

    assert captured["days"] == [
        date(2026, 6, 7),
        date(2026, 6, 8),
        date(2026, 6, 9),
    ]
    assert captured["md_folder"] == str(tmp_path / "arxiv" / "quant-ph")
    assert captured["category"] == "quant-ph"
    assert captured["query_args"] == {"size": "200"}
    assert isinstance(captured["zotero"], FakeZotero)


def test_journal_issue_passes_official_only_source_policy(monkeypatch, tmp_path):
    from papertrack import cli

    captured = {}
    monkeypatch.delenv("PAPERTRACK_CROSSREF_MODE", raising=False)
    monkeypatch.setattr(cli, "ZoteroQuery", lambda: None)
    monkeypatch.setattr(
        cli,
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
    monkeypatch.setattr(
        cli,
        "filter_journal_to_md",
        lambda **kwargs: captured.update(kwargs),
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "papertrack",
            "journal",
            "issue",
            "--journal",
            "jcp",
            "--volume",
            "164",
            "--issue",
            "16",
            "--data-dir",
            str(tmp_path),
        ],
    )

    cli.main()

    assert captured["source_policy"].crossref_mode is CrossrefMode.DISABLED


def test_invalid_source_policy_stops_before_journal_handler(monkeypatch, tmp_path):
    from papertrack import cli

    calls = []
    monkeypatch.setenv("PAPERTRACK_CROSSREF_MODE", "membership")
    monkeypatch.setattr(cli, "ZoteroQuery", lambda: None)
    monkeypatch.setattr(
        cli,
        "filter_journal_to_md",
        lambda **kwargs: calls.append(kwargs),
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "papertrack",
            "journal",
            "issue",
            "--journal",
            "jcp",
            "--volume",
            "164",
            "--issue",
            "16",
            "--data-dir",
            str(tmp_path),
        ],
    )

    with pytest.raises(RuntimeError, match="PAPERTRACK_CROSSREF_MODE"):
        cli.main()

    assert calls == []


def test_metadata_source_policy_reaches_journal_handler(monkeypatch, tmp_path):
    from papertrack import cli

    captured = {}
    monkeypatch.setenv("PAPERTRACK_CROSSREF_MODE", "metadata")
    monkeypatch.setattr(cli, "ZoteroQuery", lambda: None)
    monkeypatch.setattr(cli, "filter_journal_to_md", lambda **kwargs: captured.update(kwargs))
    monkeypatch.setattr(
        "sys.argv",
        [
            "papertrack",
            "journal",
            "issue",
            "--journal",
            "jcp",
            "--volume",
            "164",
            "--issue",
            "16",
            "--data-dir",
            str(tmp_path),
        ],
    )

    cli.main()

    assert captured["source_policy"].crossref_mode is CrossrefMode.METADATA


def test_arxiv_backfill_month_dispatches_month_days(monkeypatch, tmp_path):
    from papertrack import cli

    captured = {}

    monkeypatch.setattr(cli, "ZoteroQuery", lambda: None)
    monkeypatch.setattr(cli, "query_args", {"quant-ph": {"size": "200"}})
    monkeypatch.setattr(
        cli,
        "filter_arxiv_days_to_md",
        lambda **kwargs: captured.update(kwargs),
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "papertrack",
            "arxiv",
            "backfill",
            "--category",
            "quant-ph",
            "--month",
            "2026-02",
            "--data-dir",
            str(tmp_path),
        ],
    )

    cli.main()

    assert captured["days"][0] == date(2026, 2, 1)
    assert captured["days"][-1] == date(2026, 2, 28)
    assert len(captured["days"]) == 28


def test_arxiv_backfill_date_range_dispatches_exact_range(monkeypatch, tmp_path):
    from papertrack import cli

    captured = {}

    monkeypatch.setattr(cli, "ZoteroQuery", lambda: None)
    monkeypatch.setattr(cli, "query_args", {"quant-ph": {"size": "200"}})
    monkeypatch.setattr(
        cli,
        "filter_arxiv_days_to_md",
        lambda **kwargs: captured.update(kwargs),
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "papertrack",
            "arxiv",
            "backfill",
            "--category",
            "quant-ph",
            "--from-date",
            "2026-06-01",
            "--to-date",
            "2026-06-03",
            "--data-dir",
            str(tmp_path),
        ],
    )

    cli.main()

    assert captured["days"] == [
        date(2026, 6, 1),
        date(2026, 6, 2),
        date(2026, 6, 3),
    ]


def test_global_diagnostics_options_configure_diagnostics(monkeypatch, tmp_path):
    from papertrack import cli

    captured = {}
    diagnostics_dir = tmp_path / "diag"

    monkeypatch.setattr(cli, "ZoteroQuery", lambda: None)
    monkeypatch.setattr(cli, "query_args", {"quant-ph": {"size": "200"}})
    monkeypatch.setattr(cli, "filter_arxiv_days_to_md", lambda **kwargs: None)
    monkeypatch.setattr(cli, "configure_diagnostics", lambda **kwargs: captured.update(kwargs))
    monkeypatch.setattr(
        "sys.argv",
        [
            "papertrack",
            "--diagnostics-dir",
            str(diagnostics_dir),
            "--diagnostics-all-attempts",
            "--no-failure-snapshots",
            "arxiv",
            "refresh",
            "--category",
            "quant-ph",
            "--anchor-date",
            "2026-06-09",
            "--refresh-days",
            "0",
            "--data-dir",
            str(tmp_path),
        ],
    )

    cli.main()

    assert captured == {
        "diagnostics_dir": str(diagnostics_dir),
        "all_attempts": True,
        "failure_snapshots": False,
    }


def test_arxiv_backfill_rejects_missing_range(monkeypatch, tmp_path):
    from papertrack import cli

    monkeypatch.setattr(cli, "ZoteroQuery", lambda: None)
    monkeypatch.setattr(
        "sys.argv",
        [
            "papertrack",
            "arxiv",
            "backfill",
            "--category",
            "quant-ph",
            "--data-dir",
            str(tmp_path),
        ],
    )

    with pytest.raises(
        RuntimeError,
        match="exactly one of --month or --from-date/--to-date",
    ):
        cli.main()


def test_arxiv_backfill_rejects_month_plus_range(monkeypatch, tmp_path):
    from papertrack import cli

    monkeypatch.setattr(cli, "ZoteroQuery", lambda: None)
    monkeypatch.setattr(
        "sys.argv",
        [
            "papertrack",
            "arxiv",
            "backfill",
            "--category",
            "quant-ph",
            "--month",
            "2026-06",
            "--from-date",
            "2026-06-01",
            "--to-date",
            "2026-06-03",
            "--data-dir",
            str(tmp_path),
        ],
    )

    with pytest.raises(
        RuntimeError,
        match="exactly one of --month or --from-date/--to-date",
    ):
        cli.main()


def test_arxiv_backfill_rejects_incomplete_range(monkeypatch, tmp_path):
    from papertrack import cli

    monkeypatch.setattr(cli, "ZoteroQuery", lambda: None)
    monkeypatch.setattr(
        "sys.argv",
        [
            "papertrack",
            "arxiv",
            "backfill",
            "--category",
            "quant-ph",
            "--from-date",
            "2026-06-01",
            "--data-dir",
            str(tmp_path),
        ],
    )

    with pytest.raises(
        RuntimeError,
        match="requires both --from-date and --to-date",
    ):
        cli.main()


def test_migrate_dispatches_data_dir(monkeypatch, tmp_path):
    from papertrack import cli

    captured = {}

    monkeypatch.setattr(cli, "ZoteroQuery", lambda: None)
    def fake_migrate_reports(data_dir):
        captured["data_dir"] = data_dir
        return []

    monkeypatch.setattr(cli, "migrate_reports", fake_migrate_reports)
    monkeypatch.setattr(
        "sys.argv",
        [
            "papertrack",
            "migrate",
            "--data-dir",
            str(tmp_path),
        ],
    )

    cli.main()

    assert captured["data_dir"] == str(tmp_path)

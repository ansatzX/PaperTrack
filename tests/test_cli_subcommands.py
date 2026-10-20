from datetime import date

import pytest


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
    assert captured["refresh_existing"] is True


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
    assert captured["refresh_existing"] is False


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


def test_migrate_journal_layout_does_not_parse_sources(monkeypatch, tmp_path):
    from papertrack import cli
    from papertrack.reports import layout as journal_layout

    captured = {}
    monkeypatch.setattr(cli, 'ZoteroQuery', lambda: None)
    def migrate(data_dir, backup_dir):
        captured.update(data_dir=data_dir, backup_dir=backup_dir)
        return []
    monkeypatch.setattr(journal_layout, 'migrate_journal_layout', migrate)
    monkeypatch.setattr(cli, 'migrate_reports', lambda *args: pytest.fail('unexpected content migration'))
    monkeypatch.setattr('sys.argv', ['papertrack', 'migrate', '--journal-layout-only',
                                   '--data-dir', str(tmp_path), '--backup-dir', str(tmp_path.parent / 'backup')])
    cli.main()
    assert captured == dict(data_dir=str(tmp_path), backup_dir=str(tmp_path.parent / 'backup'))


def test_migrate_provenance_only_does_not_run_source_migration(monkeypatch, tmp_path):
    from papertrack import cli

    captured = {}
    monkeypatch.setattr(cli, "ZoteroQuery", lambda: None)
    def cleanup(data_dir, backup_dir):
        captured.update(data_dir=data_dir, backup_dir=backup_dir)
        return []
    monkeypatch.setattr(cli, 'remove_report_provenance', cleanup)
    monkeypatch.setattr(cli, 'migrate_reports', lambda *args: pytest.fail('unexpected source migration'))
    monkeypatch.setattr('sys.argv', ['papertrack', 'migrate', '--remove-provenance-only',
                                   '--data-dir', str(tmp_path), '--backup-dir', str(tmp_path / 'backup')])
    cli.main()
    assert captured == dict(data_dir=str(tmp_path), backup_dir=str(tmp_path / 'backup'))

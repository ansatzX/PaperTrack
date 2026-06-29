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

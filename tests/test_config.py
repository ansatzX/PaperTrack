from pathlib import Path

import pytest

from papertrack import config as codex
from papertrack.config import get_query_args, official_arxiv_category
from papertrack.arxiv.fetch import _advanced_search_url


def test_category_term_queries_include_primary_and_cross_list_membership():
    query_args = get_query_args()

    for category in ("chem-ph", "cs.AI", "cs.LG"):
        args = query_args[category]
        expected_term = "physics.chem-ph" if category == "chem-ph" else category
        assert args["terms-0-term"] == ""
        assert args["terms-0-field"] == "title"
        assert args["terms-1-term"] == expected_term
        assert args["terms-1-field"] == "all"
        assert "cross_list_category" not in args.values()
        assert args["date-date_type"] == "submitted_date_first"

    assert query_args["chem-ph"]["classification-physics"] == "y"
    assert query_args["chem-ph"]["classification-physics_archives"] == "physics"
    assert query_args["cs.AI"]["classification-computer_science"] == "y"
    assert query_args["cs.LG"]["classification-computer_science"] == "y"
    assert "classification-computer_science_archives" not in query_args["cs.AI"]
    assert "classification-computer_science_archives" not in query_args["cs.LG"]


def test_category_term_arxiv_urls_use_all_fields_candidate_query():
    query_args = get_query_args()

    for category in ("chem-ph", "cs.AI", "cs.LG"):
        args = dict(query_args[category])
        args["date-from_date"] = "2026-06-01"
        args["date-to_date"] = "2026-06-02"

        first_page = _advanced_search_url(args, 0)
        second_page = _advanced_search_url(args, 200)

        expected_term = "physics.chem-ph" if category == "chem-ph" else category
        assert "terms-0-term=&" in first_page
        assert "terms-0-field=title" in first_page
        assert f"terms-1-term={expected_term}" in first_page
        assert "terms-1-field=all" in first_page
        assert "cross_list_category" not in first_page
        assert "classification-computer_science_archives" not in first_page
        assert f"terms-1-term={expected_term}" in second_page
        assert "start=200" in second_page


def test_hardcoded_fallback_matches_toml_category_queries(monkeypatch):
    configured = get_query_args()
    monkeypatch.setattr(codex, "_load_toml_categories", lambda: {})

    fallback = codex.build_query_args()

    assert fallback == configured


def test_category_config_rejects_colliding_generated_tag_names(monkeypatch):
    monkeypatch.setattr(
        codex,
        "_load_toml_categories",
        lambda: {"cs.AI": {}, "cs-ai": {}},
    )

    with pytest.raises(ValueError, match="generated report tag collision"):
        codex.build_query_args()


def test_journal_config_rejects_colliding_generated_tag_slugs(monkeypatch):
    monkeypatch.setattr(
        codex,
        "_load_toml_journals",
        lambda: {"first": {"slug": "JCP"}, "second": {"slug": "jcp"}},
    )
    monkeypatch.setattr(codex, "_journals", None)

    for _ in range(2):
        with pytest.raises(ValueError, match="generated report tag collision"):
            codex.get_journals()


def test_journal_config_rejects_invalid_generated_tag_slug(monkeypatch):
    monkeypatch.setattr(
        codex,
        "_load_toml_journals",
        lambda: {"bad": {"slug": "journal name"}},
    )
    monkeypatch.setattr(codex, "_journals", None)

    with pytest.raises(ValueError, match="invalid generated report tag"):
        codex.get_journals()


def test_ledger_category_maps_to_official_arxiv_subject():
    assert official_arxiv_category("chem-ph") == "physics.chem-ph"
    assert official_arxiv_category("cs.AI") == "cs.AI"
    assert official_arxiv_category("quant-ph") == "quant-ph"


ROOT = Path(__file__).resolve().parents[1]


def test_daily_arxiv_throttle_stops_other_categories(monkeypatch, tmp_path):
    from papertrack import cli
    monkeypatch.setenv('XDG_RUNTIME_DIR', str(tmp_path))
    monkeypatch.setattr(cli, '_build_zotero', lambda: None)
    calls = []
    def limited(task, zotero):
        calls.append(task.category)
        raise cli.ArxivRateLimitError('arXiv rate limited')
    monkeypatch.setattr(cli, '_run_arxiv_backfill', limited)
    args = cli._build_parser().parse_args([
        '--diagnostics-dir', str(tmp_path/'logs'), 'refresh', '--data-dir', str(tmp_path),
        '--journals', '', '--category', 'chem-ph,quant-ph,cs.AI,cs.LG'])
    with pytest.raises(RuntimeError, match='Refresh failed for: chem-ph'):
        cli._run_refresh(args, None)
    assert calls == ['chem-ph']
    assert 'stopping remaining categories' in (tmp_path/'logs/daily.log').read_text()


def test_daily_explicit_issue_repair_is_bounded_and_failure_does_not_skip_latest(monkeypatch, tmp_path):
    from papertrack import cli
    monkeypatch.setenv('XDG_RUNTIME_DIR', str(tmp_path))
    calls = []
    def repair(task, zotero):
        calls.append((task.journal, task.volume, task.issue, task.journal_command))
        assert task.run_agent and not task.browser_snapshot
        raise RuntimeError('incomplete source')
    monkeypatch.setattr(cli, '_run_journal_issue', repair)
    monkeypatch.setattr(cli, '_run_journal_refresh', lambda task, z: calls.append(('latest', task.journal)))
    monkeypatch.setattr(cli, '_build_zotero', lambda: None)
    args = cli._build_parser().parse_args([
        '--diagnostics-dir', str(tmp_path/'logs'), 'refresh', '--data-dir', str(tmp_path),
        '--journals', 'prl', '--category', '',
        '--repair-issue', 'prl:136:1', '--repair-issue', 'prl:136:1'])
    with pytest.raises(RuntimeError, match='prl:136:1'):
        cli._run_refresh(args, None)
    assert calls == [('prl', '136', '1', 'issue'), ('latest', 'prl')]


@pytest.mark.parametrize('target', ['prl:../136:1', 'unknown:136:1', 'prl:136'])
def test_daily_rejects_invalid_repair_before_creating_output(tmp_path, target):
    from papertrack import cli
    output = tmp_path/'not-created'
    args = cli._build_parser().parse_args([
        'refresh', '--data-dir', str(output), '--journals', 'prl', '--repair-issue', target])
    with pytest.raises(ValueError, match='repair-issue'):
        cli._run_refresh(args, None)
    assert not output.exists()


def test_daily_refresh_continues_sources_after_a_failure(monkeypatch, tmp_path):
    from papertrack import cli
    monkeypatch.setenv('XDG_RUNTIME_DIR', str(tmp_path))
    calls = []
    def journal(task, zotero):
        calls.append(task.journal)
        if task.journal == 'jcim':
            raise RuntimeError('source unavailable')
    monkeypatch.setattr(cli, '_run_journal_refresh', journal)
    monkeypatch.setattr(cli, '_build_zotero', lambda: None)
    monkeypatch.setattr(cli, '_run_arxiv_refresh', lambda task, z: calls.append('refresh:'+task.category))
    monkeypatch.setattr(cli, '_run_arxiv_backfill', lambda task, z: calls.append('backfill:'+task.category))
    existing = tmp_path / 'arxiv/quant-ph'
    existing.mkdir(parents=True)
    (existing / 'day.md').write_text('existing reading state')
    legacy = tmp_path / 'acs/jctc/19/19/report.md'
    legacy.parent.mkdir(parents=True)
    legacy.write_text('''# JCTC Vol. 19, Iss. 19 — 2023

---
tags:
  - #jctc-v19-i19
---

```dataview
TASK
from #jctc-v19-i19
WHERE completed
```

## collected

## not collected

### 10.1021/example

- [x] [10.1021/example](https://doi.org/10.1021/example)

Title: Original title

Abstract:
> [!quote]- Abstract
> Original source text.
''')
    args = cli._build_parser().parse_args([
        '--diagnostics-dir', str(tmp_path/'logs'), 'refresh',
        '--data-dir', str(tmp_path), '--journals', 'jctc,jcim,jacs,jcp,prl',
        '--category', 'chem-ph,quant-ph,cs.AI,cs.LG'])
    with pytest.raises(RuntimeError, match='Refresh failed for: jcim'):
        cli._run_refresh(args, None)
    assert [c for c in calls if ':' not in c] == ['jctc','jcim','jacs','jcp','prl']
    assert [c for c in calls if ':' in c] == ['backfill:chem-ph',
                     'refresh:quant-ph','backfill:cs.AI','backfill:cs.LG']
    assert 'Journal refresh failed: jcim' in (tmp_path/'logs/daily.log').read_text()
    from papertrack.reports.migration import validate_report_content
    migrated = legacy.read_text()
    assert validate_report_content(migrated, source='journal')
    assert 'papertrack_provider: acs' in migrated
    assert '- [x] [10.1021/example]' in migrated
    assert '> Original source text.' in migrated
    assert (existing / 'day.md').read_text() == 'existing reading state'


def test_daily_pipelines_overlap_and_keep_failures_independent(monkeypatch, tmp_path, caplog):
    caplog.set_level("INFO")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    import threading
    from papertrack import cli

    barrier = threading.Barrier(2, timeout=3)
    seen = []
    def journal(task, zotero):
        barrier.wait()
        seen.append('journal')
        raise RuntimeError('journal blocked')
    def arxiv(task, zotero):
        barrier.wait()
        seen.append('arxiv')
    monkeypatch.setattr(cli, '_run_journal_refresh', journal)
    monkeypatch.setattr(cli, '_run_arxiv_backfill', arxiv)
    monkeypatch.setattr(cli, '_build_zotero', lambda: None)
    args = cli._build_parser().parse_args([
        '--diagnostics-dir', str(tmp_path/'logs'), 'refresh',
        '--data-dir', str(tmp_path), '--journals', 'jcim', '--category', 'chem-ph'])
    with pytest.raises(RuntimeError, match='Refresh failed for: jcim'):
        cli._run_refresh(args, None)
    assert sorted(seen) == ['arxiv', 'journal']
    assert 'journals pipeline finished' in (tmp_path/'logs/journals.log').read_text()
    assert 'arxiv pipeline finished' in (tmp_path/'logs/arxiv.log').read_text()

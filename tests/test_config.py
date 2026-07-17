from pathlib import Path

from papertrack.codex import get_query_args
from papertrack.arxiv_index_fetch import _advanced_search_url


def test_arxiv_config_includes_computer_science_categories():
    query_args = get_query_args()

    assert query_args["cs.AI"]["classification-computer_science"] == "y"
    assert query_args["cs.AI"]["terms-0-term"] == "cs.AI"
    assert query_args["cs.AI"]["terms-0-field"] == "cross_list_category"
    assert "terms-1-term" not in query_args["cs.AI"]
    assert query_args["cs.LG"]["classification-computer_science"] == "y"
    assert query_args["cs.LG"]["terms-0-term"] == "cs.LG"
    assert query_args["cs.LG"]["terms-0-field"] == "cross_list_category"
    assert "terms-1-term" not in query_args["cs.LG"]
    assert query_args["cs.AI"]["date-date_type"] == "submitted_date_first"
    assert query_args["cs.LG"]["date-date_type"] == "submitted_date_first"


def test_computer_science_arxiv_urls_use_archive_filters():
    query_args = get_query_args()

    for category in ("cs.AI", "cs.LG"):
        args = dict(query_args[category])
        args["date-from_date"] = "2026-06-01"
        args["date-to_date"] = "2026-06-02"

        first_page = _advanced_search_url(args, 0)
        second_page = _advanced_search_url(args, 200)

        assert f"terms-0-term={category}" in first_page
        assert "terms-0-field=cross_list_category" in first_page
        assert f"terms-1-term={category}" not in first_page
        assert "classification-computer_science_archives" not in first_page
        assert f"terms-0-term={category}" in second_page
        assert "start=200" in second_page


def test_run_sh_runs_arxiv_sources_only_during_journal_pause():
    run_sh = Path(__file__).resolve().parents[1] / "run.sh"
    content = run_sh.read_text(encoding="utf-8")
    active_lines = "\n".join(
        line for line in content.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )

    assert 'ARXIV_CATEGORIES="chem-ph,quant-ph,cs.AI,cs.LG"' in content
    assert "ARXIV_BACKFILL_FROM=2026-01-01" in content
    assert "ARXIV_REQUEST_DELAY_SECONDS=10" in content
    assert "ARXIV_RATE_LIMIT_BACKOFF=30,60,120" in content
    assert 'PAPERTRACK_ARXIV_REQUEST_DELAY="$ARXIV_REQUEST_DELAY_SECONDS"' in content
    assert 'PAPERTRACK_ARXIV_RATE_LIMIT_BACKOFF="$ARXIV_RATE_LIMIT_BACKOFF"' in content
    assert "export PAPERTRACK_CROSSREF_MODE=metadata" in content
    assert 'run --no-capture-output -n "$CONDA_ENV" python main.py' in content
    assert "# JOURNALS=(jctc jcim jcp prl)" in content
    assert 'arxiv_category_has_data "$category"' in content
    assert '--category "$category"' in content
    assert '--refresh-days "$ARXIV_REFRESH_DAYS"' in content
    assert '--from-date "$ARXIV_BACKFILL_FROM"' in content
    assert '--to-date "$TODAY"' in content
    assert '--diagnostics-dir "$DIAGNOSTICS_DIR"' in content
    assert "run_papertrack journal" not in active_lines

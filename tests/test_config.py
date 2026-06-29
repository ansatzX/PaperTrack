from pathlib import Path

from papertrack.codex import get_query_args


def test_arxiv_config_includes_computer_science_categories():
    query_args = get_query_args()

    assert query_args["cs.AI"]["classification-computer_science"] == "y"
    assert query_args["cs.AI"]["terms-1-term"] == "cs.AI"
    assert query_args["cs.AI"]["terms-1-field"] == "all"
    assert query_args["cs.LG"]["classification-computer_science"] == "y"
    assert query_args["cs.LG"]["terms-1-term"] == "cs.LG"
    assert query_args["cs.LG"]["terms-1-field"] == "all"
    assert query_args["cs.AI"]["date-date_type"] == "submitted_date_first"
    assert query_args["cs.LG"]["date-date_type"] == "submitted_date_first"


def test_run_sh_refreshes_new_arxiv_and_journal_sources():
    run_sh = Path(__file__).resolve().parents[1] / "run.sh"
    content = run_sh.read_text(encoding="utf-8")

    assert "--category chem-ph,quant-ph,cs.AI,cs.LG" in content
    assert "--journal jcim" in content
    assert "--journal prl" in content

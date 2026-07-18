import stat
import subprocess
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


ROOT = Path(__file__).resolve().parents[1]


def _active_shell_lines(path: Path) -> str:
    content = path.read_text(encoding="utf-8").replace("\\\n", "")
    return "\n".join(
        line for line in content.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


def test_run_sh_runs_arxiv_sources_only():
    run_sh = Path(__file__).resolve().parents[1] / "run.sh"
    content = run_sh.read_text(encoding="utf-8")
    active_lines = _active_shell_lines(run_sh)

    assert 'ARXIV_CATEGORIES="chem-ph,quant-ph,cs.AI,cs.LG"' in content
    assert "ARXIV_BACKFILL_FROM=2026-01-01" in content
    assert "ARXIV_REQUEST_DELAY_SECONDS=10" in content
    assert "ARXIV_RATE_LIMIT_BACKOFF=30,60,120" in content
    assert 'PAPERTRACK_ARXIV_REQUEST_DELAY="$ARXIV_REQUEST_DELAY_SECONDS"' in content
    assert 'PAPERTRACK_ARXIV_RATE_LIMIT_BACKOFF="$ARXIV_RATE_LIMIT_BACKOFF"' in content
    assert "PAPERTRACK_CROSSREF_MODE" not in content
    assert 'run --no-capture-output -n "$CONDA_ENV" python main.py' in content
    assert 'arxiv_category_has_data "$category"' in content
    assert '--category "$category"' in content
    assert '--refresh-days "$ARXIV_REFRESH_DAYS"' in content
    assert '--from-date "$ARXIV_BACKFILL_FROM"' in content
    assert '--to-date "$TODAY"' in content
    assert '--diagnostics-dir "$DIAGNOSTICS_DIR"' in content
    assert "run_papertrack journal" not in active_lines
    assert "JOURNAL" not in active_lines
    assert run_sh.stat().st_mode & stat.S_IXUSR


def test_run_j_sh_configures_exact_production_request_intervals():
    run_j_sh = ROOT / "run_j.sh"
    content = run_j_sh.read_text(encoding="utf-8")

    assert "set -uo pipefail" in content
    assert "export PAPERTRACK_CROSSREF_MODE=metadata" in content
    assert "export PAPERTRACK_ACS_ISSUE_INTERVAL=30" in content
    assert "export PAPERTRACK_AIP_ISSUE_INTERVAL=30" in content
    assert "export PAPERTRACK_AIP_ABSTRACT_INTERVAL=10" in content
    assert "export PAPERTRACK_APS_ISSUE_INTERVAL=30" in content
    assert "CONDA_BIN=/home/ansatz/soft/miniconda3/bin/conda" in content
    assert "CONDA_ENV=arxiv" in content
    assert "DATA_DIR=/home/ansatz/data/obsidian/1/papertrack_datas" in content
    assert 'DIAGNOSTICS_DIR="${PROJECT_DIR}/.papertrack_diagnostics"' in content


def test_run_j_sh_defines_and_reaps_exactly_three_provider_workers():
    run_j_sh = ROOT / "run_j.sh"
    active_lines = _active_shell_lines(run_j_sh)
    worker_scripts = (
        "scripts/rebuild_acs_candidates.py",
        "scripts/rebuild_aip_candidates.py",
        "scripts/rebuild_aps_candidates.py",
    )

    for script in worker_scripts:
        matching_lines = [
            line for line in active_lines.splitlines() if script in line
        ]
        assert len(matching_lines) == 1

    assert '/usr/bin/setsid /usr/bin/python3 "$SUPERVISOR"' in active_lines
    assert active_lines.count('"${worker_flags[@]}" &') == 1
    assert 'pid="$!"' in active_lines
    assert 'owned_tokens["$pid"]="$token"' in active_lines
    assert 'owned_starttimes["$pid"]=""' in active_lines
    assert 'owned_ready_files["$pid"]="$ready_file"' in active_lines
    assert '--ready-file "$ready_file"' in active_lines
    assert '[[ -s "$ready_file" ]]' in active_lines
    assert 'for script in "${worker_scripts[@]}"; do' in active_lines
    assert 'start_worker "$script" || break' in active_lines
    assert 'while ((${#owned_tokens[@]} > 0)); do' in active_lines
    assert 'wait -n -p completed_pid "${active_pids[@]}"' in active_lines
    assert 'remove_owned_supervisor "$completed_pid"' in active_lines
    assert "child_status != 0 && shutdown_status == 0 && final_status == 0" in active_lines
    assert 'kill -s "$signal" -- "-$pid"' in active_lines
    assert 'verify-owner "$pid" "$token" "$starttime"' in active_lines
    assert '"$SUPERVISOR" signal-unready' in active_lines
    assert '"$pid" "${owned_tokens[$pid]}" "$$" "$signal_to_send"' in active_lines
    assert 'kill -s "$signal_to_send" -- "$pid"' not in active_lines
    assert 'if [[ -n "$shutdown_signal" ]]; then' in active_lines
    assert "set -e" not in active_lines
    assert "trap 'forward_signal TERM' TERM" in active_lines
    assert "trap 'forward_signal INT' INT" in active_lines
    assert run_j_sh.stat().st_mode & stat.S_IXUSR


def test_run_j_sh_allows_only_shared_recovery_flags():
    active_lines = _active_shell_lines(ROOT / "run_j.sh")

    assert "--retry-unresolved|--reset-progress|--dry-run)" in active_lines
    assert 'worker_flags+=("$argument")' in active_lines
    assert 'echo "unknown argument: $argument" >&2' in active_lines


def test_run_j_sh_rejects_unknown_arguments_before_starting_workers():
    completed = subprocess.run(
        ["bash", str(ROOT / "run_j.sh"), "--not-a-worker-option"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 2
    assert "unknown argument: --not-a-worker-option" in completed.stderr


def test_readme_documents_journal_wrapper_platform_dependencies():
    content = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "Linux `/proc`" in content
    assert "`prctl`" in content
    assert "child-subreaper support" in content
    assert "`/usr/bin/setsid`" in content
    assert "Bash 5.1+" in content
    assert "`wait -n -p`" in content
    assert "`os.pidfd_open`" in content
    assert "`signal.pidfd_send_signal`" in content

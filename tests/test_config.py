import stat
import subprocess
from pathlib import Path

from papertrack import codex
from papertrack.codex import get_query_args, official_arxiv_category
from papertrack.arxiv_index_fetch import _advanced_search_url


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


def test_ledger_category_maps_to_official_arxiv_subject():
    assert official_arxiv_category("chem-ph") == "physics.chem-ph"
    assert official_arxiv_category("cs.AI") == "cs.AI"
    assert official_arxiv_category("quant-ph") == "quant-ph"


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
    assert "export PAPERTRACK_CROSSREF_MODE=disabled" in content
    assert "PAPERTRACK_CROSSREF_MODE=metadata" not in content
    assert "export PAPERTRACK_ACS_ISSUE_INTERVAL=30" in content
    assert "export PAPERTRACK_AIP_ISSUE_INTERVAL=30" in content
    assert "export PAPERTRACK_AIP_ABSTRACT_INTERVAL=10" in content
    assert "export PAPERTRACK_APS_ISSUE_INTERVAL=30" in content
    assert "CONDA_BIN=/home/ansatz/soft/miniconda3/bin/conda" in content
    assert "CONDA_ENV=arxiv" in content
    assert "DATA_DIR=/home/ansatz/data/obsidian/1/papertrack_datas" in content
    assert 'DIAGNOSTICS_DIR="${PROJECT_DIR}/.papertrack_diagnostics"' in content
    assert 'PROFILE_ROOT="${PROJECT_DIR}/.papertrack_browser_profiles"' in content
    assert 'SNAPSHOT_ROOT="${PROJECT_DIR}/.papertrack_official_cache"' in content
    assert "CHROME_BIN=/usr/bin/google-chrome-stable" in content
    assert 'command -v xvfb-run' in content


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
    assert 'owned_ack_files["$pid"]="$ack_file"' in active_lines
    assert '--ready-file "$ready_file"' in active_lines
    assert '--ack-file "$ack_file" --parent-pid "$$" --' in active_lines
    assert '[[ -s "$ready_file" ]]' in active_lines
    assert ': > "$ack_file"' in active_lines
    assert 'for script in "${worker_scripts[@]}"; do' in active_lines
    assert 'start_worker "$script" || break' in active_lines
    assert 'while ((${#owned_tokens[@]} > 0)); do' in active_lines
    assert 'wait -n -p completed_pid "${active_pids[@]}"' in active_lines
    assert 'remove_owned_supervisor "$completed_pid"' in active_lines
    assert "child_status != 0 && shutdown_status == 0 && final_status == 0" in active_lines
    assert 'kill -s "$signal" -- "-$pid"' in active_lines
    assert 'verify-owner "$pid" "$token" "$starttime"' in active_lines
    assert '"$SUPERVISOR" signal-unready' in active_lines
    assert '"$SUPERVISOR" check-pidfd' in active_lines
    assert '"$XVFB_RUN_BIN" -a "$CONDA_BIN" run' in active_lines
    assert '--profile-root "$PROFILE_ROOT"' in active_lines
    assert '--snapshot-root "$SNAPSHOT_ROOT"' in active_lines
    assert '"$pid" "${owned_tokens[$pid]}" "$$" "$signal_to_send"' in active_lines
    assert 'bounded_reap_unready_supervisor "$pid"' in active_lines
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

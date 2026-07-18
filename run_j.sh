#!/usr/bin/env bash
set -uo pipefail

worker_flags=()
for argument in "$@"; do
    case "$argument" in
        --retry-unresolved|--reset-progress|--dry-run)
            worker_flags+=("$argument")
            ;;
        *)
            echo "unknown argument: $argument" >&2
            echo "usage: $0 [--retry-unresolved] [--reset-progress] [--dry-run]" >&2
            exit 2
            ;;
    esac
done

source /home/ansatz/.bashrc

PROJECT_DIR=/home/ansatz/data/code/arxiv_reading
CONDA_BIN=/home/ansatz/soft/miniconda3/bin/conda
CONDA_ENV=arxiv
DATA_DIR=/home/ansatz/data/obsidian/1/papertrack_datas
DIAGNOSTICS_DIR="${PROJECT_DIR}/.papertrack_diagnostics"

export PAPERTRACK_CROSSREF_MODE=metadata
export PAPERTRACK_ACS_ISSUE_INTERVAL=30
export PAPERTRACK_AIP_ISSUE_INTERVAL=30
export PAPERTRACK_AIP_ABSTRACT_INTERVAL=10
export PAPERTRACK_APS_ISSUE_INTERVAL=30

cd "$PROJECT_DIR"

child_pids=()
worker_scripts=(
    scripts/rebuild_acs_candidates.py
    scripts/rebuild_aip_candidates.py
    scripts/rebuild_aps_candidates.py
)
final_status=0
signal_generation=0
shutdown_signal=""
shutdown_status=0
wrapper_pgid="$(ps -o pgid= -p "$$")"
wrapper_pgid="${wrapper_pgid//[[:space:]]/}"

signal_process_group() {
    local signal="$1"
    local pid="$2"
    local attempt pgid

    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    (( pid != wrapper_pgid )) || return 1

    # The parent can run immediately after forking, before setsid(1) has made
    # the child its own group leader. Never signal the wrapper's group; wait
    # briefly for the recorded PID to become the dedicated PGID instead.
    for ((attempt = 0; attempt < 100; attempt++)); do
        pgid="$(ps -o pgid= -p "$pid" 2>/dev/null)" || return 0
        pgid="${pgid//[[:space:]]/}"
        if [[ "$pgid" == "$pid" ]]; then
            kill -s "$signal" -- "-$pid" 2>/dev/null || true
            return 0
        fi
        sleep 0.01
    done

    echo "unable to signal worker group $pid safely" >&2
    return 1
}

forward_signal() {
    local signal="$1"
    local signal_status=1
    local index pid

    if [[ "$signal" == TERM ]]; then
        signal_status=143
    elif [[ "$signal" == INT ]]; then
        signal_status=130
    fi
    if [[ -z "$shutdown_signal" ]]; then
        shutdown_signal="$signal"
        shutdown_status="$signal_status"
        final_status="$signal_status"
    fi
    ((signal_generation += 1))

    for index in "${!child_pids[@]}"; do
        pid="${child_pids[$index]}"
        signal_process_group "$signal" "$pid" || true
    done
}

start_worker() {
    local script="$1"
    local pid

    [[ -z "$shutdown_signal" ]] || return 1

    /usr/bin/setsid "$CONDA_BIN" run --no-capture-output -n "$CONDA_ENV" \
        python "$script" \
        --data-dir "$DATA_DIR" --diagnostics-dir "$DIAGNOSTICS_DIR" \
        "${worker_flags[@]}" &
    pid="$!"
    child_pids+=("$pid")

    # A trap can run after the pre-launch check but before this PID is stored.
    # Persisted shutdown state closes that window for the newly created group.
    if [[ -n "$shutdown_signal" ]]; then
        signal_process_group "$shutdown_signal" "$pid" || true
        return 1
    fi
}

trap 'forward_signal TERM' TERM
trap 'forward_signal INT' INT

for script in "${worker_scripts[@]}"; do
    start_worker "$script" || break
done

for index in "${!child_pids[@]}"; do
    pid="${child_pids[$index]}"
    while true; do
        observed_signal_generation="$signal_generation"
        wait "$pid"
        child_status=$?
        if (( observed_signal_generation != signal_generation )); then
            continue
        fi
        break
    done
    unset 'child_pids[index]'
    if (( child_status != 0 && shutdown_status == 0 && final_status == 0 )); then
        final_status=1
    fi
done

exit "$final_status"

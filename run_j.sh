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
final_status=0
signal_generation=0

forward_signal() {
    local signal="$1"
    local signal_status=1
    local index pid

    if [[ "$signal" == TERM ]]; then
        signal_status=143
    elif [[ "$signal" == INT ]]; then
        signal_status=130
    fi
    final_status="$signal_status"
    ((signal_generation += 1))

    for index in "${!child_pids[@]}"; do
        pid="${child_pids[$index]}"
        if kill -0 "$pid" 2>/dev/null; then
            kill -s "$signal" -- "$pid" 2>/dev/null || true
        fi
    done
}

trap 'forward_signal TERM' TERM
trap 'forward_signal INT' INT

"$CONDA_BIN" run --no-capture-output -n "$CONDA_ENV" \
    python scripts/rebuild_acs_candidates.py \
    --data-dir "$DATA_DIR" --diagnostics-dir "$DIAGNOSTICS_DIR" \
    "${worker_flags[@]}" &
child_pids+=("$!")

"$CONDA_BIN" run --no-capture-output -n "$CONDA_ENV" \
    python scripts/rebuild_aip_candidates.py \
    --data-dir "$DATA_DIR" --diagnostics-dir "$DIAGNOSTICS_DIR" \
    "${worker_flags[@]}" &
child_pids+=("$!")

"$CONDA_BIN" run --no-capture-output -n "$CONDA_ENV" \
    python scripts/rebuild_aps_candidates.py \
    --data-dir "$DATA_DIR" --diagnostics-dir "$DIAGNOSTICS_DIR" \
    "${worker_flags[@]}" &
child_pids+=("$!")

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
    if (( child_status != 0 && final_status == 0 )); then
        final_status=1
    fi
done

exit "$final_status"

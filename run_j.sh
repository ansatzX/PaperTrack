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
SUPERVISOR="${PROJECT_DIR}/scripts/run_j_worker_supervisor.py"

export PAPERTRACK_CROSSREF_MODE=metadata
export PAPERTRACK_ACS_ISSUE_INTERVAL=30
export PAPERTRACK_AIP_ISSUE_INTERVAL=30
export PAPERTRACK_AIP_ABSTRACT_INTERVAL=10
export PAPERTRACK_APS_ISSUE_INTERVAL=30

cd "$PROJECT_DIR"

ownership_dir="$(mktemp -d "${TMPDIR:-/tmp}/papertrack-run-j.XXXXXX")" || {
    echo "unable to create journal worker ownership directory" >&2
    exit 1
}
cleanup_ownership_dir() {
    rm -rf -- "$ownership_dir"
}
trap cleanup_ownership_dir EXIT

worker_scripts=(
    scripts/rebuild_acs_candidates.py
    scripts/rebuild_aip_candidates.py
    scripts/rebuild_aps_candidates.py
)
declare -A owned_tokens=()
declare -A owned_starttimes=()
declare -A owned_scripts=()
declare -A owned_ready_files=()
final_status=0
shutdown_signal=""
shutdown_status=0

capture_owned_starttime() {
    local pid="$1"
    local token="$2"
    local ready_file="${owned_ready_files[$pid]:-}"
    local attempt starttime

    for ((attempt = 0; attempt < 100; attempt++)); do
        if [[ -s "$ready_file" ]]; then
            starttime="$(<"$ready_file")"
            if /usr/bin/python3 "$SUPERVISOR" \
                verify-owner "$pid" "$token" "$starttime"; then
                owned_starttimes["$pid"]="$starttime"
                return 0
            fi
            [[ -e "/proc/$pid" ]] || return 2
        fi
        [[ -e "/proc/$pid" ]] || return 1
        sleep 0.01
    done
    return 1
}

reap_ready_completion() {
    local pid="$1"
    local child_status

    wait "$pid"
    child_status=$?
    remove_owned_supervisor "$pid"
    if (( child_status != 0 && shutdown_status == 0 && final_status == 0 )); then
        final_status=1
    fi
}

signal_owned_group() {
    local signal="$1"
    local pid="$2"
    local token="${owned_tokens[$pid]:-}"
    local starttime="${owned_starttimes[$pid]:-}"

    [[ -n "$token" ]] || return 0
    if [[ -z "$starttime" ]]; then
        capture_owned_starttime "$pid" "$token" || return 0
        starttime="${owned_starttimes[$pid]}"
    fi
    if /usr/bin/python3 "$SUPERVISOR" \
        verify-owner "$pid" "$token" "$starttime"; then
        kill -s "$signal" -- "-$pid" 2>/dev/null || true
        return 0
    fi
    echo "refusing to signal unowned worker group $pid" >&2
    return 0
}

remove_owned_supervisor() {
    local pid="$1"

    unset 'owned_tokens[$pid]'
    unset 'owned_starttimes[$pid]'
    unset 'owned_scripts[$pid]'
    unset 'owned_ready_files[$pid]'
}

fail_unready_launch() {
    local pid="$1"
    local signal_to_send="${shutdown_signal:-TERM}"

    /usr/bin/python3 "$SUPERVISOR" signal-unready \
        "$pid" "${owned_tokens[$pid]}" "$$" "$signal_to_send" || true
    wait "$pid" 2>/dev/null || true
    remove_owned_supervisor "$pid"
    if (( shutdown_status == 0 )); then
        final_status=1
    fi
}

forward_signal() {
    local signal="$1"
    local signal_status=1
    local pid

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
    for pid in "${!owned_tokens[@]}"; do
        signal_owned_group "$signal" "$pid"
    done
}

start_worker() {
    local script="$1"
    local pid provider readiness_status ready_file token

    [[ -z "$shutdown_signal" ]] || return 1

    provider="${script#scripts/rebuild_}"
    provider="${provider%_candidates.py}"
    token="papertrack-${provider}-$$-${RANDOM}-${#owned_tokens[@]}"
    ready_file="${ownership_dir}/${provider}.ready"
    /usr/bin/setsid /usr/bin/python3 "$SUPERVISOR" \
        supervise --ownership-token "$token" --ready-file "$ready_file" -- \
        "$CONDA_BIN" run --no-capture-output -n "$CONDA_ENV" \
            python "$script" \
            --data-dir "$DATA_DIR" --diagnostics-dir "$DIAGNOSTICS_DIR" \
            "${worker_flags[@]}" &
    pid="$!"
    owned_tokens["$pid"]="$token"
    owned_starttimes["$pid"]=""
    owned_scripts["$pid"]="$script"
    owned_ready_files["$pid"]="$ready_file"
    capture_owned_starttime "$pid" "$token"
    readiness_status=$?
    if (( readiness_status == 2 )); then
        reap_ready_completion "$pid"
        return 0
    fi
    if (( readiness_status != 0 )); then
        echo "journal worker failed readiness: $script" >&2
        fail_unready_launch "$pid"
        return 1
    fi

    # A trap can run after the pre-launch check but before this PID is stored.
    # Persisted shutdown state closes that window for the newly created group.
    if [[ -n "$shutdown_signal" ]]; then
        signal_owned_group "$shutdown_signal" "$pid"
        return 1
    fi
}

trap 'forward_signal TERM' TERM
trap 'forward_signal INT' INT

for script in "${worker_scripts[@]}"; do
    start_worker "$script" || break
done

while ((${#owned_tokens[@]} > 0)); do
    active_pids=("${!owned_tokens[@]}")
    completed_pid=""
    wait -n -p completed_pid "${active_pids[@]}"
    child_status=$?
    if [[ -z "${completed_pid:-}" ]]; then
        continue
    fi
    remove_owned_supervisor "$completed_pid"
    if (( child_status != 0 && shutdown_status == 0 && final_status == 0 )); then
        final_status=1
    fi
done

exit "$final_status"

#!/usr/bin/env bash
set -euo pipefail
cd /home/ansatz/data/code/arxiv_reading

mkdir -p .work/tmp .papertrack_diagnostics
exec 9>.work/run.lock
flock -n 9 || { echo 'PaperTrack is already running' >&2; exit 1; }
export TMPDIR="$PWD/.work/tmp"
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
export PAPERTRACK_ARXIV_REQUEST_DELAY=10

systemd-run --user --scope --quiet --collect \
    --property=MemoryHigh=6G --property=MemoryMax=8G --property=MemorySwapMax=0 \
    --property=CPUQuota=200% --property=TasksMax=256 --property=RuntimeMaxSec=24h -- \
    /home/ansatz/soft/miniconda3/bin/conda run --no-capture-output -n arxiv bash <<'PIPELINES'
set -euo pipefail
data=/home/ansatz/data/obsidian/1/papertrack_datas

run_arxiv() {
    for category in chem-ph quant-ph cs.AI cs.LG; do
        if [[ -d "$data/arxiv/$category" ]] &&
           [[ -n "$(find "$data/arxiv/$category" -type f -name '*.md' -print -quit)" ]]; then
            python main.py --diagnostics-dir .papertrack_diagnostics arxiv refresh \
                --category "$category" --refresh-days 30 --data-dir "$data" || return $?
        else
            python main.py --diagnostics-dir .papertrack_diagnostics arxiv backfill \
                --category "$category" --from-date 2026-01-01 --to-date "$(date +%F)" \
                --data-dir "$data" || return $?
        fi
    done
}

run_arxiv >> .papertrack_diagnostics/arxiv.log 2>&1 &
arxiv_pid=$!
python main.py --diagnostics-dir .papertrack_diagnostics refresh \
    --journals prl,jctc,jcim,jacs,jcp --category '' \
    --refresh-days 30 --from-date 2026-01-01 --data-dir "$data" &
journal_pid=$!

status=0
wait "$arxiv_pid" || status=1
wait "$journal_pid" || status=1
exit "$status"
PIPELINES

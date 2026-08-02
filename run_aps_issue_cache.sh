#!/usr/bin/env bash
# Cache APS formal issue snapshots before any recent-page abstract completion.
set -euo pipefail

if (( $# > 1 )); then
    echo "usage: $0 [VOLUME/ISSUE]" >&2
    exit 2
fi

PROJECT_DIR=/home/ansatz/data/code/arxiv_reading
CONDA_BIN=/home/ansatz/soft/miniconda3/bin/conda
DATA_DIR=/home/ansatz/data/obsidian/1/papertrack_datas
PROFILE_ROOT="${PROJECT_DIR}/.papertrack_browser_profiles"
SNAPSHOT_ROOT="${PROJECT_DIR}/.papertrack_official_cache"
DIAGNOSTICS_DIR="${PROJECT_DIR}/.papertrack_diagnostics"
CHROME_PROFILE=/home/ansatz/.config/google-chrome-papertrack
FROM_ISSUE="${1:-120/1}"

cd "$PROJECT_DIR"
exec "$CONDA_BIN" run --no-capture-output -n arxiv \
    python scripts/repair_all_aps_reports.py \
    --data-dir "$DATA_DIR" \
    --diagnostics-dir "$DIAGNOSTICS_DIR" \
    --profile-root "$PROFILE_ROOT" \
    --snapshot-root "$SNAPSHOT_ROOT" \
    --browser-user-data-dir "$CHROME_PROFILE" \
    --launch-browser \
    --from-issue "$FROM_ISSUE" \
    --phase issue \
    --issue-interval 120

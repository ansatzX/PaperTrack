#!/usr/bin/env bash
set -euo pipefail

source /home/ansatz/.bashrc

PROJECT_DIR=/home/ansatz/data/code/arxiv_reading
CONDA_BIN=/home/ansatz/soft/miniconda3/bin/conda
CONDA_ENV=arxiv
DATA_DIR=/home/ansatz/data/obsidian/1/papertrack_datas
DIAGNOSTICS_DIR="${PROJECT_DIR}/.papertrack_diagnostics"

ARXIV_CATEGORIES="chem-ph,quant-ph,cs.AI,cs.LG"
ARXIV_REFRESH_DAYS=30
ARXIV_BACKFILL_FROM=2026-01-01
ARXIV_REQUEST_DELAY_SECONDS=10
ARXIV_RATE_LIMIT_BACKOFF=30,60,120
# Formal journal issue fetching is intentionally paused while the arXiv ledger
# is stabilized. Keep the previous configuration here so it can be restored
# without rediscovering the production command shape.
# JOURNAL_FROM_YEAR=2018
# JOURNALS=(jctc jcim jcp prl)

cd "$PROJECT_DIR"

export PAPERTRACK_ARXIV_REQUEST_DELAY="$ARXIV_REQUEST_DELAY_SECONDS"
export PAPERTRACK_ARXIV_RATE_LIMIT_BACKOFF="$ARXIV_RATE_LIMIT_BACKOFF"
# This stable scheduled wrapper may complete missing abstracts from CrossRef.
# Direct development commands leave this unset and therefore use official
# publisher sources only.
export PAPERTRACK_CROSSREF_MODE=metadata

run_papertrack() {
    "$CONDA_BIN" run --no-capture-output -n "$CONDA_ENV" python main.py \
        --diagnostics-dir "$DIAGNOSTICS_DIR" \
        "$@"
}

arxiv_category_has_data() {
    local category_dir="${DATA_DIR}/arxiv/$1"

    [[ -d "$category_dir" ]] || return 1
    [[ -n "$(find "$category_dir" -type f -name '*.md' -print -quit)" ]]
}

TODAY="$(date +%F)"
IFS=',' read -r -a ARXIV_CATEGORY_LIST <<< "$ARXIV_CATEGORIES"

# Existing arXiv ledgers use a rolling calendar refresh. New empty ledgers are
# rebuilt from 2026-01-01 so a new source does not start with only recent data.
for category in "${ARXIV_CATEGORY_LIST[@]}"; do
    if arxiv_category_has_data "$category"; then
        run_papertrack arxiv refresh \
            --category "$category" \
            --refresh-days "$ARXIV_REFRESH_DAYS" \
            --data-dir "$DATA_DIR"
    else
        run_papertrack arxiv backfill \
            --category "$category" \
            --from-date "$ARXIV_BACKFILL_FROM" \
            --to-date "$TODAY" \
            --data-dir "$DATA_DIR"
    fi
done

# Issue ledger refresh is paused.
# for journal in "${JOURNALS[@]}"; do
#     run_papertrack journal backfill \
#         --journal "$journal" \
#         --from-year "$JOURNAL_FROM_YEAR" \
#         --data-dir "$DATA_DIR"
# done

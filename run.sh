#!/bin/bash
source /home/ansatz/.bashrc

export my_conda_bin=/home/ansatz/soft/miniconda3/bin/conda
cd /home/ansatz/data/code/arxiv_reading

# arXiv daily submissions
$my_conda_bin run -n arxiv python main.py \
    --category chem-ph,quant-ph \
    --data_dir /home/ansatz/data/obsidian/1/papertrack_datas \
    --time 2026.04,2026.03

# JCTC — backfill oldest-first, then auto new issues
$my_conda_bin run -n arxiv python main.py \
    --source journal \
    --journal jctc \
    --backfill --from_year 2018 \
    --data_dir /home/ansatz/data/obsidian/1/papertrack_datas

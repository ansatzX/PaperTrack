#!/bin/bash
source /home/ansatz/.bashrc

export my_conda_bin=/home/ansatz/soft/miniconda3/bin/conda
cd /home/ansatz/data/code/arxiv_reading

# arXiv rolling refresh
$my_conda_bin run -n arxiv python main.py arxiv refresh \
    --category chem-ph,quant-ph \
    --data-dir /home/ansatz/data/obsidian/1/papertrack_datas

# JCTC — discover from 2018 and process unprocessed issues oldest-first
$my_conda_bin run -n arxiv python main.py journal backfill \
    --journal jctc \
    --from-year 2018 \
    --data-dir /home/ansatz/data/obsidian/1/papertrack_datas

# JCP — discover from 2018 and process unprocessed issues oldest-first
$my_conda_bin run -n arxiv python main.py journal backfill \
    --journal jcp \
    --from-year 2018 \
    --data-dir /home/ansatz/data/obsidian/1/papertrack_datas

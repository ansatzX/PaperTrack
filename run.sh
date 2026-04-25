#!/bin/bash
source /home/ansatz/.bashrc

export my_conda_bin=/home/ansatz/soft/miniconda3/bin/conda
cd /home/ansatz/data/code/arxiv_reading

$my_conda_bin run -n arxiv python arxiv_update.py \
    --category chem-ph,quant-ph \
    --arxiv_folder /home/ansatz/data/obsidian/1/arxiv_datas \
    --time 2026.04,2026.03

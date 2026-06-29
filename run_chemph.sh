#!/bin/bash
source ~/.bashrc

conda run -n arxiv python main.py arxiv backfill \
    --category chem-ph \
    --month 2025-04 \
    --data-dir /home/ansatz/data/obsidian/1/papertrack_datas

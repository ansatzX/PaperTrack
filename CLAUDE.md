# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

PaperTrack is a literature workflow manager that queries arXiv daily submissions and journal issues, checks against Zotero reference library, and generates Markdown reports for Obsidian. It handles metadata synchronization and detects new/updated articles in historical submissions.

## Running the Tool

Main entry point is `main.py`:

```bash
# arXiv mode - refresh recent submitted-date buckets
python main.py arxiv refresh --category chem-ph,quant-ph --data-dir /path/to/output

# Journal mode - process the latest formal issue
python main.py journal latest --journal jctc --data-dir /path/to/output

# Journal mode - backfill historical formal issues
python main.py journal backfill --journal jctc --data-dir /path/to/output

# Journal mode - explicit volume/issue
python main.py journal issue --journal jctc --volume 22 --issue 8 --data-dir /path/to/output
```

Using conda (recommended):
```bash
conda run -n arxiv python main.py arxiv backfill --category chem-ph --data-dir /path/to/folder --month 2025-04
```

Scheduled execution via crontab:
```bash
30 7 * * * bash /home/ansatz/data/code/arxiv_reading/run.sh
```

## Installation

```bash
pip install .  # Install as package
# or
pip install -r requirements.txt
```

## Zotero Setup

Enable Zotero local API: Settings > Advanced > Miscellaneous > "Allow other applications on this computer to communicate with Zotero"

## Architecture

### Core Modules (`src/papertrack/`)

- `arxiv_index_fetch.py` - Queries arXiv advanced search API via feedparser, parses HTML with BeautifulSoup, extracts arxiv_id, title, authors, abstract, external DOI
- `zotero_query.py` - Interface to Zotero via pyzotero library, supports local connections with optional caching
- `report.py` - Generates Markdown reports with collected/not collected sections, compares against existing reports to detect updates
- `codex.py` - Defines arXiv category query configurations (quantum physics, hep, chem-ph categories)

### Data Flow

1. `main.py` entry point parses CLI subcommands and args (source command, date/volume/issue, category/journal, data-dir)
2. For each day in month, calls `query_arxiv_dict()` to fetch arXiv entries
3. `zotero_query` checks each entry against Zotero library by DOI (both arXiv DOI and external published DOI)
4. `_gen_oneday_markdown()` creates report with "collected" (in Zotero) and "not collected" sections
5. If old report exists, compares to find new/updated entries and adds "update" section
6. Markdown saved to `{data_dir}/{category}/{year}/{month:02}/{day:02}.md` (arXiv) or `{data_dir}/{journal_slug}/{volume}/{issue}.md` (journal)

### Category Configuration in `codex.py`

Categories are defined as dictionaries mapping arXiv search parameters. The `chem-ph` category uses special cross-category logic with `terms-1-field='all'` and `terms-1-term='physics.chem-ph'`. Other categories map directly to `classification-physics_archives`.

To add a new category:
1. Create config dict by `deepcopy(quant_ph)`
2. Set `classification-physics_archives` or add custom term filters
3. Add to `query_args` dict

### Report Structure

Generated Markdown includes:
- Frontmatter with tag `#{category}-{date_string}`
- Dataview query for completed tasks
- "collected" section for entries found in Zotero
- "not collected" section for new entries
- "update" section listing newly added entries compared to previous report run

Each entry includes arXiv link (checkbox), title, authors, abstract in quote block.

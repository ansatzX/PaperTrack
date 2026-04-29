# PaperTrack — Literature Workflow Manager

Query arXiv daily submissions and journal issues, match against a Zotero reference library, and generate Markdown reports for Obsidian.

## Quick Start

```bash
# arXiv — daily submissions for current month
python main.py --category chem-ph,quant-ph --data_dir /path/to/output

# Journal — auto-detect and process latest issue
python main.py --source journal --journal jctc --data_dir /path/to/output

# Journal — backfill from a specific year
python main.py --source journal --journal jctc --backfill --from_year 2018 --data_dir /path/to/output

# Journal — explicit volume/issue
python main.py --source journal --journal jctc --volume 22 --issue 8 --data_dir /path/to/output
```

## Installation

```bash
pip install .
# or
conda run -n arxiv pip install .
```

Dependencies: `pyzotero`, `bs4`, `lxml`, `feedparser`, `jinja2`, `cloudscraper`, `requests`.

### Zotero Setup

Enable local API: **Zotero → Settings → Advanced → Miscellaneous → "Allow other applications on this computer to communicate with Zotero"**

### Obsidian Setup

Install the `Dataview` plugin. Generated reports use Dataview queries to track read/completed papers.

## Output Structure

```
papertrack_datas/
├── arxiv/
│   ├── quant-ph/
│   │   └── 2026/04/01.md
│   └── chem-ph/
│       └── 2026/04/01.md
└── acs/
    └── jctc/
        ├── 22/8/report.md
        └── 22/7/report.md
```

## Journal Pipeline

For ACS journals (e.g. JCTC), the tool scrapes the TOC page with `cloudscraper` (Cloudflare bypass) and extracts full metadata including abstracts and TOC graphics.

### Auto-Discovery

When `--volume` and `--issue` are omitted, the tool scrapes the ACS List-of-Issues page to find the latest issue. A state file (`.papertrack_state.json`) tracks processed issues so repeated runs only pick up newly published ones.

### Backfill

`--backfill` processes all historical issues from oldest to newest. Use `--from_year` to limit how far back to go.

## CLI Reference

| Flag | Default | Description |
|------|---------|-------------|
| `--source` | `arxiv` | `arxiv` or `journal` |
| `--data_dir` | — | Output base directory |
| **arXiv mode** | | |
| `--time` | `1949.10` | `YYYY.MM`. `1949.10` = current month |
| `--category` | `quant-ph` | Comma-separated categories |
| `--output_format` | `category/year/month/day` | Directory layout |
| **Journal mode** | | |
| `--journal` | — | Journal key (e.g. `jctc`) |
| `--volume` / `--issue` | — | Explicit issue; omit for auto-detect |
| `--backfill` | `false` | Process all historical issues |
| `--from_year` | `0` | Limit backfill from this year |

## Configuration

### arXiv Categories (`categories.toml`)

```toml
[arxiv.quant-ph]
advanced = ""
terms-0-term = ""
classification-physics_archives = "quant-ph"
# ...
```

### Journals (`categories.toml`)

```toml
[journals.jctc]
name = "Journal of Chemical Theory and Computation"
issn = "1549-9618"
slug = "jctc"
acs_code = "jctcce"
```

To add a journal, add its config to `categories.toml` with `issn`, `slug`, and `acs_code` (for ACS journals).

## crontab

```crontab
30 7 * * * bash /home/ansatz/data/code/arxiv_reading/run.sh
```

## Architecture

```
src/papertrack/
├── arxiv_entry.py       # ArxivEntry dataclass
├── arxiv_index_fetch.py # arXiv advanced search → ArxivEntry dicts
├── journal_entry.py     # JournalEntry dataclass
├── journal_fetch.py     # CrossRef query (fallback)
├── acs_fetch.py         # ACS TOC scraper (cloudscraper + BeautifulSoup)
├── acs_loi.py           # ACS List-of-Issues discovery
├── zotero_query.py      # Zotero interface, DOI/URL index, 3-layer match
├── codex.py             # TOML config loader (categories + journals)
├── report.py            # Report orchestration, Jinja2 rendering, state tracking
├── cli.py               # CLI entry point
├── categories.toml      # arXiv categories + journal configs
└── templates/
    ├── paper.md.j2          # arXiv paper template
    ├── report.md.j2         # arXiv daily report template
    ├── journal_paper.md.j2  # Journal paper template (TOC image, abstract)
    └── journal_report.md.j2 # Journal issue report template
```

### Zotero Matching

Three-layer fallback per paper:

1. **arXiv DOI** — `10.48550/arXiv.XXXX.YYYYY` exact match on Zotero `DOI` field
2. **External DOI** — publisher DOI from arXiv metadata (populated after publication)
3. **arXiv ID from URL** — extracted from Zotero `url` field (webpage-type items without DOI)

Journal articles are matched by direct publisher DOI lookup.

### Per-Day Re-fetch (arXiv)

Each calendar day is queried individually. Re-running a date catches newly cross-listed papers and updated metadata (external DOIs, journal references added after publication).

### Issue State Tracking (Journal)

`.papertrack_state.json` records processed `(volume, issue)` pairs. Once an issue is generated, it won't be re-fetched unless the state file is deleted.

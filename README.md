# arXiv Tools — Literature Workflow Manager

Query arXiv daily submissions, match against a Zotero reference library, and generate Markdown reports for Obsidian.

## How It Works

```
arXiv advanced search  ──→  ArxivEntry (dataclass)  ──→  ZoteroQuery (3-layer match)
                                                              │
                                   ┌──────────────────────────┘
                                   ▼
                              Markdown report (Jinja2)
                                   │
                                   ▼
                              Obsidian vault
```

### Zotero Matching Strategy

Each arXiv paper is checked against the local Zotero library using three fallback layers:

1. **arXiv DOI** — `10.48550/arXiv.XXXX.YYYYY` exact match on Zotero `DOI` field
2. **External DOI** — publisher DOI (e.g. `10.1103/PhysRevLett.134.010201`) extracted from arXiv metadata
3. **arXiv ID from URL** — extract `XXXX.YYYYY` from Zotero `url` field (catches papers saved as `webpage` type without DOI)

Only top-level Zotero items are indexed (via `zot.top()`), excluding attachments and notes.

### Per-Day Re-fetch Design

The tool re-queries each day individually within a month. This is intentional: arXiv updates metadata after publication — external DOIs and journal references are added later. Re-running historical dates catches these updates.

## Installation

```bash
pip install .
```

Dependencies: `pyzotero`, `bs4`, `lxml`, `feedparser`, `jinja2` (all handled by `pyproject.toml`).

### Zotero Setup

Enable local API access: **Zotero → Settings → Advanced → Miscellaneous → "Allow other applications on this computer to communicate with Zotero"**

### Obsidian Setup (Optional)

Install and enable the `Dataview` plugin. Generated reports use Dataview queries to track read/completed papers.

## Configuration

### Categories (`categories.toml`)

Category query parameters are in `categories.toml` at the repo root. Six categories are pre-configured: `quant-ph`, `hep-ex`, `hep-lat`, `hep-ph`, `hep-th`, `chem-ph`.

To add a new category, add a section to `categories.toml`:

```toml
[cs.AI]
advanced = ""
terms-0-term = ""
terms-0-operator = "AND"
terms-0-field = "title"
classification-computer_science = "y"
classification-computer_science_archives = "cs.AI"
classification-include_cross_list = "include"
date-filter_by = "date_range"
date-year = ""
date-from_date = "2025-02-01"
date-to_date = "2025-02-02"
date-date_type = "submitted_date_first"
abstracts = "show"
size = "200"
order = "submitted_date"
```

If `categories.toml` is missing, hardcoded defaults in `codex.py` are used as fallback.

## Usage

### CLI

```bash
# Installed entry point
arxiv-update --category quant-ph --arxiv_folder /path/to/obsidian/arxiv_datas

# Or run directly
python arxiv_update.py --category quant-ph --arxiv_folder /path/to/obsidian/arxiv_datas

# Multiple categories and months
arxiv-update --category chem-ph,quant-ph --time 2026.04,2026.03 --arxiv_folder /path/to/folder

# Custom output format
arxiv-update --category hep-ex --output_format year/month/category/day --arxiv_folder /path/to/folder
```

| Flag | Default | Description |
|------|---------|-------------|
| `--time` | `1949.10` | `YYYY.MM` format. The sentinel `1949.10` means "current date" |
| `--category` | `quant-ph` | Comma-separated arXiv categories |
| `--arxiv_folder` | — | Output base directory |
| `--output_format` | `category/year/month/day` | Directory structure under base |

### crontab

```crontab
30 7 * * * bash /home/ansatz/data/code/arxiv_reading/run.sh
```

### Conda (recommended)

```bash
conda run -n arxiv python arxiv_update.py --category quant-ph --arxiv_folder /path/to/folder
```

## Output

Reports are written as `{arxiv_folder}/{category}/{year}/{month}/{day}.md` (default format). Each report contains:

- **Frontmatter** with `#category-date` tag for Dataview queries
- **collected** section — papers found in Zotero
- **not collected** section — papers not yet in Zotero (with checkboxes)
- **update** section — newly appeared papers since last run (compares against previous report)

## Architecture

```
src/ArXiv_Tools/
├── arxiv_entry.py      # ArxivEntry dataclass
├── arxiv_index_fetch.py # arXiv advanced search → ArxivEntry dicts
├── zotero_query.py     # Zotero interface, DOI/URL index, 3-layer match
├── codex.py            # Category config loader (TOML + fallback)
├── report.py           # Report orchestration, Jinja2 rendering, old-report diffing
├── cli.py              # CLI entry point
└── templates/
    ├── paper.md.j2     # Single paper Markdown template
    └── report.md.j2    # Full day report template
```

### Key data model

```python
@dataclass
class ArxivEntry:
    arxiv_id: str       # "arXiv:2502.07673"
    title: str
    authors: list[str]
    abstract: str
    external_doi: str   # publisher DOI from arXiv metadata
    journal_ref: str    # "Phys. Rev. Lett. 134, 010201 (2025)"
    comments: str       # "18 pages, 6 figures"
    subjects: list[str] # ["quant-ph", "cond-mat.mes-hall"]
```

## Metadata Fields Extracted from arXiv

| HTML source | Field |
|---|---|
| `<p class="list-title">` | `arxiv_id` |
| `<p class="title">` | `title` |
| `<p class="authors">` | `authors` |
| `<span class="abstract-full">` | `abstract` |
| `<span class="tag is-light">` | `external_doi` |
| `<p class="comments">` ("Comments:") | `comments` |
| `<p class="comments">` ("Journal ref:") | `journal_ref` |
| `<span class="tag is-link/is-grey">` | `subjects` |

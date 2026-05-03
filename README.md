# PaperTrack

PaperTrack builds Obsidian-friendly Markdown reports for arXiv daily submissions
and journal issues, then marks papers already present in a local Zotero library.

The current journal workflow is source-conscious:

- ACS journals such as JCTC use ACS issue pages as the primary source.
- AIP journals such as JCP use AIP issue pages as the primary source.
- CrossRef is a fallback only. For JCP, it can fill missing abstracts by DOI, but
  it does not decide which articles belong to an issue.

## Requirements

- Python 3.10+
- Zotero desktop, optional but recommended
- Obsidian with the Dataview plugin, if you want the generated task queries

Install locally:

```bash
pip install -e ".[dev]"
```

The project depends on `curl_cffi` for publisher pages, plus `pyzotero`,
`beautifulsoup4`, `lxml`, `feedparser`, `jinja2`, and `requests`.

## Quick Start

arXiv reports:

```bash
python main.py \
  --category chem-ph,quant-ph \
  --time 2026.04,2026.03 \
  --data_dir /home/ansatz/data/obsidian/1/papertrack_datas
```

Latest journal issue:

```bash
python main.py \
  --source journal \
  --journal jcp \
  --data_dir /home/ansatz/data/obsidian/1/papertrack_datas
```

Backfill journal issues:

```bash
python main.py \
  --source journal \
  --journal jcp \
  --backfill \
  --from_year 2018 \
  --data_dir /home/ansatz/data/obsidian/1/papertrack_datas
```

Explicit issue:

```bash
python main.py \
  --source journal \
  --journal jcp \
  --volume 164 \
  --issue 16 \
  --year 2026 \
  --data_dir /home/ansatz/data/obsidian/1/papertrack_datas
```

`run.sh` uses the local conda environment:

```bash
/home/ansatz/soft/miniconda3/bin/conda run -n arxiv python main.py ...
```

## Output Layout

`--data_dir` is the root. PaperTrack separates sources under that root:

```text
papertrack_datas/
├── arxiv/
│   ├── chem-ph/2026/04/01.md
│   └── quant-ph/2026/04/01.md
├── acs/
│   └── jctc/22/8.md
└── aip/
    └── jcp/164/16.md
```

Journal output is always:

```text
{data_dir}/{provider}/{journal_slug}/{volume}/{issue}.md
```

## Journal Source Policy

### JCTC / ACS

JCTC is configured as an ACS journal:

```toml
[journals.jctc]
name = "Journal of Chemical Theory and Computation"
issn = "1549-9618"
slug = "jctc"
acs_code = "jctcce"
```

Auto-discovery uses ACS list-of-issues pages. Article metadata comes from the
ACS issue TOC page.

### JCP / AIP

JCP is configured as an AIP journal:

```toml
[journals.jcp]
name = "Journal of Chemical Physics"
issn = "0021-9606"
slug = "jcp"
provider = "aip"
```

For each issue, PaperTrack:

1. Fetches the AIP issue page, for example
   `https://pubs.aip.org/aip/jcp/issue/164/16`.
2. Extracts the official issue article list, DOI, title, authors, page, year,
   TOC image, and `data-articleid`.
3. Fetches abstracts from AIP's official issue-page AJAX endpoint:
   `https://pubs.aip.org/PlatformArticle/ArticleAbstractAjax`.
4. If any AIP article still lacks an abstract, fills only that missing
   `abstract` field from CrossRef by matching the DOI.

PaperTrack does not visit each AIP article page. CrossRef is never allowed to
add articles to an AIP issue or replace AIP metadata.

## CLI Reference

| Flag | Default | Description |
| --- | --- | --- |
| `--source` | `arxiv` | `arxiv` or `journal` |
| `--data_dir` | `/home/ansatz/data/obsidian/1/papertrack_datas/` | Output root |
| `--debug` | false | Enable debug logging |
| `--time` | `1949.10` | arXiv month list, `YYYY.MM`; sentinel means current month |
| `--category` | `quant-ph` | Comma-separated arXiv categories |
| `--output_format` | `category/year/month/day` | arXiv directory layout |
| `--journal` | empty | Journal key from `categories.toml`, e.g. `jctc` or `jcp` |
| `--volume` | empty | Explicit journal volume |
| `--issue` | empty | Explicit journal issue |
| `--year` | `0` | Publication year for explicit journal issue mode |
| `--backfill` | false | Process all discovered journal issues |
| `--from_year` | `0` | Start year for backfill/discovery |

## State And Rebuilds

Journal auto mode stores processed `(volume, issue)` pairs in
`.papertrack_state.json` in the current working directory.

On every journal auto run, PaperTrack checks previously processed issue files by
relative path:

```text
{journal_slug}/{volume}/{issue}.md
```

If a processed file is missing or the generated report structure is damaged,
that issue is rebuilt even if it is not the latest issue.

The format check intentionally ignores Obsidian task edits. Marking a paper as
complete, changing task state, or editing the Dataview task block does not count
as corruption. The required skeleton is:

- `## collected`
- `## not collected`
- at least one `### ...` paper entry

arXiv reports are queried one calendar day at a time. Re-running a month
re-fetches each day and rewrites the day file; if an old file is missing or its
generated skeleton is broken, that day is rebuilt without trying to preserve old
IDs.

## Zotero Matching

Zotero is optional. If Zotero is unavailable, all papers are rendered under
`not collected`.

For arXiv entries, matching uses three layers:

1. arXiv DOI, such as `10.48550/arXiv.2502.07673`
2. External publisher DOI from arXiv metadata
3. arXiv ID extracted from a Zotero URL

For journal entries, matching uses the publisher DOI.

Enable Zotero local API:

```text
Zotero -> Settings -> Advanced -> Miscellaneous
-> Allow other applications on this computer to communicate with Zotero
```

## Configuration

Configuration lives in:

```text
src/papertrack/categories.toml
```

Add arXiv categories under `[arxiv.<key>]`.

Add ACS journals with `acs_code`:

```toml
[journals.example_acs]
name = "Example ACS Journal"
issn = "0000-0000"
slug = "example"
acs_code = "abcd"
```

Add AIP-style journals with `provider = "aip"`:

```toml
[journals.example_aip]
name = "Example AIP Journal"
issn = "0000-0000"
slug = "example"
provider = "aip"
```

## Development

Run tests:

```bash
pytest -q
```

Run syntax checks:

```bash
python -m compileall -q src tests
```

Current verification commands used during development:

```bash
/home/ansatz/soft/miniconda3/bin/conda run -n arxiv pytest -q
/home/ansatz/soft/miniconda3/bin/conda run -n arxiv python -m compileall -q src tests
```

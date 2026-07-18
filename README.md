# PaperTrack

PaperTrack builds Obsidian-friendly Markdown reports for arXiv daily submissions
and journal issues, then marks papers already present in a local Zotero library.

The current journal workflow is source-conscious:

- ACS journals such as JCTC use ACS issue pages as the primary source.
- AIP journals such as JCP use AIP issue pages as the primary source.
- APS journals such as PRL use APS issue pages as the primary source.
- CrossRef is disabled by default. The journal-only `run_j.sh` wrapper enables
  metadata mode only to fill missing abstracts by exact DOI; it never decides
  issue membership.

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

The bounded AIP Cloudflare fallback also needs Playwright, Chromium, and Xvfb:

```bash
pip install -e ".[dev,browser]"
playwright install chromium
```

## Quick Start

arXiv reports:

```bash
python main.py arxiv refresh \
  --category chem-ph,quant-ph \
  --data-dir /home/ansatz/data/obsidian/1/papertrack_datas
```

Latest journal issue:

```bash
python main.py journal latest \
  --journal jcp \
  --data-dir /home/ansatz/data/obsidian/1/papertrack_datas
```

In default official-only development mode, AIP `latest`/`backfill` cannot
discover volume/issue pairs without CrossRef. Use the explicit `journal issue`
command shown below. The repository-provided `run_j.sh` is the production
journal rebuild path that enables metadata mode.

Backfill journal issues:

```bash
python main.py journal backfill \
  --journal jcp \
  --from-year 2018 \
  --data-dir /home/ansatz/data/obsidian/1/papertrack_datas
```

Explicit issue:

```bash
python main.py journal issue \
  --journal jcp \
  --volume 164 \
  --issue 16 \
  --year 2026 \
  --data-dir /home/ansatz/data/obsidian/1/papertrack_datas
```

Migrate existing generated reports to the current Markdown schema:

```bash
python main.py migrate \
  --data-dir /home/ansatz/data/obsidian/1/papertrack_datas
```

Collect damaged reports and decide whether to refetch authoritative sources:

```bash
python main.py repair \
  --data-dir /home/ansatz/data/obsidian/1/papertrack_datas
```

Both production wrappers use the local conda environment:

```bash
/home/ansatz/soft/miniconda3/bin/conda run -n arxiv python main.py ...
```

## Scheduled Production Wrappers

arXiv and journal work have separate entry points and should use separate cron
invocations. For example:

```cron
0 1 * * * /home/ansatz/data/code/arxiv_reading/run.sh
0 4 * * 0 /home/ansatz/data/code/arxiv_reading/run_j.sh
```

`run.sh` is arXiv-only. It refreshes the existing `chem-ph`, `quant-ph`,
`cs.AI`, and `cs.LG` ledgers over the rolling 30-day window, or backfills a new
empty category from 2026-01-01. It retains the arXiv 10-second request delay and
30/60/120-second rate-limit backoff.

`run_j.sh` concurrently starts the ACS, AIP, and APS candidate workers. It is a
bounded repair run over exactly 43 audited issues: 2 ACS, 32 AIP, and 9 APS. It
does not run general journal discovery or expand the candidate lists. The
wrapper waits for all three providers even if one fails and returns nonzero if
any provider leaves failed or unresolved work.

With no arguments, a journal run resumes each provider's durable progress and
skips already resolved candidates. The only accepted optional arguments are
passed identically to all three workers:

```bash
./run_j.sh --retry-unresolved  # retry records currently marked unresolved
./run_j.sh --reset-progress    # discard provider progress and assess all candidates
./run_j.sh --dry-run           # inspect the ordered work without publisher requests
```

The flags can be combined. Unknown arguments are rejected rather than forwarded
to the worker commands.

Journal rebuild runtime files live under the ignored, machine-local
`.papertrack_diagnostics/` directory:

```text
journal_rebuild_manifest.json
journal_rebuild_acs_state.json
journal_rebuild_aip_state.json
journal_rebuild_aps_state.json
```

The shared manifest records only the approved candidates, while the provider
state files record resumable outcomes. These JSON files are runtime state, are
ignored by Git, and should not be treated as source configuration.

The wrapper exports request intervals of 30 seconds for ACS issue pages, 30
seconds for AIP issue pages, 10 seconds for AIP abstracts, and 30 seconds for APS
issue pages. These values match the non-bypassable request floors enforced in
code; lowering an environment value cannot weaken those floors.

Official publisher issue pages remain authoritative for issue membership and
publisher metadata. CrossRef metadata mode may complete a missing abstract by
exact DOI, but cannot add or remove articles or replace official title, author,
DOI, volume, issue, page, or date fields. A blocked fetch, parse failure, or
unresolved repair produces a visible nonzero result and preserves the prior
valid Markdown instead of replacing it with empty or partial output.

## Output Layout

`--data-dir` is the root. PaperTrack separates sources under that root:

```text
papertrack_datas/
├── arxiv/
│   ├── chem-ph/2026/04/01.md
│   └── quant-ph/2026/04/01.md
├── acs/
│   └── jctc/22/8.md
├── aip/
│   └── jcp/164/16.md
└── aps/
    └── prl/136/1.md
```

Journal output is always:

```text
{data_dir}/{provider}/{journal_slug}/{volume}/{issue}.md
```

## Journal Source Policy

`PAPERTRACK_CROSSREF_MODE` has two accepted values:

- unset or `disabled`: official publisher pages only; this is the development
  default and makes no CrossRef requests;
- `metadata`: permit CrossRef issue discovery for AIP and exact-DOI abstract
  completion only after an official issue fetch succeeds.

Any other value is an error. CrossRef cannot add or remove issue members, or
replace official DOI, title, author, volume, issue, page, or date metadata. Only
`run_j.sh` exports `PAPERTRACK_CROSSREF_MODE=metadata`; `run.sh` remains
arXiv-only and does not configure CrossRef.

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
   and `data-articleid`.
3. Fetches abstracts from AIP's official issue-page AJAX endpoint:
   `https://pubs.aip.org/aip/jcp/PlatformArticle/ArticleAbstractAjax`.
4. If direct HTTP receives a Cloudflare challenge, makes one bounded headful
   Playwright attempt under Xvfb, reusing one browser context for that issue and
   blocking images, fonts, media, and article-page navigation.
5. In production `metadata` mode only, fills a still-missing `abstract` from
   CrossRef by exact DOI.

PaperTrack does not visit each AIP article page. CrossRef is never allowed to
add articles to an AIP issue or replace AIP metadata.

### PRL / APS

PRL is configured as an APS journal:

```toml
[journals.prl]
name = "Physical Review Letters"
issn = "0031-9007"
slug = "prl"
provider = "aps"
```

Auto-discovery uses APS list-of-issues pages such as
`https://journals.aps.org/prl/issues` and volume pages such as
`https://journals.aps.org/prl/issues/136#v136`. Article membership and issue
metadata come from official issue pages such as
`https://journals.aps.org/prl/issues/136/1`.

Partial APS issues are skipped in `journal latest` and `journal backfill` so the
ledger does not mark an unstable issue as processed. Use `journal issue` with an
explicit volume and issue if you intentionally want to process a partial issue.

## CLI Reference

### arXiv refresh

```bash
python main.py arxiv refresh --category quant-ph --refresh-days 30
```

Refreshes every day from local today minus `--refresh-days` through local today.
Use `--anchor-date YYYY-MM-DD` for reproducible reruns.

### arXiv backfill

```bash
python main.py arxiv backfill --category quant-ph --month 2026-04
python main.py arxiv backfill --category quant-ph --from-date 2026-04-01 --to-date 2026-04-30
```

Backfill is explicit historical processing. It replaces the old `--time`
sentinel behavior.

### journal latest

```bash
python main.py journal latest --journal jcp
```

### journal backfill

```bash
python main.py journal backfill --journal jcp --from-year 2018
```

### journal issue

```bash
python main.py journal issue --journal jcp --volume 164 --issue 16 --year 2026
```

### migrate

```bash
python main.py migrate --data-dir /home/ansatz/data/obsidian/1/papertrack_datas
```

Runs a local-only Markdown schema migration. It removes obsolete generated
content such as old TOC image lines, adds PaperTrack format metadata, preserves
supported Obsidian task edits, and writes through a `.bak` validation protocol.

### repair

```bash
python main.py repair --data-dir /home/ansatz/data/obsidian/1/papertrack_datas
python main.py repair --data-dir /home/ansatz/data/obsidian/1/papertrack_datas --yes
python main.py repair --data-dir /home/ansatz/data/obsidian/1/papertrack_datas --no-fetch
```

Collects corrupt or missing generated reports first, then asks before fetching
authoritative sources. Outcomes are recorded as `fixed`, `unresolved`, or
`deferred` in the diagnostics repair ledger.

## State And Rebuilds

`journal latest` and `journal backfill` store processed `(volume, issue)` pairs in
`.papertrack_state.json` in the current working directory.

On every `journal latest` or `journal backfill` run, PaperTrack checks
previously processed issue files by relative path:

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

arXiv production refresh uses a rolling calendar window. By default it queries
every day from local today minus 30 days through local today. This keeps recent
day buckets converged as arXiv category assignments, external DOIs, and journal
references settle. Historical processing uses explicit `arxiv backfill` months
or date ranges.

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

Add APS-style journals with `provider = "aps"`:

```toml
[journals.example_aps]
name = "Example APS Journal"
issn = "0000-0000"
slug = "example"
provider = "aps"
```

## Development

Run tests:

```bash
pytest -q
```

Direct journal development commands intentionally leave
`PAPERTRACK_CROSSREF_MODE` unset. For AIP, test a known formal issue explicitly:

```bash
python main.py journal issue --journal jcp --volume 164 --issue 16 --year 2026
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

For a deliberately bounded live check of one fixed formal issue per provider:

```bash
/home/ansatz/soft/miniconda3/bin/conda run -n arxiv \
  python scripts/verify_official_journal_issues.py --provider all
```

The verifier is official-only and read-only: it does not import report, Zotero,
CrossRef, or state code. Publisher blocking is reported as a failed observation,
not converted into an empty successful issue.

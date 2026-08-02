# PaperTrack

PaperTrack builds Obsidian-friendly Markdown reports for arXiv daily submissions
and journal issues, then marks papers already present in a local Zotero library.

The current journal workflow is source-conscious:

- ACS journals such as JCTC use ACS issue pages as the primary source.
- AIP journals such as JCP use AIP issue pages as the primary source.
- APS journals such as PRL use APS issue pages as the primary source.
- CrossRef is disabled in the journal-only `run_j.sh` workflow. Formal issue
  membership and metadata come only from official publisher resources.

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

The unattended official runtime also needs Playwright, Google Chrome, and Xvfb:

```bash
pip install -e ".[dev,browser]"
playwright install chrome
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

In official-only mode, AIP `latest`/`backfill` cannot discover volume/issue
pairs without a third-party index. Use the explicit `journal issue` command
shown below. The repository-provided `run_j.sh` is the official-only production
journal rebuild path over an audited list of formal issues.

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

For APS production repair, use the separate issue-cache runner rather than the
bounded candidate worker. It starts (or attaches to) an isolated visible Chrome
profile, uses a 120-second APS request floor, and performs no recent-page or
Markdown work:

```bash
./run_aps_issue_cache.sh          # resume all existing PRL reports from 120/1
./run_aps_issue_cache.sh 127/1    # resume from a later formal issue
```

When this phase has cached every selected formal issue, run the `recent` and
then `render` phases through `repair_all_aps_reports.py`. Never run these APS
phases concurrently: they share one browser profile, one provider floor, and
one circuit breaker.

The journal wrapper is Linux-specific and requires Linux `/proc` and `prctl`
child-subreaper support. It also requires `/usr/bin/setsid`, `/usr/bin/python3`,
and Bash 5.1+ for associative arrays and completion-driven `wait -n -p`. The
deployed Python must expose `os.pidfd_open` and `signal.pidfd_send_signal`.
The wrapper checks those APIs before creating launch state or starting a
provider. Each supervisor also requires a verified startup acknowledgement and
arms a parent-death signal; if the wrapper cannot safely command or acknowledge
it, the supervisor boundedly terminates and reaps its own provider descendants.

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

Journal rebuild runtime files live under ignored, machine-local directories.
Browser profiles persist in `.papertrack_browser_profiles/`, validated official
snapshots persist in `.papertrack_official_cache/`, and progress and failure
evidence live in `.papertrack_diagnostics/`:

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
seconds for AIP issue pages, 10 seconds for AIP abstracts, and 120 seconds for
APS issue pages. These values match the non-bypassable request floors enforced
in code; lowering an environment value cannot weaken those floors.

Official publisher issue pages remain authoritative for issue membership and
publisher metadata. `run_j.sh` exports `PAPERTRACK_CROSSREF_MODE=disabled`; it
does not make CrossRef requests. A blocked fetch, parse failure, or unresolved
repair produces a visible nonzero result and preserves the prior valid Markdown
instead of replacing it with empty or partial output.

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

`PAPERTRACK_CROSSREF_MODE` has two accepted values in the general CLI:

- unset or `disabled`: official publisher pages only; this is the development
  default and makes no CrossRef requests;
- `metadata`: permit CrossRef issue discovery for AIP and exact-DOI abstract
  completion only after an official issue fetch succeeds.

Any other value is an error. CrossRef cannot add or remove issue members, or
replace official DOI, title, author, volume, issue, page, or date metadata.
The production journal wrapper explicitly exports
`PAPERTRACK_CROSSREF_MODE=disabled`; `run.sh` remains arXiv-only and does not
configure CrossRef.

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

1. Navigates the canonical AIP issue page in the publisher worker's single
   persistent Chrome context, for example
   `https://pubs.aip.org/aip/jcp/issue/164/16`.
2. Extracts the official issue article list, DOI, title, authors, page, year,
   and `data-articleid`.
3. Requests required abstracts through the official same-origin issue handler
   in that same browser context:
   `https://pubs.aip.org/Handlers/ArticleAbstractHandler.ashx`.
4. On a challenge, keeps that context and profile, waits once, and reloads the
   canonical issue page according to the bounded provider runtime policy.
5. Validates the complete issue result before committing its official snapshot.

PaperTrack does not visit each AIP article page. The production journal wrapper
does not use CrossRef.

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

#### APS issue membership and recent-page abstract completion

APS has two distinct official views, with deliberately different authority:

1. The repair plan first enumerates the formal reports to repair, creates a
   durable plan, and caches every canonical issue page before any abstract
   completion begins. An issue cache records its DOI set, article count, and
   earliest/latest article dates. A failed request remains retryable; a provider
   circuit skip remains pending because no request was made.
2. The canonical APS issue page is the sole authority for
   the article set and for DOI, title, authors, volume, issue, page, and date.
   Its historical `summary-container` text is an editorial teaser and is never
   treated as an article abstract.
3. Only when all target issue snapshots are present, one shared reverse-
   chronological scan of the journal's official paginated
   `/{journal}/recent?page=N` stream fills empty abstracts by exact normalized
   DOI match. It starts at page 1, so cached pages are reused across issues;
   article counts and dates provide a page-range hint only. A recent page never
   establishes issue membership. Once its oldest date passes an issue's earliest
   date, unmatched DOI values for that issue are recorded as unresolved.
4. A final render phase reads only the cached issue and recent data. It writes
   only issues with complete DOI abstract coverage, preserving the existing
   Obsidian task state and collection grouping. No browser navigation is allowed
   in this phase.

Recent results never create an issue, add or remove issue members, infer an
issue number, or replace issue metadata. For an issue with dated missing
abstracts, the search begins at recent page 1 and advances in published-time
descending order only until all target DOIs are found or the page has reached
the issue's earliest article date. They are fetched in the same persistent APS
browser context as issue pages, share the 30-second provider-wide navigation
floor and circuit breaker, and are cached locally under
`.papertrack_official_cache/aps-recent/`. A valid cached recent page is reused
without network access. If a recent page is blocked, malformed, unavailable, or
does not contain a matching DOI, the official issue result remains intact and
the abstract stays visibly unresolved; PaperTrack does not fall back to visiting
individual article pages.

Run the full repair (or a resumable individual phase) through the single owner:

```bash
python scripts/repair_all_aps_reports.py \
  --data-dir /home/ansatz/data/obsidian/1/papertrack_datas \
  --diagnostics-dir .papertrack_diagnostics \
  --profile-root .papertrack_browser_profiles \
  --snapshot-root .papertrack_official_cache --phase all --issue-interval 120
```

For APS sessions protected by a browser challenge, the repair tool can start a
dedicated visible Chrome profile and attach to it through a loopback-only CDP
port. The profile is deliberately separate from the user's default Chrome
profile, which Chrome does not permit for remote debugging:

```bash
python scripts/repair_all_aps_reports.py \
  --data-dir /home/ansatz/data/obsidian/1/papertrack_datas \
  --diagnostics-dir .papertrack_diagnostics \
  --profile-root .papertrack_browser_profiles \
  --snapshot-root .papertrack_official_cache \
  --browser-user-data-dir /home/ansatz/.config/google-chrome-papertrack \
  --launch-browser --phase issue --candidate 136/23
```

The first run may require a user to complete the publisher's browser challenge
in that visible dedicated Chrome window. Later runs reuse that profile; they
never expose DevTools beyond `127.0.0.1`.

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

For a deliberately bounded live check of exactly one formal issue:

```bash
/home/ansatz/soft/miniconda3/bin/conda run -n arxiv \
  python scripts/verify_official_issue_runtime.py \
  --provider acs --journal jcim --volume 66 --issue 12 \
  --profile-root .papertrack_browser_profiles \
  --snapshot-root .papertrack_official_cache \
  --diagnostics-dir .papertrack_diagnostics
```

The verifier deliberately bypasses a valid cached snapshot for this single
check so it performs one canonical live issue fetch; it never deletes or
damages the prior snapshot, and a blocked or failed live check leaves it
available to production. Production `run_j.sh` remains snapshot-first. The
verifier contacts only the canonical official issue resource (plus AIP's
required same-origin official abstract endpoint). It never uses CrossRef and
does not write Markdown unless `--write-report --data-dir PATH` is explicitly
supplied. It prints one JSON observation, including a concise failure message;
`blocked` is a legitimate visible outcome and is never converted into an empty
successful issue.

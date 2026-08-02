# Repository Guidelines

## Core Design Contract

PaperTrack is not a generic paper crawler. Its whole object is a continuous,
re-runnable reading ledger for arXiv submissions and formal journal issues,
with Zotero collection state and Obsidian task state preserved across runs.

All future changes must protect these invariants:

- Reading units must be stable. arXiv is tracked by submitted-date day buckets;
  journals are tracked by formal volume/issue. Do not mix journal ASAP articles
  into the issue ledger, because they lack stable volume/issue/page placement.
- The report is a reading queue, not a metadata dump. Keep the generated
  Markdown dense and scannable. Abstract, title, authors, DOI/arXiv link, and
  collection status are high-value fields. Decorative or low-signal fields such
  as TOC images should not reduce on-screen paper density.
- Re-running must improve or repair the ledger, not erase user state. Preserve
  completed task checkboxes, existing generated anchors, and user edits wherever
  possible. Missing or corrupt generated skeletons may be rebuilt; ordinary
  Obsidian task edits are not corruption.
- Network failure must not masquerade as empty data. If an authoritative source
  cannot be fetched, do not silently mark an issue/day as successfully processed
  with no articles. Existing valid reports should survive later fetch failures.
- Production fetch failures should be recorded with enough detail for future
  extractor upgrades: source, journal, volume, issue, URL, provider, status or
  exception, and which metadata field failed if parsing partially succeeded.

## Source Policy

### arXiv

Use arXiv advanced search with explicit submitted-date ranges and category
filters. This is intentional:

- the advanced-search result page contains enough metadata for the report in a
  single request;
- category assignments and publication metadata can lag submission time;
- re-querying a fixed day can repair delayed category assignment while keeping
  the reading ledger continuous.

Do not replace the date-bucket workflow with a recent-feed workflow. Recent
feeds answer "what is new now"; this project needs "what belongs to this day
after metadata settles".

### Journals

Journal reports are issue-based. Formal issue pages define the article set.

- ACS journals use ACS list-of-issues and issue TOC pages as the primary source.
- AIP journals use AIP issue pages as the primary source, including the official
  issue-page abstract endpoint when needed.
- APS issue pages exclusively establish formal issue membership. If an APS
  issue page omits abstracts, the official paginated `/{journal}/recent?page=N`
  pages may fill abstracts only by exact DOI match for existing issue members.
  Historical APS `summary-container` text is a teaser rather than an abstract
  and must remain ineligible for report Abstract fields.
  Recent pages must be locally cached and share the provider request floor and
  circuit breaker; they must never add articles, infer issue placement, or
  trigger individual article-page crawling.
  For APS data repair, keep issue and recent access as separate phases:
  first cache the formal issue memberships, DOI sets, article counts, and
  publication-date ranges; only then fetch official recent pages in reverse
  chronological page order. Join the two views solely by exact normalized DOI.
  Issue date ranges and cumulative per-issue article counts may estimate which
  recent pages to prioritize, but are scheduling hints rather than membership
  evidence. A recent page may fill an abstract only for an already cached issue
  DOI. Process newest issues first; stop a given issue only after all its DOI
  targets match, or after the recent stream has passed its earliest publication
  date and remaining DOI targets are explicitly recorded as unresolved. On a
  provider circuit break, stop the phase and leave unvisited work pending;
  never manufacture `circuit_open` results for work that made no request.
  The established production browser path is an isolated persistent Chrome
  profile with loopback-only CDP attachment, launched by
  `run_aps_issue_cache.sh`; do not launch a second browser against that
  profile or use Chrome's default user-data directory. Cache formal issues
  first at the 120-second provider floor, then run recent completion, then
  render. The dedicated profile is durable local runtime state, not source.
- CrossRef is a fallback source. For publisher-backed issue workflows, CrossRef
  may fill missing abstracts by DOI, but it must not add articles to an
  official issue, remove official articles, or replace official title/author
  metadata.

If a journal source has strong anti-scraping behavior, prefer explicit retries,
clear failure visibility, and local preservation of prior valid reports over
weakening the source boundary.

The economic strategy is to avoid bulk crawling. PaperTrack should prefer one
official issue-page visit that yields metadata for many articles over visiting
each article page. When that strategy fails in production, keep detailed failure
records so developers can improve the issue-level extractor or source adapter
later, rather than silently falling back to expensive or boundary-weak crawling.

## State And Migration

Generated Markdown is durable project state because it contains both generated
paper entries and user reading state. Any format change should have a migration
path for old reports when practical.

Migration tools should be local-first: changing old Markdown format should not
require re-fetching publisher pages. A migration may remove obsolete generated
content, such as old TOC image lines, while preserving task completion and paper
anchors.

Journal `latest` and `backfill` modes store processed `(volume, issue)` pairs in
`.papertrack_state.json` in the current working directory. Be careful when
changing this state boundary; moving it changes rebuild and backfill behavior.

## Project Structure

```text
src/papertrack/
├── cli.py                # argparse entry point and source dispatch
├── report.py             # report orchestration, rendering, state tracking
├── arxiv_index_fetch.py  # arXiv advanced search parsing
├── arxiv_entry.py        # ArxivEntry dataclass
├── acs_fetch.py          # ACS issue TOC scraper
├── acs_loi.py            # ACS list-of-issues discovery
├── aip_fetch.py          # AIP issue scraper and abstract endpoint
├── crossref_loi.py       # CrossRef issue discovery fallback
├── journal_fetch.py      # journal source selection and CrossRef fallback
├── journal_entry.py      # JournalEntry dataclass
├── zotero_query.py       # Zotero local API and DOI/arXiv URL matching
├── codex.py              # TOML config loader
├── categories.toml       # arXiv categories and journal definitions
└── templates/            # Jinja2 Markdown templates
tests/                    # pytest suite
main.py                   # logging setup and CLI call
run.sh                    # cron-oriented wrapper
```

## Development Commands

```bash
pip install -e ".[dev]"
pytest
python main.py arxiv refresh --category quant-ph --data-dir /tmp/out
python main.py journal latest --journal jcp --data-dir /tmp/out
```

The deployed local environment is usually:

```bash
/home/ansatz/soft/miniconda3/bin/conda run -n arxiv pytest -q
```

## Coding And Tests

- Target Python 3.10+.
- Follow the existing style: PEP 8, 4-space indentation, snake_case functions,
  PascalCase dataclasses, internal helpers prefixed with `_`.
- Prefer structured dataclasses and parser logic over positional lists or ad hoc
  string handling.
- Add or update tests for parsing, source precedence, Zotero matching, report
  generation, state rebuild behavior, and migration behavior.
- Mock network and Zotero calls with `monkeypatch`; tests should not require
  live publisher sites or a running Zotero instance.
- If adding journal support, update `src/papertrack/categories.toml` and define
  the authoritative source policy for that journal.

## Environment Notes

Zotero is optional at runtime. If Zotero is unavailable, reports should still be
generated with all papers under `not collected`.

For Zotero matching, prefer exact DOI or arXiv URL-derived identifiers. Do not
introduce fuzzy title matching unless its false-positive behavior and failure
visibility are explicitly designed and tested.

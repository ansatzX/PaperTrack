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

Journal reports are issue-based. Formal volume/issue defines the article set.
Fetching is a three-tier fallback chain; each tier must expose the high-value
fields the report needs (title, authors, DOI, and the abstract where the source
provides one). Tiers, in order:

1. Official issue page via plain HTTP (no browser automation). ACS list-of-issues
   and issue TOC pages; AIP issue pages including the official issue-page
   abstract endpoint; APS issue pages. TLS fingerprinting via curl_cffi is
   permitted; browser/CDP automation is intentionally never used.
2. If the official issue page cannot be fetched or yields no articles, use the
   NCBI PubMed search path. The query must select the formal issue and must
   request abstracts, e.g.
   `https://pubmed.ncbi.nlm.nih.gov/?term=<abbrev>[jour] AND <vol>[volume] AND <issue>[issue]&format=abstract&size=200`.
   Abstracts from the result page must flow into report Abstract fields.
3. CrossRef is the last resort. It may establish issue membership and supply
   title/author/abstract only after both official and PubMed paths returned no
   usable articles for the issue.

Coverage caveat: PubMed and CrossRef are third-party indexes and may be
incomplete for a given issue (APS titles in particular have partial PubMed
coverage). Under-reporting a fallback-filled issue is accepted and expected;
do not fabricate completeness, and do not run expensive completion passes.
A fallback-filled issue is still recorded as processed, with its source marked
(`official` / `pubmed` / `crossref`) so a later re-run with a healthy official
path can repair it. A fallback that returns zero articles is a fetch failure,
not a successful empty issue.

No browser automation is used anywhere in journal fetching. The earlier APS
browser/CDP workflow (`run_aps_issue_cache.sh`, browser backends inside
`provider_runtime`) is retired; runtime caches and browser profiles are local
artifacts, not part of the fetch path.

If a journal source has strong anti-scraping behavior, prefer explicit retries,
clear failure visibility, and local preservation of prior valid reports over
weakening the source boundary.

The economic strategy is to avoid bulk crawling. PaperTrack should prefer one
official issue-page visit that yields metadata for many articles over visiting
each article page, and prefer the single PubMed issue query over per-article
crawling. Keep detailed failure records so extractors can be improved later.

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
├── pubmed_fetch.py       # PubMed/NCBI issue search fallback (abstract-exposing)
├── crossref_loi.py       # CrossRef issue discovery fallback
├── journal_fetch.py      # journal source selection: official -> PubMed -> CrossRef
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

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
Official issue membership is authoritative. Plain HTTP remains the default;
an explicit connected-browser DOM capture (`journal issue --browser-snapshot`)
can supply the official inventory and expanded abstracts. `browser/issue.mjs`
controls an already-connected user-visible browser through its supported tab
API. ACS/AIP use issue-page Abstract controls; APS uses linked Abstract pages
in a separate tab. Both operate in resumable bounded batches. Publisher Extract text is accepted in the reading abstract field, including editorials and corrections; retain its original type in provenance.

Third-party sources cross-complement one another: Europe PMC, Crossref and
PubMed supply exact-volume/issue records; OpenAlex and Semantic Scholar batch
lookups fill remaining abstracts by exact DOI. If official entries exist,
third parties must not add or remove issue members or overwrite nonempty
official fields. If official access fails, their union is provisional membership,
not proof of issue completeness. Field and membership sources are retained;
conflicts and remaining gaps are diagnosed. An empty union is a fetch failure.

A third-party rerun that loses prior report members or abstracts preserves the
prior report. A later healthy official capture can repair provisional reports.
Keep network work bounded and failure-visible; no bulk per-paper crawling by default. APS connected-browser capture explicitly
visits the known issue members' Abstract pages in bounded batches.
Discovery still supports Crossref fallback when official discovery fails.
The legacy CROSSREF_MODE flag does not disable this authorized issue-level
complement pipeline; the offline provider-runtime replay path remains isolated.

DOI casing is not a provenance signal: PubMed exposes lowercase PRL DOIs such
as `10.1103/physrevlett.129.270502`, which are canonical (DOI matching is
case-insensitive). Such reports must pass quality validation. Only malformed/
truncated PRL DOIs (missing the article number) are defects, and those are
guarded at the journal_rebuild layer. PubMed author names rendered as inline
MathML (e.g. Greek-letter collaboration names) are reduced to plain text at
parse time.

The earlier APS browser/CDP workflow remains retired from automatic journal
fetching; the explicit connected-browser module is a separate implementation. The new connected-browser collector is explicit and agent-driven;
it is not an unattended cron browser launcher. `provider_runtime` in
`journal_rebuild` still replays local snapshots without live metadata calls.

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
├── journal_fetch.py      # official membership and complementary metadata
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

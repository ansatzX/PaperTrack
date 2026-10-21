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

Journal access is agent-owned. `journal issue --run-agent` launches one bounded
Codex call (default gpt-6-luna), then Python validates the capture and writes the
report. Without that flag, `journal issue` only prints collection instructions.
A browser-capable external agent establishes access to the official issue,
then calls the packaged `papertrack/browser/collect.mjs`. Fixed code owns selectors, Abstract/Extract
expansion, DOM/HTML parsing, checkpoints and the `papertrack-browser-issue-v1`
capture. The agent must not write ad hoc parsers or reconstruct source text. Do not rebuild static
publisher HTTP clients, anti-bot backend chains or unattended CDP launchers.

Formal issue membership is authoritative. Recent pages can only complement
abstracts for already known exact DOIs; never use ASAP entries to infer membership.
When a Silverchair issue has genuine member cards without reading controls and
an observed `/<journal>/latest-articles` link, the collector returns `needs_recent`.
The same bounded agent uses `collectCuaRecentBatch` on that observed URL and its
observed rel=next links (maximum 20 pages). Keep `.recent.json` and `.recent-pages/`
beside the issue capture. Python validates the source link chain and per-DOI
Abstract HTML, and preserves issue titles/authors/membership. Missing matches
remain unresolved; unsupported recent layouts need extractor work, not guessing.
Capture verbatim Abstract or Extract, preserving the original type in provenance.
Formal correction/Erratum entries remain issue members; never omit them merely
because their title identifies a correction. Keep Extract labelled as such.
Do not turn teasers, failed clicks or blocked pages into successful abstracts.
An unavailable control requires observed evidence; unresolved work must remain
visible and resumable. If an agent cannot complete a challenge, hand off visibly.

The importer makes no publisher or third-party metadata calls. It rejects wrong
issue identities, duplicate DOI records, inconsistent counts, unresolved abstracts
and errors. Prior issue members, abstracts, anchor spelling and reading state
must survive re-runs. A consistent capture is not independent proof of completeness;
keep browser evidence and separate real acceptance from unit tests.

`browser/collect.mjs` is the core fixed collector after access. It returns
needs_access/partial/needs_attention/needs_extractor_update/wrong_issue/complete.
On a readable page with parser failures, preserve evidence and fix source/tests;
do not ask the runtime model to improvise selectors. The retired queue prototype
is recoverable from Git history. Generating task instructions does not launch
Codex/Claude or imply successful collection. Legacy static access
implementations are recoverable from checkpoint commit `8804413`.

The package owns process timeout/termination, independent run logs and acceptance.
Agent exit zero is not successful collection; missing, partial or invalid artifacts
must fail without replacing reports. Keep one invocation, with no automatic model
fallback, broader permissions or retry service. The launcher runs in a dedicated
output workspace and must not ask the child to edit code or publish reports.
Browser JS ships as package data from `src/papertrack/browser/`; browser tests
live in `tests/browser/`. Do not introduce compatibility copies or a separate
browser transport for the launcher.

## State And Migration

Generated Markdown is durable project state because it contains both generated
paper entries and user reading state. Any format change should have a migration
path for old reports when practical.

Migration tools should be local-first: changing old Markdown format should not
require re-fetching publisher pages. A migration may remove obsolete generated
content, such as old TOC image lines, while preserving task completion and paper
anchors.

Journal `latest` and `backfill` now emit agent discovery tasks. They do not update
the legacy cwd `.papertrack_state.json`; preserve it for historical reference and
missing-report discovery. Task generation must never mark an issue as processed.

`journal refresh` reconciles all formal issues in a date range (default 2026-01-01
through today). Read dated official archive pages first, including linked volumes
and year pages, and retain their HTML. Reuse today's archive snapshots; refresh
stale/missing archive pages with bounded archive-only agent calls. Python owns
archive parsing and traversal. Unknown dates, missing issue numbers and incomplete
navigation fail visibly; never infer issue membership from numbering alone.
Validate capture JSON, HTML and abstract evidence for every observed issue. Reuse
valid captures to generate reports locally; call the issue agent only for cache
gaps. Existing Markdown or a successful old status is not a cache hit. Record the
inventory and per-issue outcomes in `.papertrack-runs/*-sync-*/plan.json`.
The daily `refresh` command only fetches newly observed issues after archive
discovery. Previously observed issues without valid cache stay `repair_required`
and make the run fail visibly, without automatic re-fetch. Explicit
`journal refresh` remains the separate date-range gap repair operation.
No-argument run.sh starts independent journal and arXiv pipelines concurrently.
Journals/issue agents remain serial; arXiv keeps its date-bucket workflow. Both
pipelines finish independently; failures are logged and yield a nonzero exit.
run.sh is a fixed deployment command: one conda run with explicit arguments,
wrapped in resource limits. It accepts no user parameters. Bash owns the script
lock and starts separate arXiv and journal-only daily refresh processes in the
background, waits for both and propagates either failure. The arXiv branch chooses
initial backfill or rolling refresh per category, with one sequential request
stream. Keep script-level concurrency here; do not change Python for that purpose.

Retain real agent capture JSON, rendered issue/archive/access HTML and run logs
in the working directory's .papertrack-runs cache, including partial/failed runs.
The data directory is the Markdown export destination, not the runtime cache root.
Retain collector-owned `.readings/` per-DOI raw card/panel HTML evidence too;
Silverchair can clear old panels before the final whole-page snapshot. The
Python importer validates the panel's source URL, DOI and control binding.
Do not delete this evidence during source cleanup or use a cached partial capture
as a successful refresh. Retired static caches/profiles are unrelated to this cache.

## Project Structure

```text
src/papertrack/
├── cli.py                # public command entrypoint and daily orchestration
├── config.py             # categories and journal configuration
├── arxiv/                # stable advanced-search fetch and entry model
├── journals/             # archive inventory, bounded agent, capture validation/cache
├── reports/              # rendering, reading state, migrations and templates
├── browser/              # installed fixed JS collector
├── identity.py           # exact work identities
├── zotero_query.py       # optional exact local collection matching
├── diagnostics.py
├── adapter_result.py
├── calendar_plan.py
└── categories.toml
tests/                    # Python regression tests
tests/browser/            # JS tests importing the packaged collector directly
docs/browser/             # collector usage and dated acceptance records
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
- Use the Ruff policy in `pyproject.toml`: 80 columns, 4-space indentation,
  double quotes, sorted imports and Google-style public API docstrings.
  Use snake_case functions, PascalCase dataclasses and `_`-prefixed helpers.
- Check with `ruff check .` and `ruff format --check .`; apply formatting with
  `ruff format <changed-paths>`. Do not use unsafe fixes or suppress real errors
  merely to make lint pass. Existing code has lint debt; report remaining issues
  separately from checks passing on a focused change.
- Prefer structured dataclasses and parser logic over positional lists or ad hoc
  string handling.
- Add or update tests for parsing, source precedence, Zotero matching, report
  generation, state rebuild behavior, and migration behavior.
- Mock network and Zotero calls with `monkeypatch`; tests should not require
  live publisher sites or a running Zotero instance.
- If adding journal support, update `src/papertrack/categories.toml` and define
  the authoritative source policy for that journal.

## Environment Notes

### Resource and browser lifecycle requirements

- Keep working scripts, report backups and temporary work under the project
  `.work/` directory, never `/tmp` or `/private`. Set `TMPDIR` to the absolute
  project `.work/tmp` path for tests and helper commands. Publisher capture
  evidence remains in the working directory's `.papertrack-runs` cache.

- Agent workloads must run under a resource-limited systemd user scope, with no
  unbounded fallback. Current limits: MemoryHigh=6G, MemoryMax=8G,
  MemorySwapMax=0, CPUQuota=200%, TasksMax=256 and bounded RuntimeMaxSec.
- Existing user Chrome is outside the Agent cgroup. Do not claim that limiting
  the Agent also bounds Chrome or restart heavy browser work without checking
  that separate resource boundary after an incident.
- Do not call browser.nameSession or proactively create/name tab groups. The
  project has no dependency on tab groups. Do not mark test tabs as deliverables
  or handoffs. Close only task-created tabs on success/failure and report cleanup
  that cannot be verified; never close unrelated user tabs.
- Use exactly one bounded collector call per browser-tool invocation. Export
  full issue HTML only at first capture, errors and completion.

Zotero is optional at runtime. If Zotero is unavailable, reports should still be
generated with all papers under `not collected`.

For Zotero matching, prefer exact DOI or arXiv URL-derived identifiers. Do not
introduce fuzzy title matching unless its false-positive behavior and failure
visibility are explicitly designed and tested.

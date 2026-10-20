# PaperTrack

PaperTrack maintains a continuous reading ledger in Obsidian-friendly Markdown,
with exact DOI/arXiv matching against a local Zotero library and preserved reading
checkboxes. arXiv uses submitted-date day buckets; journals use formal volume/issue.

## arXiv

The mature arXiv path remains conventional Python: advanced-search pages expose
metadata and expanded abstracts in one request. Re-query fixed submitted dates
to repair delayed metadata; do not replace this with recent feeds.

```bash
python main.py arxiv refresh --category chem-ph,quant-ph,cs.AI,cs.LG --refresh-days 30 --data-dir /path/to/reports
python main.py arxiv backfill --category quant-ph --month 2026-09 --data-dir /path/to/reports
```

`run.sh` takes no arguments. One fixed `conda run` starts a Bash script with two
background pipelines: arXiv CLI commands and the journal-only daily `refresh`.
Journal/Codex calls remain serial. arXiv categories also share one sequential
request stream with a 10-second delay. Empty categories backfill from 2026-01-01;
existing categories refresh the last 30 days. The script waits for both pipelines
and returns a nonzero exit if either fails; a lock prevents overlapping script runs.
To run an individual task, call the package directly:

```bash
python main.py journal issue --journal jcim --volume 66 --issue 12 --year 2026 --run-agent
python main.py journal refresh --journal jcim  # reconcile issues since 2026-01-01, fetching cache gaps only
python main.py journal latest --journal jcim  # prints a discovery task only
```

Output goes to `/home/ansatz/data/obsidian/1/papertrack_datas`; edit the fixed
command in the script to change deployment paths. The daily command uses a systemd user scope
(8 GiB memory, two CPUs, no swap, 256 tasks, 24-hour ceiling). Existing Chrome is
outside these limits. Journal agents retain their own 660-second default deadline.
Daily runs log to `.papertrack_diagnostics/daily.log`, use a nonblocking lock to
avoid overlap, and continue other sources after a failure while returning nonzero.
Cron's user-bus environment is set explicitly; a working user bus and authorized
browser connection are still required. Browser challenges can require a handoff.
Python owns source iteration, locking, logging and failure status; the shell
script only supplies the deployment command and resource limits.

`journal refresh` first discovers dated formal issues from `--from-date` (default
2026-01-01) through `--to-date` (default today). It follows official archive year/
volume links, reusing today's saved archive pages and fetching only missing/stale
archive pages. "Today" uses the host's local calendar date; UTC run timestamps
are converted before comparison. Python then validates every issue's capture and HTML evidence.
Valid caches are imported locally; only missing or invalid captures launch an
issue agent. Existing Markdown alone is not a cache. Unsupported archive layouts,
undated issues and inventory gaps fail visibly instead of producing empty success.
Each archive/issue call is bounded; browser calls are serial inside the journal
pipeline. This checks observed archive coverage, not independent publisher completeness.
Plans and per-issue outcomes live under `.papertrack-runs/*-sync-*/plan.json`.
Daily `refresh` uses that discovery history to fetch only newly observed issues.
Known issues with missing or invalid caches remain `repair_required` (nonzero
exit), without automatic repeat collection. Use explicit `journal refresh` for
date-range gap repair; valid caches can still regenerate Markdown locally.
Separate `.papertrack_diagnostics/journals.log` and `arxiv.log` track the two
pipelines; `daily.log` retains the combined run and failure status.

## Journals: agent access, fixed collection

The external agent owns browser access and challenge handling in the user's
session. After the formal issue is readable, repository code owns every DOM
selector, Abstract/Extract interaction, metadata extraction and checkpoint write.
The agent must not generate a parser or reconstruct JSON from its own prose.

Run a bounded Codex collection and publish only after Python acceptance:

```bash
papertrack journal issue --journal jcim --volume 66 --issue 12 --year 2026 \
  --run-agent --data-dir /path/to/reports
```

This launches the installed `codex exec` with `gpt-6-luna`, a workspace-write
sandbox rooted in a new run directory, and a 660-second process deadline. Codex
must already be authenticated and have its connected browser tools available.
No API key or model SDK is added to PaperTrack. Override the model/deadline with
`--agent-model` / `--agent-timeout` when needed. There is one invocation, with no
automatic process retry, model fallback or permission escalation.
On Linux it requires `systemd-run --user --scope`: MemoryHigh=6G, MemoryMax=8G,
MemorySwapMax=0, CPUQuota=200%, TasksMax=256 and a matching RuntimeMaxSec.
There is no unbounded fallback if systemd is unavailable. The launcher stops the
scope on exit, including detached tool descendants. **An already-running user
Chrome is outside this cgroup**; this does not establish a browser memory limit.
The task receives the same deadline minus 60 seconds for returning results. A failed
abstract interaction allows one repeat of the same fixed call per DOI; repeated
failure still stops and preserves evidence.

Each run keeps `task.txt`, `events.jsonl`, `stderr.log`, `final.txt`, capture JSON,
rendered HTML and `result.json` under `<data-dir>/.papertrack-runs/`. The agent
only collects; the parent Python process validates issue identity, inventory and
abstracts before querying optional Zotero and writing the report. Exit code zero
alone never authorizes publication. Timeout/failure retains partial evidence and
existing reports. An explicit `--capture-path` must be a new path; completed
captures can be imported offline as shown below. Partial captures remain available
for explicit recovery through the fixed collector; the launcher does not resume
sessions automatically.

For an explicit partial-issue repair, add `--resume-capture /path/to/capture.json`
to `journal issue --run-agent` with the same journal, volume and issue. The package
copies the checkpoint and its HTML evidence into a new run, preserving the original.
APS abstracts without matching saved article HTML are collected again. ACS/AIP
Abstract and Extract text is likewise checked against retained issue controls and
panel HTML. Conflicting text is cleared only in the new checkpoint; conflicting
panel evidence is retained under a unique previous-evidence filename so the fixed
collector must capture a fresh panel. The original run remains unchanged. The resumed
capture must pass the same whole-issue and source-text checks before publication.

For APS Errata, a readable official detail page can prove that no Abstract exists:
the DOI must match, the page must identify itself as an erratum and contain Article
Text, and both the Abstract section and its navigation control must be absent.
The fixed collector retains that HTML and marks the abstract unavailable. Python
independently checks the same evidence; it never uses the correction body as an
Abstract. A blank, blocked or incompletely loaded page does not satisfy this rule.

When that verified APS Erratum replaces the legacy pseudo-abstract “This corrects
the article DOI: …”, the importer can remove that exact teaser. It first saves a
byte-identical original report under the capture run's `report-backups/` directory
and preserves reading state. Other existing abstracts retain the normal protection
against removal; a title or agent assertion alone cannot authorize this repair.

An APS issue explicitly labelled `(partial)` by the publisher is reported as
`needs_attention` / `publisher_partial_issue`, not an access challenge. Its HTML
is retained. Python also rejects that source for complete-issue import, even if
the captured article count agrees with the observed partial directory.

Cached title and author mathematics is parsed by Python too. APS titles use the
DOI-bound article's complete citation title; ACS/AIP titles use the matching issue
card's explicit formula markup. Explicit author accents are composed as Unicode.
Publisher private-use characters with retained text are preserved verbatim in
Markdown, including numeric character entities. They do not block issue import;
the importer does not guess a standard symbol or require the publisher's font.
For CSS-only MathJax glyphs, retained MathML is required; empty glyphs without that
source fail visibly instead of disappearing from the report.

For Abstract/Extract content, a formula whose layout cannot be parsed is kept as
its existing nonempty source text. No missing symbol is guessed. Normal formulas
still render as TeX; the package logs each literal fallback with its DOI and HTML
evidence path. Empty formulas, loading/error messages, missing abstracts and
source identity or text mismatches still fail. This fixed policy also applies
when reusing caches; titles and authors retain their strict rendering rules.

These run directories are also the persistent webpage cache. Keep their original
Abstract/Extract types, issue HTML, archive HTML (`capture.json.archive.html`) and
failed access/parser HTML (`capture.json.access.html`) for offline extractor
repairs. `result.json` lists available artifacts. Partial and failed attempts are
retained and never treated as published reports; there is no automatic cache
expiration. Old static-backend caches and private browser profiles are retired.

To print the access-and-handoff task for manual execution instead, omit `--run-agent`:

```bash
python main.py journal issue --journal jcim --volume 66 --issue 12 --year 2026 \
  --capture-path .work/cache/jcim-66-12.json --data-dir /path/to/reports
```

This prints instructions; it does not itself launch a model or browser. For the
current Codex trial, explicitly use `gpt-6-luna` on both launch and resume. The
agent needs a supported connected browser-client tab and a runtime able to import
local JS modules. No model SDK is required by the fixed collector.

The installed `papertrack/browser/cua.mjs` is the Codex handoff adapter, running
inside the SAME `cua_repl` as browser access. It binds the documented DOM tab and
calls the packaged `collect.mjs`. Repository `browser/*.mjs` entrypoints forward
to this implementation; there is one collector, also included in the wheel.
Do not import it in a separate `node_repl` and assume browser variables are shared.
The latter module is the fixed collection entrypoint. It probes access without
navigating or solving challenges, calls the ACS/AIP or APS collector in bounded
batches, and atomically saves a checkpoint. Panel IDs come from `aria-controls`,
not inferred control-ID strings. A new tab can resume the same checkpoint; prior
members and nonempty text cannot regress. Optional rendered HTML is saved beside
the JSON to diagnose parsers locally. Parsing uses the authenticated browser DOM,
without a second publisher HTTP fetch. HTML exports occur on the first batch,
errors and completion, rather than repeatedly serializing the entire page for
every partial batch. One collector call is permitted per browser-tool invocation.
The agent must not name browser sessions, create tab groups or mark tabs as
deliverables. The adapter closes its task tabs on completion; the agent must
also close task tabs before returning on failure and report unconfirmed cleanup.

Statuses separate `needs_access`, `partial`, `needs_attention`,
`needs_extractor_update`, `wrong_issue` and `complete`. The agent may repair access
and repeat the module call. On a readable page with parser failures, preserve
artifacts and report the defect for a code/test change; do not improvise selectors.
Details and supported browser API are in [browser/README.md](browser/README.md).

Import only a completed capture:

```bash
python main.py journal issue --journal jcim --volume 66 --issue 12 --year 2026 \
  --browser-snapshot .work/cache/jcim-66-12.json --data-dir /path/to/reports
```

Import makes no publisher or metadata API calls; Zotero's optional local API
still supplies collection matching. The `papertrack-browser-issue-v1` schema and
old captures remain supported. New fixed captures identify
`capture_method: "fixed_browser"` and their collector version. Expanded Abstract
and Extract both populate the reading field with their original type retained.
Failed/unloaded controls remain gaps, not unavailable abstracts.
For issue members without a reading control, an observed same-journal
`latest-articles` link activates the fixed recent-page collector. It collects
Abstracts only for exact issue DOIs, follows observed `rel=next` links within the
same bounded invocation, and saves `.recent.json` plus `.recent-pages/` HTML and
panel evidence. Python validates those sources and fills only the Abstract;
issue membership, titles and authors remain authoritative. Exhausted pages with
unmatched DOIs fail visibly. This supports the observed Silverchair URL/DOM
contract; APS still uses its existing DOI-specific abstract-page path.

Validation rejects wrong issue identities, duplicate DOIs, inconsistent counts,
unresolved abstracts/errors and replacement data that loses prior members or
abstracts. It preserves anchor spelling, supported task edits and collection
state. A consistent observed inventory is not independent proof of publisher
coverage; live acceptance remains issue/provider-specific.

Supported configurations: `jcim`, `jctc`, `jacs`, `jcp`, `prl`, in
`src/papertrack/categories.toml`.

`journal latest` and `journal backfill --from-year YYYY` now print discovery tasks
for the agent. They no longer discover or fetch issues themselves, and do not
update the legacy cwd `.papertrack_state.json`. Existing state is retained for
historical reference and missing-report discovery by `repair`.

The previous `run_j.sh`, APS cache runner, publisher HTTP clients, automatic
browser/CDP runtimes and third-party network enrichment chain have been removed.
Their implementation and earlier acceptance records are recoverable from commit
`8804413`. Existing production reports, snapshots, profiles, diagnostics and cron
configuration have not been migrated or deleted. Any external schedule pointing
at a removed script must be retired before it is used again.

The optional [journal worker](experiments/journal-workflow/README.md) remains available as an
older queue prototype. The new handoff uses the fixed browser module directly;
it is independent of the Python `--run-agent` launcher. Claude launching is not implemented; `journal refresh` is the
automatic date-range reconciliation entrypoint.

## Reports and local maintenance

```text
reports/
├── arxiv/quant-ph/2026/10/14.md
├── acs/jcim/66/12.md
├── aip/jcp/164/16.md
└── aps/prl/136/1.md
```

```bash
python main.py migrate --data-dir /path/to/reports
python main.py repair --data-dir /path/to/reports --no-fetch
```

Migration is local and retains reading state. Repair still supports arXiv
refetching; damaged journal reports are deferred with an agent-task command.
Internal source provenance stays in capture JSON and HTML evidence, rather than
being repeated in the reading Markdown. To remove old generated provenance
comments without changing any other report text or consulting captures, run:

```bash
python main.py migrate --remove-provenance-only --data-dir /path/to/reports --backup-dir /path/to/project/.work/backups/provenance
```

This operation skips hidden cache directories and saves each changed report's
original bytes under the backup directory before writing.

Zotero is optional; enable its local API in desktop settings when using it.
Matching uses exact identifiers, not fuzzy titles.

ACS/AIP capture imports require the collector's rendered HTML sidecar at
`<capture-path>.html`, including manual `--browser-snapshot` imports. Before
rendering Markdown, Python verifies article DOI, reading control/panel and source
type against this HTML, and compares Abstract text with the official non-graphical
abstract section. Extract is accepted only when the issue card offers no Abstract.
Whitespace and mathematical Unicode font differences are normalized for comparison
only. Missing evidence or conflicting text fails without overwriting the report.

APS captures additionally retain each visited abstract page in
`<capture-path>.articles/<sha256-of-lowercase-doi>.html`. Python verifies the DOI
and `abstract-section-content` text against these files before import. Keep this
directory with the capture; older APS captures without detail HTML need a fresh
collection before reimport. Existing reports are preserved.

## Development

### Repository layout

The daily Python `refresh` command migrates existing reports for each configured
journal before collection. It preserves paper text, reading checkboxes and paths,
including legacy `volume/issue/report.md`, while updating the generated schema
and removing old TOC image lines. Migration errors are logged and make the daily
run fail even if a later source fetch succeeds. Format migration does not certify
the historical abstract source; that still requires publisher evidence.

Before migration, daily refresh can upgrade an older ACS/AIP Extract capture
that exactly matches the former paragraph-only selector when retained official
HTML contains additional `div.block-child-p` paragraphs. The package copies the
capture and its evidence to a new run directory, records the original hashes,
and validates the entire repaired capture before using the normal report
importer. Original evidence remains unchanged. These records use
`kind=cached_extract_repair` and `agent_invoked=false`; they are not browser
calls. Nonempty but unsupported reading formulas use the literal source policy
above; unrelated text conflicts remain failures.
The importer must succeed before the repaired run is recorded as complete;
an interrupted import can resume from its validated local cache.

Presentation migration also restores source-backed mathematical title formatting
when the current title exactly matches the old captured or rendered title.
User-modified titles are preserved. Repairing an existing report's presentation
does not certify issue completeness or add a future, in-progress issue to the
daily date-range inventory.

An official `In Progress` or `partial` marker bound to the current issue is a
completeness failure even when every currently visible article was captured.
The collector retains failure evidence and does not replace a prior checkpoint;
Python also rejects these sources during import and presentation migration.
Markers for other issues in the navigation menu do not trigger this check.

All journal reports use `volume/issue.md`, for example `acs/jctc/22/1.md`.
The importer never maintains a second `volume/issue/report.md`. Retire an old
layout locally, without fetching or regenerating paper metadata:

```bash
python main.py migrate --journal-layout-only --data-dir /path/to/reports --backup-dir /path/to/project/.work/backups/journal-layout
```

The migration backs up both originals, preserves completed tasks from either
copy, and removes the old `report.md` and its empty directory. A lone old report
moves byte-for-byte. Duplicate files may differ only in generated presentation
or task completion; conflicting notes, anchors or article text leave both files
untouched and fail visibly. Other assets in the old directory remain in place.
The importer requires this migration before updating an issue with both layouts.

```text
run.sh                   Fixed daily deployment command
main.py                  Python CLI entrypoint
src/papertrack/
  cli.py, config.py      Commands and configuration
  arxiv/                 arXiv fetch and entry model
  journals/              Issue discovery, agent, capture validation and cache
  reports/               Rendering, templates, reading state and migrations
  browser/               Installed fixed JavaScript collector
tests/                   Python tests and fixtures
browser/                 Compatibility entrypoints and JavaScript tests
scripts/                 Local maintenance tools
docs/                    Documentation index, historical plans and experiments
experiments/             Optional older prototypes
```

See [the documentation map](docs/README.md). Generated reports and raw browser
captures live under the configured data directory; `.papertrack-runs/` retains
successful, partial and failed agent evidence. Local diagnostics and historical
`.papertrack_state.json` remain separate from source code.

See [INSTALL.md](INSTALL.md) for installation and optional browser components.

```bash
pip install -e '.[dev]'
pytest -q
node --test browser/*.test.mjs
cd experiments/journal-workflow && npm test
```

Tests mock network and Zotero. Live publisher access and an agent's end-to-end
collection require separate acceptance in a review output directory.

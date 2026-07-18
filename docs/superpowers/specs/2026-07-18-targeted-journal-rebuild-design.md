# Targeted Journal Rebuild Design

## Goal

PaperTrack will rebuild only journal reports selected by an offline quality
audit. arXiv remains independently scheduled by `run.sh`. Journal rebuilding
uses `run_j.sh`, runs ACS, AIP, and APS concurrently, and keeps each publisher's
requests strictly sequential and conservatively spaced.

The rebuild must never erase an existing report because an official source was
blocked, unavailable, or parsed suspiciously. CrossRef remains metadata-only
and cannot define issue membership.

## Audit Scope And Result

The read-only LLM-assisted audit examined all 1,145 existing formal journal
reports (32,914 paper entries):

| Provider / journal | Reports | Must rebuild | Review by official refetch | Keep |
|---|---:|---:|---:|---:|
| ACS / JCIM | 156 | 0 | 1 | 155 |
| ACS / JCTC | 144 | 1 | 0 | 143 |
| AIP / JCP | 409 | 0 | 32 | 377 |
| APS / PRL | 436 | 2 | 7 | 427 |
| Total | 1,145 | 3 | 40 | 1,102 |

The audit did not classify an issue solely because one correction, editorial,
comment, reply, erratum, retraction, or publisher note lacked an abstract.
Frontmatter/path/provider mismatches, duplicate DOI records, empty reports,
missing titles, and missing authors were also checked and were not found.

### Definite rebuilds

- `acs/jctc/22/12.md`: all 30 papers lack abstracts and titles contain raw
  HTML fragments.
- `aps/prl/127/27.md`: all 40 papers lack abstracts; DOI casing and raw MathML
  expose the old CrossRef-membership report.
- `aps/prl/129/27.md`: all 20 papers have the same old-source defects, including
  truncated titles.

### Official-refetch review set

ACS, first priority:

```text
acs/jcim/66/12.md
```

AIP, with `160/{4,5,6,9}` first because adjacent issues show batch-like
abstract failures:

```text
aip/jcp/148/10.md
aip/jcp/148/11.md
aip/jcp/148/13.md
aip/jcp/148/14.md
aip/jcp/148/15.md
aip/jcp/148/24.md
aip/jcp/149/7.md
aip/jcp/149/24.md
aip/jcp/150/4.md
aip/jcp/150/20.md
aip/jcp/153/12.md
aip/jcp/153/21.md
aip/jcp/154/9.md
aip/jcp/154/22.md
aip/jcp/155/5.md
aip/jcp/155/9.md
aip/jcp/157/16.md
aip/jcp/158/5.md
aip/jcp/158/23.md
aip/jcp/159/14.md
aip/jcp/160/4.md
aip/jcp/160/5.md
aip/jcp/160/6.md
aip/jcp/160/9.md
aip/jcp/161/4.md
aip/jcp/161/7.md
aip/jcp/161/19.md
aip/jcp/162/19.md
aip/jcp/162/22.md
aip/jcp/163/12.md
aip/jcp/163/21.md
aip/jcp/164/22.md
```

APS:

```text
aps/prl/122/10.md
aps/prl/123/25.md
aps/prl/124/22.md
aps/prl/125/12.md
aps/prl/131/6.md
aps/prl/132/13.md
aps/prl/133/20.md
```

## Process Separation

`run.sh` becomes arXiv-only. It does not export a journal CrossRef mode and
contains no journal scheduling loop.

`run_j.sh` is a stable journal wrapper. It exports:

```text
PAPERTRACK_CROSSREF_MODE=metadata
PAPERTRACK_ACS_ISSUE_INTERVAL=30
PAPERTRACK_AIP_ISSUE_INTERVAL=30
PAPERTRACK_AIP_ABSTRACT_INTERVAL=10
PAPERTRACK_APS_ISSUE_INTERVAL=30
```

It starts three independent processes and waits for all of them:

- `scripts/rebuild_acs_candidates.py`
- `scripts/rebuild_aip_candidates.py`
- `scripts/rebuild_aps_candidates.py`

One provider failing does not terminate the other providers. `run_j.sh` exits
nonzero after all workers finish if any worker has a fetch failure or unresolved
candidate.

## Worker Boundaries

Each worker owns one publisher and processes its candidates sequentially. This
keeps publisher caches, throttling clocks, and failure responsibility local.

- ACS orders JCIM 66/12 before JCTC 22/12.
- AIP orders 160/4, 160/5, 160/6, and 160/9 before the other JCP candidates.
- APS orders the definite 127/27 and 129/27 repairs before its review set.

The three entry-point scripts share a small module for manifest loading,
configuration validation, report dispatch, progress persistence, and summary
rendering. Provider candidate definitions remain separate so one publisher's
selection can change without changing the others.

## Manifest And Runtime State

JSON files are local runtime data and are not committed. Candidate definitions
are committed in the provider scripts. On first execution the shared runner
materializes:

```text
.papertrack_diagnostics/journal_rebuild_manifest.json
```

The manifest records schema version, audit kind, provider, journal, volume,
issue, relative path, classification, priority, and reasons. Workers validate
that manifest paths match their structured fields before fetching.

Each provider stores resumable state separately:

```text
.papertrack_diagnostics/journal_rebuild_acs_state.json
.papertrack_diagnostics/journal_rebuild_aip_state.json
.papertrack_diagnostics/journal_rebuild_aps_state.json
```

State writes use a temporary file plus `os.replace`. Terminal outcomes are:

- `resolved`: authoritative fetch succeeded and the targeted defect disappeared;
- `official_success_unresolved`: authoritative membership succeeded but the
  targeted missing field remains;
- `fetch_failed`: authoritative fetch did not succeed and the old report was
  preserved.

Default reruns skip `resolved` and `official_success_unresolved` items to avoid
repeated publisher traffic. A prior `fetch_failed` item is eligible on the next
run. A deliberate `--retry-unresolved` option retries
`official_success_unresolved`; `--reset-progress` requires explicit use and does
not delete Markdown.

## Request Policy

All official issue-page requests use a minimum 30-second provider interval.
The larger of the environment value and the built-in safety floor wins; a
misconfigured smaller value cannot weaken the floor.

AIP abstract AJAX uses a separate 10-second minimum clock. This applies to
direct HTTP and to the Playwright/Xvfb browser loop. Browser abstract calls are
strictly sequential. ACS, AIP, and APS may run concurrently because they are
independent publisher domains; requests within one publisher never overlap.

Transient errors use bounded retries. A detected challenge or explicit block
stops HTTP retries immediately. AIP may still make its single bounded browser
fallback. `Retry-After` overrides local backoff.

## Fetch And Report Safety

Workers call the existing explicit-volume/issue report path with
`SourcePolicy(metadata)`. Official adapters still define membership. CrossRef
may fill only a missing abstract for an exact DOI already returned by the
official issue adapter.

Before a rebuild, the existing Markdown is read but not removed. The report
layer writes only after an authoritative success with nonempty entries.
Fetch-blocked, transport-failed, and suspicious-empty results leave the old file
unchanged. Existing task completion and supported Obsidian task suffixes remain
preserved by the report renderer.

After a successful write, the worker re-runs local quality checks relevant to
that candidate's reasons. An official success that still lacks an optional
abstract is recorded as unresolved instead of being silently declared repaired.

## Testing

Tests use temporary data directories and mocked adapters; no test contacts a
publisher or Zotero.

Coverage includes:

- `run.sh` contains only arXiv work;
- `run_j.sh` starts exactly three workers, waits for all, and exports the stable
  source and interval policy;
- provider candidate order, including JCIM first;
- three providers can overlap while each provider remains sequential;
- issue intervals cannot be configured below 30 seconds;
- direct and browser AIP abstract intervals cannot be configured below 10
  seconds;
- manifest validation rejects path/provider/volume/issue mismatches;
- successful progress resumes without refetching;
- failed official fetches preserve old reports and do not become completed;
- post-fetch quality checks distinguish resolved and unresolved reports;
- runtime JSON remains ignored by Git.

## Acceptance Criteria

- arXiv and journal scheduled runs are independent.
- Only the 43 audited issues are scheduled by default.
- ACS, AIP, and APS workers run concurrently; one provider's requests are
  sequential.
- JCIM 66/12 is the first ACS issue attempted.
- Issue requests have a non-bypassable 30-second floor.
- AIP abstract requests have a non-bypassable 10-second floor in HTTP and
  browser modes.
- Official failures never erase an existing Markdown report.
- Runtime JSON files are not committed.
- A rerun is resumable and does not recontact publishers for terminal items by
  default.

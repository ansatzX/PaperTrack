# arXiv Category Membership Repair Design

Date: 2026-07-20
Status: approved in conversation; awaiting written-spec review

## Problem

PaperTrack must build each arXiv day ledger from papers whose official subject
list contains the configured category, whether that category is primary or a
cross-list. The current `chem-ph`, `cs.AI`, and `cs.LG` configurations instead
search the advanced-search field `cross_list_category`. That field excludes
papers whose primary category is the target.

Official same-window comparisons demonstrated the failure:

| Category | Current primary | Current cross-list | Correct union |
|---|---:|---:|---:|
| `physics.chem-ph` | 0 | 5 | 11 = 6 + 5 |
| `cs.AI` | 0 | 92 | 141 = 49 + 92 |
| `cs.LG` | 0 | 57 | 136 = 79 + 57 |

The archive-backed configurations `quant-ph`, `hep-ex`, `hep-lat`, `hep-ph`,
and `hep-th` returned both primary and cross-listed papers with no observed
wrong-category members and do not need query-shape changes.

## Approaches Considered

### Selected: high-recall official query plus exact subject post-filter

Use the previously proven advanced-search `all fields` category term for
`physics.chem-ph`, `cs.AI`, and `cs.LG`. Treat that search as candidate recall,
not final membership. Accept a parsed entry only when its official subject tags
contain the exact requested category.

This keeps one issue-day request in the common case, restores primary and
cross-listed membership, and makes false positives from an all-fields text
match impossible to write into a report.

### Rejected: use `cross_list_category` alone

This is the current behavior. It is precise for cross-lists but systematically
excludes primary-category papers.

### Rejected: fetch a broad archive and filter locally

Fetching all Physics or all Computer Science papers for every day would recover
the desired set, but it weakens the project's economic strategy and may require
many pagination requests. It is unnecessary when the category term already
provides high-recall candidates.

## Query Configuration

For `chem-ph`, `cs.AI`, and `cs.LG`:

- keep the broad arXiv classification family required by the advanced form;
- restore an empty first title term and a second `all` term containing the
  exact category string;
- remove `cross_list_category` from these configurations;
- make the hardcoded fallback in `codex.py` identical to `categories.toml`.

The five archive-backed configurations remain unchanged.

## Membership Boundary

`query_arxiv_result` will accept an optional `required_category` argument. The
report layer must pass its ledger category on every production fetch.

For every parsed candidate:

1. If subject metadata is absent, fail the page parse. A missing authoritative
   membership field must not look like a valid empty result.
2. If `required_category` is present in `entry.subjects`, accept the entry.
3. Otherwise reject it as an all-fields false positive.

Pagination termination must use the unfiltered page result count. Using the
accepted count could stop after a page of false positives and miss valid papers
on later pages.

Adapter metadata should record candidate, accepted, and rejected counts so a
future query change can be audited without reading Markdown.

## Failure And State Protection

- A page with missing subject metadata returns `parse_failed` and writes
  nothing.
- A successful query that loses an ID from an existing report remains subject
  to the current shrink guard; the old report survives.
- Empty accepted results do not replace existing reports.
- Task state, collection state, anchors, and user edits retain the protections
  added by the arXiv refresh repair.
- No historical Markdown is bulk-overwritten as part of the code change.

## Tests

Tests must prove collection semantics rather than URL spelling:

- the three affected configs use the high-recall `all fields` form;
- the five archive configs remain unchanged;
- primary and cross-listed candidates are both accepted;
- an all-fields false positive is rejected;
- missing subjects cause parse failure;
- pagination continues based on raw candidate count even when a page filters to
  zero accepted entries;
- the report layer supplies its category to the adapter;
- fallback configuration matches TOML configuration.

Network calls remain mocked in automated tests.

## Historical Data Audit

After code verification, perform a read-only audit of existing `chem-ph`,
`cs.AI`, and `cs.LG` reports. For each day, compare existing IDs with the exact
official accepted set and classify:

- missing primary-category papers;
- missing cross-listed papers;
- non-member papers;
- exact matches;
- official fetch or parse failures.

The audit produces a machine-readable repair manifest. A later repair run may
use that manifest, but it must preserve old reports on fetch failure or set
shrinkage and must not require manual per-paper decisions.

## Success Criteria

- Official subject membership, not query-field naming, owns category inclusion.
- All eight category configurations have an explicit, tested membership path.
- `chem-ph`, `cs.AI`, and `cs.LG` include both primary and cross-listed papers.
- No wrong-category candidate can reach generated Markdown.
- Existing reading state remains intact throughout code deployment and audit.

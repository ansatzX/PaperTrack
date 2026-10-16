# Browser issue capture — verified 2026-09-11

The connected Chrome module completed three real publisher issues and the
PaperTrack CLI generated reports from all three captures.

| Issue | Unique official records | Reading entries after existing filters | Full abstracts captured / available |
|---|---:|---:|---:|
| [JCIM 66/12](https://pubs.acs.org/jcisd8/issue/66/12) | 43 | 41 | 39 / 39 |
| [JCP 164/16](https://pubs.aip.org/aip/jcp/issue/164/16) | 42 | 41 | 41 / 41 |
| [PRL 136/1](https://journals.aps.org/prl/issues/136/1) | 56 | 56 | 56 / 56 |

Total: **138 reading entries, 136 full abstracts**. JCIM has two journal-information
records excluded by existing filters and two Extract-only editorial/correction
records now populated from the publisher Extract, per the user’s explicit reading policy. JCIM therefore has 41/41 populated reading abstract fields (39 Abstract + 2 Extract). The publisher type remains in source provenance. JCP's erratum is excluded by
existing filters. APS has 67 displayed cards because 11 highlighted papers
repeat in the sectioned contents; these are deduplicated by DOI.

## Implemented

- `browser/issue.mjs`: real ACS/AIP issue navigation and Abstract-button
  interaction through a supplied connected browser-client tab. It waits for the
  issue inventory, opens each abstract, reads it, then closes its floating panel
  so it cannot obstruct the next button. Bounded batches can resume from saved
  captures without revisiting successful abstracts.
- `browser/aps_issue.mjs`: keeps the APS issue inventory tab and visits the
  linked Abstract pages in a second tab. It verifies each page's DOI and extracts
  only the abstract container, preserving rendered MathJax text. Issue summaries
  are not abstracts. No PDF or full-text link is clicked.
- `journal issue --browser-snapshot`: validates publisher, formal issue,
  per-card placement and DOI inventory, then renders through the normal report
  writer. Wrong or incomplete identity data fails before replacing a report.
- Europe PMC, Crossref and PubMed cross-complement missing fields or provisional
  membership when official access fails. Known official membership is retained;
  OpenAlex/Semantic Scholar only enrich exact existing DOIs. Provenance, conflicts,
  missing abstracts and provider failures remain visible.
- The ACS HTTP parser also recognizes the observed new Silverchair layout.

## Acceptance evidence

- Full Python suite: **577 passed** after the Extract policy update.
- JavaScript interaction tests: **5 passed** (overlay handling/resume, issue
  identity, membership drift, APS deduplication and abstract DOI mismatch).
- `git diff --check`: passed.
- A fresh-tab smoke invoked `captureIssue` with navigation enabled: it opened
  JCIM 66/12 and captured one abstract successfully (43 cards, no error).
- The completed three browser captures were consumed by the real `main.py
  journal issue --browser-snapshot` CLI. Reports contain respectively 41/41/56
  reading entries, 41/41/56 populated Abstract fields, and 41/41/56 provenance records.
- A real-capture report was checked, rerendered, and verified to preserve the
  completed checkbox; the temporary review copy was then restored.

All report acceptance outputs are under `/tmp/papertrack-browser-review/` and
are included in the delivery archive. Source captures are under
`.papertrack_official_cache/browser/`. The archive contains SHA-256 hashes.

## Failures encountered and repaired

Initially the browser connection timed out; Chrome, extension and native-host
checks passed. After the user approved opening a new window with the current
Chrome profile, browser control recovered. No extension reinstall or profile
replacement was performed. A real screenshot identified overlapping abstract
panels as the cause of missed subsequent clicks; the module now closes each
panel after reading. AIP and APS each had a transient abstract gap, subsequently
revisited successfully. Final captures have no remaining available abstracts
and no unresolved errors. Future captures retain accumulated error history.

Live Europe PMC query for JCIM 66/12 returned 41 records, 39 with abstracts.
An additional DOI enrichment probe rejected a short OpenAlex fragment; its
Semantic Scholar batch call failed with URLError. It remained a gap rather than
accepting a fragment. This does not affect the three completed official-browser
captures. Machine counts are in `metadata-live-validation.json` in the archive.

## Operating boundary

The module uses an **agent-connected existing Chrome session**. The Python
snapshot flag imports its result; it does not itself start or control Chrome.
No unattended cron/browser-launch integration is claimed. Publisher access can
change; three verified issues do not prove universal access or universal layout
compatibility. Other issues use the same adapters with explicit failure checks.

No production Obsidian reports, processed-issue state, cron jobs, or repository
commits were changed. Code changes are local and reviewable; the archive includes
an applyable patch against the recorded Git base revision.

## Extract policy update (2026-09-11)
The user explicitly requested equal treatment of the JCIM editorial and correction.
Both issue-page Extracts were opened in the real Chrome session and saved, without
re-fetching the 39 existing abstracts. Capture remaining=0, errors=[]. Original
abstract_type=extract is preserved; imported field provenance is
`official_browser_extract`. Mapping’s publisher snippet ends in an ellipsis,
which is preserved. The correction includes the displayed corrected caption range.

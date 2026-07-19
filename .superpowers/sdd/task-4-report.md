# Task 4 Report: ACS official snapshot and persistent-browser adapter

## Status

Complete. ACS issue fetching now uses the provider-scoped persistent runtime,
validates and reuses durable official snapshots, and never launches a one-shot
browser subprocess.

## RED evidence

After replacing the obsolete fallback tests with cache-first/runtime tests, the
first required ACS run did not complete: the cache-first test entered the old
live HTTP path because `query_acs_issue_result` ignored the supplied runtime.
The pytest processes were terminated after approximately 28 seconds. This was
the expected missing-behavior failure: a seeded snapshot did not prevent network
access.

After the first implementation pass, the focused suite produced two useful
validation failures:

```text
2 failed, 12 passed, 31 deselected in 0.16s
```

- the membership-reparse test fixture altered validation membership rather than
  the manifest membership hash;
- a page with zero issue cards was classified as `parse_failed` before the
  explicit suspicious-empty check.

The test fixture and validation order were corrected without weakening the
requirements.

## GREEN evidence

Required focused suite:

```text
PYTHONPATH=src /home/ansatz/soft/miniconda3/envs/arxiv/bin/pytest -q \
  tests/test_acs_browser_fallback.py tests/test_journal.py -k acs

14 passed, 31 deselected in 0.15s
```

Fresh full-suite verification:

```text
PYTHONPATH=src /home/ansatz/soft/miniconda3/bin/conda run -n arxiv pytest -q

330 passed in 7.56s
```

## Implemented behavior

- A valid ACS snapshot is loaded and reparsed before any live fetch.
- Freshly parsed, normalized DOI membership is hashed and compared with the
  stored manifest membership hash before a snapshot hit is accepted.
- Live ACS navigation uses only the exact formal issue URL and required selector
  through `ProviderRuntime.fetch_issue`.
- Successful live pages require at least one issue selector/card and at least
  one valid `10.1021/...` DOI, then atomically commit the raw HTML with selector
  counts, DOI membership, and parser version.
- Results expose `official_transport`, `snapshot_path`, `selector_counts`, and
  `membership_hash` after a validated snapshot or live commit.
- Blocked, circuit-open, fetch, parse, suspicious-empty, and snapshot-commit
  failures retain the existing ACS diagnostic identity context.
- Blocked/challenge, zero-card, missing-DOI, parser-exception, and fetch-failure
  paths never commit over prior snapshot state.
- `src/papertrack/acs_browser.py`, `_fetch_acs_browser`, subprocess imports, and
  ACS `xvfb-run` test assumptions were removed.

## Self-review

- Runtime-backed calls bypass the legacy direct-call in-memory/HTTP compatibility
  path, so cache validation cannot be skipped and runtime tests cannot contact
  live ACS.
- The adapter invokes only `fetch_issue`; it performs no article-page or
  same-origin follow-up navigation.
- Snapshot commit occurs strictly after complete page validation. The snapshot
  store's atomic manifest replacement preserves the previous valid manifest if
  commit itself fails.
- The old no-runtime direct HTTP path remains only for existing parser/API
  compatibility tests; production rebuild orchestration supplies the runtime.

## Files

- Modified `src/papertrack/acs_fetch.py`
- Deleted `src/papertrack/acs_browser.py`
- Replaced `tests/test_acs_browser_fallback.py`
- Modified `tests/test_journal.py`
- Removed the obsolete browser-helper assumption from
  `tests/test_adapter_result.py`

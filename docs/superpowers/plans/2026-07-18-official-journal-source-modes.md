# Official Journal Source Modes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make development/default journal fetches use only official ACS, AIP, and APS issue surfaces while allowing `run.sh` to opt into CrossRef discovery and abstract completion without delegating issue membership to CrossRef.

**Architecture:** Parse one immutable `SourcePolicy` at the CLI boundary and pass it through report orchestration to journal source selection. Provider adapters own official issue membership and use a shared bounded HTTP request policy; CrossRef is a separately gated discovery/metadata service. Field-level diagnostics distinguish legitimate missing abstracts from endpoint failures and optional production fills.

**Tech Stack:** Python 3.10+, dataclasses/Enum, argparse, curl_cffi, BeautifulSoup/lxml, Jinja2, pytest/monkeypatch.

## Global Constraints

- Default/unset `PAPERTRACK_CROSSREF_MODE` is `disabled`; development makes zero CrossRef requests.
- `PAPERTRACK_CROSSREF_MODE=metadata` is enabled only by `run.sh`.
- Invalid values stop execution with an actionable error.
- CrossRef never adds/removes official issue members or replaces official DOI/title/author/volume/issue/page/date fields.
- No adapter visits individual article pages; AIP may use its official issue-page abstract AJAX endpoint and one bounded Playwright/Xvfb fallback for a Cloudflare issue-page challenge.
- Official failure does not write an empty report, erase a prior report, or mark an issue processed.
- Requests are sequential, bounded, cached, and observable; challenge pages stop retries.
- Tests do not require live publisher sites or Zotero.

---

### Task 1: Explicit Source Policy

**Files:** Create `src/papertrack/source_policy.py`; modify `src/papertrack/cli.py`; create `tests/test_source_policy.py`; modify `tests/test_cli_subcommands.py`.

**Interfaces:** Produce `CrossrefMode`, immutable `SourcePolicy`, and `SourcePolicy.from_environment(env=None)`. CLI handlers pass `source_policy` through every journal path.

- [ ] **Step 1: Write the failing tests**

```python
def test_default_policy_is_official_only():
    policy = SourcePolicy.from_environment({})
    assert policy.crossref_metadata_enabled is False
    assert policy.crossref_discovery_enabled is False


def test_metadata_policy_enables_production_crossref():
    policy = SourcePolicy.from_environment({"PAPERTRACK_CROSSREF_MODE": "metadata"})
    assert policy.crossref_metadata_enabled is True
    assert policy.crossref_discovery_enabled is True


def test_invalid_policy_value_fails_closed():
    with pytest.raises(RuntimeError, match="PAPERTRACK_CROSSREF_MODE"):
        SourcePolicy.from_environment({"PAPERTRACK_CROSSREF_MODE": "membership"})
```

- [ ] **Step 2: Run the focused test and verify RED**

```bash
/home/ansatz/soft/miniconda3/bin/conda run -n arxiv pytest -q tests/test_source_policy.py
```

Expected: import/collection failure because the policy module does not exist.

- [ ] **Step 3: Implement the minimal policy**

```python
class CrossrefMode(str, Enum):
    DISABLED = "disabled"
    METADATA = "metadata"


@dataclass(frozen=True)
class SourcePolicy:
    crossref_mode: CrossrefMode = CrossrefMode.DISABLED

    @classmethod
    def from_environment(cls, env=None):
        raw = (os.environ if env is None else env).get(
            "PAPERTRACK_CROSSREF_MODE", "disabled"
        ).strip().lower() or "disabled"
        try:
            return cls(CrossrefMode(raw))
        except ValueError as exc:
            raise RuntimeError(
                "PAPERTRACK_CROSSREF_MODE must be 'disabled' or 'metadata'"
            ) from exc

    @property
    def crossref_metadata_enabled(self):
        return self.crossref_mode is CrossrefMode.METADATA

    @property
    def crossref_discovery_enabled(self):
        return self.crossref_mode is CrossrefMode.METADATA
```

- [ ] **Step 4: Parse once in `cli.main()` and pass into `_run_journal_latest`, `_run_journal_backfill`, and `_run_journal_issue`.** Direct library tests pass `SourcePolicy.official_only()` explicitly.
- [ ] **Step 5: Run `pytest -q tests/test_source_policy.py tests/test_cli_subcommands.py tests/test_journal.py` and commit `feat: add explicit journal source policy`.**

---

### Task 2: Gate CrossRef And Preserve Official Membership

**Files:** modify `src/papertrack/journal_fetch.py`, `src/papertrack/report.py`, `src/papertrack/repair.py`; test `tests/test_journal.py`, `tests/test_report.py`, `tests/test_repair.py`.

**Interfaces:** Add `source_policy: SourcePolicy` to `query_journal_issue_full_result`, `filter_journal_to_md`, `filter_journal_auto`, and repair calls.

- [ ] **Step 1: Add a failing test that monkeypatches `_fetch_year` to raise and proves official-only mode still returns official entries without CrossRef.**
- [ ] **Step 2: Add a failing metadata-mode test proving CrossRef fills only an exact DOI abstract and cannot add a CrossRef-only DOI or replace title/authors.**
- [ ] **Step 3: Add a failing AIP auto-discovery test proving official-only `latest/backfill` fails closed with guidance to use explicit `journal issue`; CrossRef discovery must not be called.**
- [ ] **Step 4: Run focused tests and observe RED.**

```bash
/home/ansatz/soft/miniconda3/bin/conda run -n arxiv pytest -q tests/test_journal.py tests/test_report.py tests/test_repair.py
```

- [ ] **Step 5: Gate `_record_crossref_abstract_fill` on `source_policy.crossref_metadata_enabled`; retain no CrossRef issue-membership fallback.**
- [ ] **Step 6: Gate AIP CrossRef volume/issue discovery on `crossref_discovery_enabled`; official `journal issue` remains available in default mode.**
- [ ] **Step 7: Thread policy through rebuild/repair and run the focused suite plus full `pytest -q`.**
- [ ] **Step 8: Commit `fix: keep CrossRef behind production source policy`.**

---

### Task 3: Bounded Respectful HTTP Fetching

**Files:** modify `src/papertrack/http_fetch.py`; test `tests/test_http_fetch.py`.

**Interfaces:** Add `HttpRequestPolicy(min_interval, max_attempts=3, backoff=(2.0, 5.0))`; add `request_policy` and explicit `strategies` parameters to `fetch_html_resilient`; retain `HttpFetchResult.history` with status, delay, and retry-after evidence.

- [ ] **Step 1: Write failing tests proving a challenge causes immediate stop and a 2-attempt policy never makes a third request.**
- [ ] **Step 2: Write failing tests proving `Retry-After` overrides backoff and provider throttles share a monotonic clock.**
- [ ] **Step 3: Run `pytest -q tests/test_http_fetch.py` and observe RED.**
- [ ] **Step 4: Replace client×variant×round Cartesian iteration with an explicit bounded attempt list. Retry only transport errors, 408/425/429, and 5xx; stop on challenge/403.**
- [ ] **Step 5: Add per-provider monotonic throttling and truthful PaperTrack contact headers.**
- [ ] **Step 6: Run focused and full tests; commit `fix: bound and throttle publisher issue requests`.**

---

### Task 4: ACS Official Issue Fields And Diagnostics

**Files:** modify `src/papertrack/acs_fetch.py`, `src/papertrack/adapter_result.py`, `src/papertrack/diagnostics.py`; test `tests/test_adapter_result.py`, `tests/test_journal.py`, `tests/test_diagnostics.py`.

- [ ] **Step 1: Add a fixture test with a normal ACS card and a correction card; assert official membership, no article-page URL, and `official_field_absent` only for the correction abstract.**
- [ ] **Step 2: Add a failing diagnostics test for one-write append of each JSONL event, preventing the observed interleaved malformed lines.**
- [ ] **Step 3: Run focused tests and observe RED.**
- [ ] **Step 4: Configure ACS with one bounded request policy (`min_interval=1`, at most 3 attempts) and stop on challenge.**
- [ ] **Step 5: Record `article_count`, `missing_fields`, request history, and field-level DOI/title/author/abstract outcomes. Missing optional abstracts remain official success; missing DOI/title makes the issue partial/invalid.**
- [ ] **Step 6: Implement atomic JSONL append using one `os.open(...O_APPEND...)` and one `os.write()` per event.**
- [ ] **Step 7: Run focused and full tests; commit `fix: make ACS issue extraction authoritative and observable`.**

---

### Task 5: AIP Official Issue, Browser Fallback, And Abstract AJAX

**Files:** modify `src/papertrack/aip_fetch.py`; test `tests/test_journal.py`, `tests/test_adapter_result.py`, `tests/test_report.py`.

**Interfaces:** Add `AipAbstractResult(abstract, outcome, status_code, message)` and return it from `_fetch_aip_abstract`; cache terminal results by official `articleId`. Add an internal browser fallback that returns the official issue HTML and keeps one browser context for the issue's official abstract AJAX calls.

- [ ] **Step 1: Add failing tests for official abstract success, control absent, empty response, blocked response, transport failure, and one challenge-triggered browser fallback. Each checks DOI, article ID, URL, status, and field.**
- [ ] **Step 2: Add a failing session/cache test proving duplicate article IDs cause one AJAX request and no article-detail URL is requested.**
- [ ] **Step 3: Run `pytest -q tests/test_journal.py tests/test_adapter_result.py -k aip` and observe RED.**
- [ ] **Step 4: Reuse one AIP HTTP session, fetch the canonical issue URL first, use a legacy URL only for genuine 404, and enforce sequential 1-second issue/AJAX spacing with bounded attempts. A challenge may trigger exactly one headful Playwright fallback under `xvfb-run`; block images/fonts/media, keep scripts/cookies, and allow only the issue URL plus journal-scoped `/aip/{journal}/PlatformArticle/ArticleAbstractAjax` calls. Do not use the browser for ACS or APS.**
- [ ] **Step 5: Emit per-DOI diagnostics for every non-success outcome; never collapse all failures to an unclassified empty string.**
- [ ] **Step 6: Verify blocked issue results are not cached, browser fallback is attempted at most once per issue, journal-scoped AJAX is used, and report/state preservation remains intact.**
- [ ] **Step 7: Run focused and full tests; commit `fix: make AIP issue and abstract failures explicit`.**

---

### Task 6: APS Historical Cards And Provenance Repair

**Files:** modify `src/papertrack/aps_fetch.py`, `src/papertrack/migration.py`; create minimized `tests/fixtures/aps/prl-127-27.html` and `prl-129-27.html`; test `tests/test_journal.py`, `tests/test_migration.py`, `tests/test_report.py`.

- [ ] **Step 1: Create minimized fixtures from saved official snapshots containing representative cards, one current heading and one historical `h4` heading, and one no-summary non-research item.**
- [ ] **Step 2: Add failing fixture-count/metadata tests for both snapshots.**
- [ ] **Step 3: Add failing provenance tests: official `10.1103/PhysRevLett...` is not corrupt; old lower-case `10.1103/physrevlett...` is a repair candidate.**
- [ ] **Step 4: Run APS/migration/report tests and observe RED.**
- [ ] **Step 5: Use `.title a[href*=...]`, bounded APS requests (`min_interval=10`, max 3), and actual selector counts.**
- [ ] **Step 6: Make only the CrossRef provenance pattern case-sensitive; do not classify all normal official APS DOI casing as corrupt.**
- [ ] **Step 7: Run focused and full tests; commit `fix: parse historical APS issue cards from official pages`.**

---

### Task 7: Production Wrapper And Documentation

**Files:** modify `run.sh`, `README.md`, `tests/test_config.py`, `tests/test_cli_subcommands.py`.

- [ ] **Step 1: Add a failing test requiring `run.sh` to export `PAPERTRACK_CROSSREF_MODE=metadata` and never mention membership fallback.**
- [ ] **Step 2: Run the focused config/CLI tests and observe RED.**
- [ ] **Step 3: Export the mode only in `run.sh`; leave journal scheduling paused unless separately enabled.**
- [ ] **Step 4: Document official-only explicit issue development and production metadata mode; state that official issue success is still mandatory.**
- [ ] **Step 5: Run focused tests and commit `docs: enable CrossRef metadata only in stable wrapper`.**

---

### Task 8: Bounded Live Verification And Completion Audit

**Files:** create `scripts/verify_official_journal_issues.py`; create `tests/test_live_verifier.py`; modify `README.md`.

- [ ] **Step 1: Add failing verifier tests proving selected providers only, official-only policy, nonzero on block/no entries, and no report/state imports.**
- [ ] **Step 2: Implement fixed low-impact checks: ACS `jcisd8/66/12`, AIP `jcp/164/16`, APS `prl/127/27`. The verifier never uses Zotero, CrossRef, report rendering, or state writes.**
- [ ] **Step 3: Run verifier tests GREEN.**
- [ ] **Step 4: Run offline verification in `arxiv`:**

```bash
/home/ansatz/soft/miniconda3/bin/conda run -n arxiv pytest -q
/home/ansatz/soft/miniconda3/bin/conda run -n arxiv python -m compileall -q src tests scripts
```

- [ ] **Step 5: Run one bounded live check:**

```bash
/home/ansatz/soft/miniconda3/bin/conda run -n arxiv python scripts/verify_official_journal_issues.py --provider all
```

Report AIP Cloudflare blocking as external evidence if it persists; never convert it into successful parsing.

- [ ] **Step 6: Audit every design acceptance criterion, inspect `git status`, then commit `test: add bounded official journal verification`.**
- [ ] **Step 7: Run final `pytest -q`, compileall, and clean-worktree verification before claiming completion.**

## Plan Self-Review

- Source policy coverage: Tasks 1, 2, and 7.
- Official adapter coverage: Tasks 4, 5, and 6.
- No-article-page and respectful-request constraints: Tasks 3, 5, and 8.
- Failure visibility and state preservation: Tasks 2, 4, 5, and 6.
- Live evidence and final audit: Task 8.
- No placeholders remain; AIP external Cloudflare blocking is explicitly treated as evidence, not silently declared solved.

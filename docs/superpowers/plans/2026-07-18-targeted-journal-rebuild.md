# Targeted Journal Rebuild Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild only 43 audited journal issues through three independent, resumable provider workers while keeping arXiv scheduling separate and enforcing conservative request floors.

**Architecture:** `run_j.sh` launches one ACS, AIP, and APS Python worker concurrently; each worker processes its own committed candidate definitions sequentially through a shared runner. Runtime manifests and progress live under `.papertrack_diagnostics/`, while publisher adapters enforce a 30-second issue floor and AIP enforces a separate 10-second abstract floor in HTTP and browser paths.

**Tech Stack:** Python 3.10+, dataclasses, JSON, pathlib, subprocess/bash, curl_cffi, Playwright, pytest/monkeypatch.

## Global Constraints

- `run.sh` performs arXiv work only; `run_j.sh` performs journal work only.
- Only the 43 issues in the approved design are scheduled by default.
- ACS, AIP, and APS may run concurrently; requests within one provider are sequential.
- Official issue pages define membership; CrossRef is exact-DOI abstract completion only.
- Official failure preserves the old Markdown and does not become success.
- Issue request intervals have a non-bypassable 30-second floor.
- Direct and browser AIP abstract requests have a non-bypassable 10-second floor.
- JSON manifests and progress are runtime files under `.papertrack_diagnostics/` and remain uncommitted.
- Tests do not contact publishers, CrossRef, Zotero, or write the real data directory.

---

### Task 1: Conservative Provider Request Floors

**Files:**
- Modify: `src/papertrack/acs_fetch.py`
- Modify: `src/papertrack/aip_fetch.py`
- Modify: `src/papertrack/aip_browser.py`
- Modify: `src/papertrack/aps_fetch.py`
- Test: `tests/test_http_fetch.py`
- Test: `tests/test_aip_fetch.py`

**Interfaces:**
- Produces: `configured_interval(env_name: str, floor: float) -> float` in `http_fetch.py`.
- Produces: AIP role-aware `_wait_for_aip_interval(role: str) -> float` with separate `issue` and `abstract` clocks.
- Consumes: `PAPERTRACK_ACS_ISSUE_INTERVAL`, `PAPERTRACK_AIP_ISSUE_INTERVAL`, `PAPERTRACK_AIP_ABSTRACT_INTERVAL`, and `PAPERTRACK_APS_ISSUE_INTERVAL`.

- [ ] **Step 1: Write failing floor tests**

Add tests proving values below the floor cannot weaken it and larger values win:

```python
def test_configured_interval_enforces_floor():
    assert configured_interval("PAPERTRACK_TEST_INTERVAL", 30, {}) == 30
    assert configured_interval("PAPERTRACK_TEST_INTERVAL", 30, {"PAPERTRACK_TEST_INTERVAL": "5"}) == 30
    assert configured_interval("PAPERTRACK_TEST_INTERVAL", 30, {"PAPERTRACK_TEST_INTERVAL": "45"}) == 45
```

Add an AIP fake-clock test that makes one issue request and two abstract requests and asserts issue spacing is at least 30 seconds while abstract spacing is at least 10 seconds.

- [ ] **Step 2: Run tests and verify RED**

```bash
PYTHONPATH=src /home/ansatz/soft/miniconda3/bin/conda run -n arxiv \
  pytest -q tests/test_http_fetch.py tests/test_aip_fetch.py
```

Expected: missing interval helper and role-aware AIP clock assertions fail.

- [ ] **Step 3: Implement interval parsing and provider policies**

Implement strict numeric parsing:

```python
def configured_interval(name, floor, env=None):
    values = os.environ if env is None else env
    raw = values.get(name, "").strip()
    if not raw:
        return float(floor)
    try:
        return max(float(floor), float(raw))
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a non-negative number") from exc
```

Construct ACS and APS `HttpRequestPolicy` values with 30-second floors. Split AIP clocks and pass `role="issue"` from `_fetch_issue_http` and `role="abstract"` from `_fetch_aip_abstract`.

- [ ] **Step 4: Pace browser AJAX**

Pass the configured abstract interval to `papertrack.aip_browser`. Before each browser AJAX `page.evaluate`, wait until the prior abstract request start plus the interval. Keep issue navigation separate from abstract pacing.

- [ ] **Step 5: Run focused and full tests**

```bash
PYTHONPATH=src /home/ansatz/soft/miniconda3/bin/conda run -n arxiv pytest -q tests/test_http_fetch.py tests/test_aip_fetch.py
PYTHONPATH=src /home/ansatz/soft/miniconda3/bin/conda run -n arxiv pytest -q
```

- [ ] **Step 6: Commit**

```bash
git add src/papertrack/{http_fetch,acs_fetch,aip_fetch,aip_browser,aps_fetch}.py tests/test_http_fetch.py tests/test_aip_fetch.py
git commit -m "fix: enforce conservative publisher request floors"
```

---

### Task 2: Shared Rebuild Manifest, Progress, And Quality Checks

**Files:**
- Create: `src/papertrack/journal_rebuild.py`
- Create: `tests/test_journal_rebuild.py`
- Modify: `.gitignore`

**Interfaces:**
- Produces: immutable `RebuildCandidate(provider, journal, volume, issue, relative_path, classification, priority, reasons)`.
- Produces: `materialize_manifest(path, candidates) -> None`.
- Produces: `load_provider_candidates(path, provider) -> list[RebuildCandidate]`.
- Produces: `run_provider_rebuild(provider, candidates, data_dir, diagnostics_dir, retry_unresolved=False, reset_progress=False, dry_run=False) -> RebuildSummary`.
- Produces: atomic provider progress JSON and `resolved`, `official_success_unresolved`, `fetch_failed` outcomes.

- [ ] **Step 1: Write failing model and validation tests**

Cover candidate JSON round-trip, stable priority ordering, and rejection when `relative_path` disagrees with provider/journal/volume/issue:

```python
candidate = RebuildCandidate("acs", "jcim", "66", "12", "acs/jcim/66/12.md", "ambiguous_review", 10, ("ordinary_article_missing_abstract",))
assert candidate.expected_relative_path == candidate.relative_path
```

- [ ] **Step 2: Write failing progress/safety tests**

Mock `filter_journal_to_md` and prove:

- resolved progress skips a later run;
- unresolved progress skips unless `retry_unresolved=True`;
- fetch failure leaves the original bytes unchanged and is eligible next run;
- progress writes use `os.replace`;
- a successful write is assessed against candidate reasons before being marked resolved.
- dry-run returns the ordered candidate keys without network, Markdown, manifest,
  or progress writes.

- [ ] **Step 3: Run tests and verify RED**

```bash
PYTHONPATH=src /home/ansatz/soft/miniconda3/bin/conda run -n arxiv pytest -q tests/test_journal_rebuild.py
```

Expected: `papertrack.journal_rebuild` cannot be imported.

- [ ] **Step 4: Implement structured runtime state**

Use dataclasses rather than positional dictionaries. Manifest schema is:

```json
{"schema_version": 1, "audit": {"kind": "offline_markdown_quality"}, "issues": []}
```

Write progress to `{diagnostics_dir}/journal_rebuild_{provider}_state.json` using a same-directory temporary path, `flush`, `os.fsync`, and `os.replace`.

- [ ] **Step 5: Implement dispatch and post-fetch assessment**

Resolve journal config from `journal_configs`, call `filter_journal_to_md` with `SourcePolicy(CrossrefMode.METADATA)`, and preserve the original file on a false/exception outcome. Assess targeted reasons:

- `raw_html` and `raw_mathml`: fail while generated tags remain;
- `lowercase_crossref_doi`: fail while lower-case APS headings remain;
- `whole_issue_missing_abstracts`: fail while every entry lacks an abstract;
- `ordinary_article_missing_abstract`: unresolved only when a non-exempt ordinary article remains without an abstract.

- [ ] **Step 6: Run focused and full tests**

```bash
PYTHONPATH=src /home/ansatz/soft/miniconda3/bin/conda run -n arxiv pytest -q tests/test_journal_rebuild.py tests/test_report.py
PYTHONPATH=src /home/ansatz/soft/miniconda3/bin/conda run -n arxiv pytest -q
```

- [ ] **Step 7: Commit**

```bash
git add .gitignore src/papertrack/journal_rebuild.py tests/test_journal_rebuild.py
git commit -m "feat: add resumable targeted journal rebuild runner"
```

---

### Task 3: Independent Provider Candidate Workers

**Files:**
- Create: `scripts/rebuild_acs_candidates.py`
- Create: `scripts/rebuild_aip_candidates.py`
- Create: `scripts/rebuild_aps_candidates.py`
- Create: `tests/test_journal_rebuild_workers.py`

**Interfaces:**
- Each script produces `default_candidates() -> tuple[RebuildCandidate, ...]` and `main(argv=None) -> int`.
- Each script consumes `run_provider_rebuild` from Task 2.

- [ ] **Step 1: Write failing exact-set and priority tests**

Assert counts ACS=2, AIP=32, APS=9 and total=43. Assert the first ACS candidate is `acs/jcim/66/12.md`, the first four AIP paths are `160/{4,5,6,9}`, and the first two APS paths are `127/27` and `129/27`.

- [ ] **Step 2: Run tests and verify RED**

```bash
PYTHONPATH=src /home/ansatz/soft/miniconda3/bin/conda run -n arxiv pytest -q tests/test_journal_rebuild_workers.py
```

Expected: worker modules do not exist.

- [ ] **Step 3: Implement ACS worker**

Define JCIM 66/12 at priority 10 and JCTC 22/12 at priority 20 with the exact classifications and reasons from the design. Parse `--data-dir`, `--diagnostics-dir`, `--retry-unresolved`, `--reset-progress`, and `--dry-run`.

- [ ] **Step 4: Implement AIP worker**

Define all 32 approved JCP candidates. Give volumes/issues 160/4, 160/5, 160/6, and 160/9 priority 10 and the remaining candidates priority 20.

- [ ] **Step 5: Implement APS worker**

Define PRL 127/27 and 129/27 as `must_rebuild` priority 10, followed by the seven approved review issues at priority 20.

- [ ] **Step 6: Test worker argument forwarding**

Monkeypatch `run_provider_rebuild` and assert each script forwards only its provider candidates, the configured paths, and retry/reset flags. No worker imports another provider worker.

- [ ] **Step 7: Run focused and full tests**

```bash
PYTHONPATH=src /home/ansatz/soft/miniconda3/bin/conda run -n arxiv pytest -q tests/test_journal_rebuild_workers.py
PYTHONPATH=src /home/ansatz/soft/miniconda3/bin/conda run -n arxiv pytest -q
```

- [ ] **Step 8: Commit**

```bash
git add scripts/rebuild_*_candidates.py tests/test_journal_rebuild_workers.py
git commit -m "feat: add independent provider rebuild workers"
```

---

### Task 4: Separate arXiv And Journal Wrappers

**Files:**
- Modify: `run.sh`
- Create: `run_j.sh`
- Modify: `tests/test_config.py`
- Modify: `README.md`

**Interfaces:**
- `run.sh` invokes only arXiv commands.
- `run_j.sh` invokes exactly the three Task 3 workers concurrently and returns after all finish.

- [ ] **Step 1: Write failing wrapper tests**

Parse non-comment shell lines and assert:

- `run.sh` contains neither `PAPERTRACK_CROSSREF_MODE` nor journal commands;
- `run_j.sh` exports metadata mode and exact 30/30/10/30 interval values;
- `run_j.sh` starts all three scripts with `&`, stores their PIDs, waits for every PID, and does not stop waiting after the first failure.

- [ ] **Step 2: Run tests and verify RED**

```bash
PYTHONPATH=src /home/ansatz/soft/miniconda3/bin/conda run -n arxiv pytest -q tests/test_config.py
```

Expected: `run_j.sh` is absent and `run.sh` still exports metadata mode.

- [ ] **Step 3: Make `run.sh` arXiv-only**

Remove journal comments/configuration and `PAPERTRACK_CROSSREF_MODE`. Preserve the current conda environment, categories, refresh/backfill behavior, diagnostics directory, and arXiv delay/backoff exports.

- [ ] **Step 4: Implement `run_j.sh`**

Use `set -uo pipefail` so a worker failure can be collected rather than terminating the wait loop. Start three conda commands in the background, capture PIDs, wait for all, aggregate a nonzero status, and use a trap to forward TERM/INT to active children.

- [ ] **Step 5: Document operation and recovery**

Document separate cron entries, candidate-only behavior, progress files, `--retry-unresolved`, interval floors, and the fact that runtime JSON is local and ignored.

- [ ] **Step 6: Verify shell and tests**

```bash
bash -n run.sh
bash -n run_j.sh
PYTHONPATH=src /home/ansatz/soft/miniconda3/bin/conda run -n arxiv pytest -q tests/test_config.py
```

- [ ] **Step 7: Commit**

```bash
git add run.sh run_j.sh README.md tests/test_config.py
git commit -m "feat: separate arXiv and journal rebuild schedules"
```

---

### Task 5: Completion Audit And Controlled Execution

**Files:**
- Modify: `tests/test_live_verifier.py` only if new import-boundary assertions are required.

**Interfaces:**
- Consumes all prior tasks.
- Produces verified local manifest/progress behavior and bounded production launch evidence.

- [ ] **Step 1: Run offline verification**

```bash
PYTHONPATH=src /home/ansatz/soft/miniconda3/bin/conda run -n arxiv pytest -q
PYTHONPATH=src /home/ansatz/soft/miniconda3/bin/conda run -n arxiv python -m compileall -q src tests scripts
bash -n run.sh
bash -n run_j.sh
git diff --check
```

Expected: zero failures and zero syntax/diff errors.

- [ ] **Step 2: Dry-run candidate scheduling**

Execute all three workers with their Task 3 `--dry-run` option. Verify exactly 43 unique issue keys, ACS begins with JCIM 66/12, and no network or Markdown write occurs.

- [ ] **Step 3: Audit source boundaries**

Use `rg` to prove provider scripts do not import one another, `run.sh` has no journal mode, and no worker calls CrossRef membership or article-detail fetch functions.

- [ ] **Step 4: Launch `run_j.sh` once**

Run the wrapper in the configured `arxiv` conda environment. Observe that three workers start independently. Do not interpret publisher blocking as empty data; report each provider's first result and preserve all pre-existing reports on failure.

- [ ] **Step 5: Inspect runtime state and Git cleanliness**

Confirm manifest/progress JSON is under `.papertrack_diagnostics/`, no runtime file is tracked, and `git status --short` contains only intentional source changes before the final commit.

- [ ] **Step 6: Final commit if audit tests changed**

```bash
git add tests/test_live_verifier.py
git commit -m "test: audit targeted journal rebuild boundaries"
```

- [ ] **Step 7: Run final verification again**

Repeat the full pytest, compileall, `bash -n`, `git diff --check`, and clean-worktree commands before reporting completion.

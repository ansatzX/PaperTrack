# Unattended Official-Issue Fetch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an unattended, official-only journal fetch path that reuses one persistent Chrome profile per publisher, caches validated official issue responses, fails closed on challenges, and reports real success rates.

**Architecture:** `run_j.sh` starts one worker per publisher. Each worker opens one explicit `ProviderRuntime`, passes it through rebuild/report/source dispatch into the provider adapter, and closes Chrome when the worker exits while retaining its profile. Provider adapters validate publisher-owned issue representations before atomically committing them to an offline snapshot store.

**Tech Stack:** Python 3.10+, Playwright with Chrome Stable, BeautifulSoup/lxml, dataclasses, pathlib, fcntl, pytest, Bash

## Global Constraints

- Only publisher-owned HTTPS issue pages and same-origin resources loaded by those pages may supply journal metadata.
- CrossRef discovery, membership, and metadata fill are disabled for `run_j.sh` journal workers.
- Do not navigate to individual article pages.
- Keep a minimum 30-second interval between issue navigations for each publisher.
- On challenge, keep the same session, wait 60 seconds, and reload once.
- Open the provider circuit after three consecutive issues remain blocked.
- Chrome processes live only for one `run_j.sh` execution; profiles persist between executions.
- Existing valid Markdown survives every fetch, parse, validation, cache, or browser failure.
- Official snapshots and browser profiles are runtime cache state and must not be committed.
- Tests must not require live publisher sites, Chrome, a GUI session, or Zotero.

---

## File Structure

- Create `src/papertrack/official_snapshot.py`: immutable snapshot records, hash verification, atomic cache writes, and cache lookup.
- Create `src/papertrack/provider_runtime.py`: provider URL policy, persistent Chrome lifecycle, pacing, challenge retry, response capture, and circuit state.
- Modify `src/papertrack/adapter_result.py` and `src/papertrack/diagnostics.py`: make `circuit_open` a structured abnormal source outcome.
- Modify `src/papertrack/journal_rebuild.py`: explicitly own one provider runtime for the whole candidate sequence and expose fetch/report metrics.
- Modify `src/papertrack/report.py`: pass the runtime to journal source dispatch without affecting arXiv.
- Modify `src/papertrack/journal_fetch.py`: pass runtime and snapshot store to authoritative adapters and remove CrossRef from the production journal path.
- Modify `src/papertrack/acs_fetch.py`: consume cached/live official ACS issue representations; remove the per-issue browser subprocess.
- Modify `src/papertrack/aps_fetch.py`: consume cached/live official APS issue representations; remove the per-issue browser subprocess.
- Modify `src/papertrack/aip_fetch.py`: reuse the provider runtime for the issue page and official abstract endpoint.
- Delete `src/papertrack/acs_browser.py`, `src/papertrack/aps_browser.py`, and `src/papertrack/aip_browser.py` after all adapters use `ProviderRuntime`.
- Modify `scripts/rebuild_acs_candidates.py`, `scripts/rebuild_aip_candidates.py`, and `scripts/rebuild_aps_candidates.py`: supply profile/cache roots and print rate summaries.
- Modify `run_j.sh`: enforce official-only policy and pass runtime roots.
- Modify `.gitignore`: ignore persistent profiles and official snapshots.
- Create focused tests under `tests/test_official_snapshot.py`, `tests/test_provider_runtime.py`, and provider integration test files.

---

### Task 1: Atomic validated official snapshot store

**Files:**
- Create: `src/papertrack/official_snapshot.py`
- Create: `tests/test_official_snapshot.py`
- Modify: `.gitignore`

**Interfaces:**
- Produces: `SnapshotKey(provider, journal, volume, issue)`
- Produces: `SnapshotAttachment(name, media_type, body)`
- Produces: `SnapshotValidation(selector_counts, membership_dois, parser_version)`
- Produces: `OfficialSnapshotStore(root).load(key) -> OfficialSnapshot | None`
- Produces: `OfficialSnapshotStore(root).commit(key, canonical_url, final_url, media_type, body, validation) -> OfficialSnapshot`

- [ ] **Step 1: Write failing tests for path safety, atomic commit, and hash validation**

```python
def test_snapshot_round_trip_and_corruption_rejection(tmp_path):
    store = OfficialSnapshotStore(tmp_path)
    key = SnapshotKey("acs", "jcim", "66", "12")
    validation = SnapshotValidation(
        selector_counts={"div.issue-item.clearfix": 31},
        membership_dois=("10.1021/acs.jcim.6c00001",),
        parser_version="acs-v1",
    )
    stored = store.commit(
        key,
        canonical_url="https://pubs.acs.org/toc/jcisd8/66/12",
        final_url="https://pubs.acs.org/toc/jcisd8/66/12",
        media_type="text/html",
        body=b"<div class='issue-item clearfix'></div>",
        validation=validation,
        attachments=(
            SnapshotAttachment(
                name="abstract-123.json",
                media_type="application/json",
                body=b'{"Success": true}',
            ),
        ),
    )
    assert store.load(key) == stored
    assert stored.attachment_paths["abstract-123.json"].read_bytes() == b'{"Success": true}'
    stored.body_path.write_bytes(b"corrupt")
    assert store.load(key) is None


def test_snapshot_key_rejects_path_components():
    with pytest.raises(ValueError, match="safe path component"):
        SnapshotKey("acs", "../jcim", "66", "12")
```

- [ ] **Step 2: Run the snapshot tests and verify the missing module failure**

Run: `PYTHONPATH=src /home/ansatz/soft/miniconda3/bin/conda run -n arxiv pytest -q tests/test_official_snapshot.py`

Expected: collection fails with `ModuleNotFoundError: No module named 'papertrack.official_snapshot'`.

- [ ] **Step 3: Implement immutable records, safe paths, and atomic writes**

Implement these exact public types in `official_snapshot.py`:

```python
@dataclass(frozen=True)
class SnapshotKey:
    provider: str
    journal: str
    volume: str
    issue: str

    def __post_init__(self) -> None:
        for value in (self.provider, self.journal, self.volume, self.issue):
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
                raise ValueError("snapshot key requires a safe path component")


@dataclass(frozen=True)
class SnapshotAttachment:
    name: str
    media_type: str
    body: bytes


@dataclass(frozen=True)
class SnapshotValidation:
    selector_counts: dict[str, int]
    membership_dois: tuple[str, ...]
    parser_version: str


@dataclass(frozen=True)
class OfficialSnapshot:
    key: SnapshotKey
    canonical_url: str
    final_url: str
    media_type: str
    content_hash: str
    membership_hash: str
    captured_at: str
    validation: SnapshotValidation
    body_path: Path
    attachment_paths: dict[str, Path]


class OfficialSnapshotStore:
    def __init__(self, root: str | os.PathLike):
        self.root = Path(root)

    def load(self, key: SnapshotKey) -> OfficialSnapshot | None:
        """Return only a manifest/body pair whose hashes and exact schema validate."""

    def commit(
        self,
        key: SnapshotKey,
        canonical_url: str,
        final_url: str,
        media_type: str,
        body: bytes,
        validation: SnapshotValidation,
        attachments: tuple[SnapshotAttachment, ...] = (),
    ) -> OfficialSnapshot:
        """Fsync immutable content blobs, then atomically replace the issue manifest."""
```

Use SHA-256 over raw bytes for `content_hash`, every attachment, and over sorted normalized DOI strings for `membership_hash`. Store bodies as immutable files below `blobs/sha256/{digest}`. Fsync each missing blob before writing the issue manifest; then replace only `provider/journal/volume/issue/manifest.json` atomically. Attachment names must match `[A-Za-z0-9_.-]+` and be unique. Reject a manifest with extra or missing keys. `load` returns `None` if the main body or any attachment hash fails. Never delete an old blob during commit, so an exception before manifest replacement leaves the previous snapshot readable.

- [ ] **Step 4: Ignore both runtime state roots**

Append exactly these lines to `.gitignore`:

```gitignore
.papertrack_browser_profiles/
.papertrack_official_cache/
```

- [ ] **Step 5: Run focused tests and commit**

Run: `PYTHONPATH=src /home/ansatz/soft/miniconda3/bin/conda run -n arxiv pytest -q tests/test_official_snapshot.py`

Expected: all tests pass.

```bash
git add .gitignore src/papertrack/official_snapshot.py tests/test_official_snapshot.py
git commit -m "feat: add validated official issue snapshots"
```

---

### Task 2: Persistent provider browser runtime and circuit breaker

**Files:**
- Create: `src/papertrack/provider_runtime.py`
- Create: `tests/test_provider_runtime.py`
- Modify: `src/papertrack/adapter_result.py`
- Modify: `src/papertrack/diagnostics.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: `OfficialSnapshotStore`
- Produces: `IssueRequest`, `CapturedResponse`, `OfficialBrowserResult`, `ProviderRunStats`, `ProviderRuntime`
- Produces: `ProviderRuntime.open()`, `close()`, `fetch_issue(request)`, and `request_same_origin(url, method, form, min_interval)`

- [ ] **Step 1: Write failing tests for profile reuse, pacing, challenge recovery, and circuit opening**

Use injected `clock`, `sleep`, and `playwright_factory` callables so tests remain deterministic:

```python
def test_runtime_reuses_context_and_opens_circuit_after_three_blocks(tmp_path):
    fake = FakePlaywright(challenge_pages=4)
    clock = FakeClock()
    runtime = ProviderRuntime(
        provider="aps",
        profile_root=tmp_path / "profiles",
        snapshot_root=tmp_path / "snapshots",
        playwright_factory=lambda: fake,
        clock=clock.time,
        sleep=clock.sleep,
        issue_interval=30.0,
        challenge_retry_delay=60.0,
        max_consecutive_blocks=3,
    )
    with runtime:
        outcomes = [runtime.fetch_issue(aps_request(index)).outcome for index in range(4)]
    assert outcomes == [BLOCKED, BLOCKED, BLOCKED, CIRCUIT_OPEN]
    assert fake.persistent_context_launches == 1
    assert clock.sleeps.count(60.0) == 3
    assert runtime.stats.blocked == 3
    assert runtime.stats.circuit_skipped == 1
    assert fake.closed is True
```

Also assert that a second issue navigation waits until 30 seconds after the first navigation and that a successful issue resets `consecutive_blocks` to zero. Add `test_profile_directory_is_private` asserting `stat.S_IMODE(profile_dir.stat().st_mode) == 0o700`, and `test_profile_lock_rejects_concurrent_owner` asserting a second runtime for the same provider/profile raises `RuntimeError("provider profile is already owned")` before Chrome launches.

- [ ] **Step 2: Run tests and verify the missing runtime module failure**

Run: `PYTHONPATH=src /home/ansatz/soft/miniconda3/bin/conda run -n arxiv pytest -q tests/test_provider_runtime.py`

Expected: collection fails because `papertrack.provider_runtime` does not exist.

- [ ] **Step 3: Implement the explicit runtime types and URL policy**

Implement these public records:

```python
CIRCUIT_OPEN = "circuit_open"


@dataclass(frozen=True)
class IssueRequest:
    canonical_url: str
    required_selector: str
    challenge_markers: tuple[str, ...]
    capture_url_patterns: tuple[re.Pattern[str], ...] = ()


@dataclass(frozen=True)
class CapturedResponse:
    url: str
    status_code: int
    media_type: str
    body: bytes


@dataclass(frozen=True)
class OfficialBrowserResult:
    outcome: str
    canonical_url: str
    final_url: str
    status_code: int | None
    html: str
    captured: tuple[CapturedResponse, ...]
    message: str = ""
    attempt_count: int = 1


@dataclass
class ProviderRunStats:
    attempted: int = 0
    live_success: int = 0
    snapshot_hit: int = 0
    blocked: int = 0
    fetch_failed: int = 0
    circuit_skipped: int = 0
```

`ProviderRuntime` must map providers to exact official hosts:

```python
OFFICIAL_HOSTS = {
    "acs": frozenset({"pubs.acs.org"}),
    "aip": frozenset({"pubs.aip.org"}),
    "aps": frozenset({"journals.aps.org"}),
}
```

Reject non-HTTPS, non-allowlisted, credential-bearing, fragment-bearing, and non-issue URLs before navigation. Permit same-origin XHR only after the canonical issue page has established the origin. Do not expose a general navigation method.

Define `CIRCUIT_OPEN` in `adapter_result.py`, import it into `provider_runtime.py`, and add it to `diagnostics.ABNORMAL_OUTCOMES` so circuit skips remain visible even when all-attempt logging is disabled.

`request_same_origin(url, method, form, min_interval)` uses the existing browser context's request API, applies a role-specific interval, and returns `CapturedResponse`. It rejects a URL whose origin differs from the canonical issue origin even when the host is otherwise allowlisted.

During issue navigation, capture same-origin `xhr` and `fetch` responses with `text/html`, `application/json`, or `application/xml` media types up to 10 MiB. Explicit `capture_url_patterns` select responses eligible for adapter parsing. Other same-origin response bodies are not used as metadata; diagnostics retain only URL, status, media type, and hash so a later extractor change has concrete evidence without accepting arbitrary JSON.

- [ ] **Step 4: Implement one persistent Chrome context and bounded recovery**

`ProviderRuntime.__enter__` calls Playwright `launch_persistent_context` exactly once with:

```python
self._context = chromium.launch_persistent_context(
    user_data_dir=str(self.profile_dir),
    channel="chrome",
    headless=False,
)
```

Create the profile directory with mode `0o700`, hold an exclusive `fcntl.flock` on `.owner.lock`, register response capture before navigation, wait for `required_selector`, detect challenge markers in title and HTML, wait 60 seconds and reload once in the same page, and close context plus Playwright in `__exit__`. A third consecutive blocked issue sets the circuit open; later calls return `CIRCUIT_OPEN` without network contact.

- [ ] **Step 5: Make the browser dependency part of development installs**

Add `playwright>=1.40` to the `dev` extra while retaining the existing `browser` extra. This makes the deployed `arxiv` test/development environment explicit without making browser support mandatory for arXiv-only installations.

- [ ] **Step 6: Run tests and commit**

Run: `PYTHONPATH=src /home/ansatz/soft/miniconda3/bin/conda run -n arxiv pytest -q tests/test_provider_runtime.py tests/test_official_snapshot.py`

Expected: all tests pass without starting Chrome.

```bash
git add pyproject.toml src/papertrack/adapter_result.py src/papertrack/diagnostics.py src/papertrack/provider_runtime.py tests/test_provider_runtime.py
git commit -m "feat: add persistent publisher browser runtime"
```

---

### Task 3: Thread runtime ownership through rebuild and report orchestration

**Files:**
- Modify: `src/papertrack/journal_rebuild.py`
- Modify: `src/papertrack/report.py`
- Modify: `src/papertrack/journal_fetch.py`
- Modify: `tests/test_journal_rebuild.py`
- Modify: `tests/test_report.py`

**Interfaces:**
- Consumes: `ProviderRuntime`, `ProviderRunStats`
- Changes: `run_provider_rebuild(..., profile_root, snapshot_root, runtime_factory=ProviderRuntime) -> RebuildSummary`
- Changes: `filter_journal_to_md(..., provider_runtime=None) -> bool`
- Changes: `query_journal_issue_full_result(..., provider_runtime=None) -> AdapterResult`
- Produces: `RebuildSummary.runtime_stats`

- [ ] **Step 1: Write a failing ownership test**

```python
def test_provider_rebuild_owns_one_runtime_for_all_candidates(monkeypatch, tmp_path):
    runtime = FakeRuntime(provider="acs")
    seen = []
    monkeypatch.setattr(
        journal_rebuild,
        "filter_journal_to_md",
        lambda **kwargs: seen.append(kwargs["provider_runtime"]) or False,
    )
    summary = journal_rebuild.run_provider_rebuild(
        "acs",
        two_acs_candidates(),
        data_dir=tmp_path / "data",
        diagnostics_dir=tmp_path / "diagnostics",
        profile_root=tmp_path / "profiles",
        snapshot_root=tmp_path / "snapshots",
        runtime_factory=lambda **kwargs: runtime,
    )
    assert seen == [runtime, runtime]
    assert runtime.enter_count == 1
    assert runtime.exit_count == 1
    assert summary.runtime_stats is runtime.stats
```

Add a propagation test that `report.filter_journal_to_md` passes the identical runtime object into `journal_fetch.query_journal_issue_full_result`.

- [ ] **Step 2: Run the focused orchestration tests and verify signature failures**

Run: `PYTHONPATH=src /home/ansatz/soft/miniconda3/bin/conda run -n arxiv pytest -q tests/test_journal_rebuild.py tests/test_report.py`

Expected: new tests fail because the runtime parameters and summary field do not exist.

- [ ] **Step 3: Add explicit runtime parameters and context ownership**

Extend `RebuildSummary` with:

```python
runtime_stats: ProviderRunStats = field(default_factory=ProviderRunStats)
```

Extend `run_provider_rebuild` with keyword-only `profile_root`, `snapshot_root`, and `runtime_factory`. Open one runtime around the entire candidate loop:

```python
with runtime_factory(
    provider=provider,
    profile_root=profile_root,
    snapshot_root=snapshot_root,
) as provider_runtime:
    for candidate in ordered:
        outcome = _rebuild_candidate(
            candidate,
            data_path,
            progress,
            provider_runtime,
        )
```

Extract the existing per-candidate backup/fetch/assessment block into `_rebuild_candidate` without changing its preservation semantics. Pass `provider_runtime` through `filter_journal_to_md` and `query_journal_issue_full_result`. Provider dispatch must pass it only to ACS, AIP, or APS authoritative adapters.

Remove the three authoritative-provider calls to `_record_crossref_abstract_fill` from `query_journal_issue_full_result`. Keep the legacy CrossRef helpers isolated for compatibility tests, but no ACS, AIP, or APS issue dispatch may invoke them regardless of `SourcePolicy`.

- [ ] **Step 4: Verify exceptional cleanup and report preservation**

Add tests where the first candidate raises `RuntimeError` and where `_assess_report` raises. Assert the runtime exits exactly once, the old report bytes are restored, and progress records `fetch_failed`.

- [ ] **Step 5: Run orchestration tests and commit**

Run: `PYTHONPATH=src /home/ansatz/soft/miniconda3/bin/conda run -n arxiv pytest -q tests/test_journal_rebuild.py tests/test_report.py tests/test_journal.py`

Expected: all selected tests pass.

```bash
git add src/papertrack/journal_rebuild.py src/papertrack/report.py src/papertrack/journal_fetch.py tests/test_journal_rebuild.py tests/test_report.py
git commit -m "refactor: own one browser runtime per publisher run"
```

---

### Task 4: ACS official snapshot and persistent-browser adapter

**Files:**
- Modify: `src/papertrack/acs_fetch.py`
- Delete: `src/papertrack/acs_browser.py`
- Replace: `tests/test_acs_browser_fallback.py`
- Modify: `tests/test_journal.py`

**Interfaces:**
- Consumes: `ProviderRuntime.fetch_issue`, `OfficialSnapshotStore`, `SnapshotValidation`
- Changes: `query_acs_issue_result(..., provider_runtime) -> AdapterResult`

- [ ] **Step 1: Write failing cache-first and live-fetch tests**

```python
def test_acs_uses_valid_snapshot_without_network(runtime, snapshot_store):
    seed_acs_snapshot(snapshot_store, ACS_FIXTURE)
    result = query_acs_issue_result("jcisd8", "66", "12", "JCIM", runtime)
    assert result.outcome == SUCCESS
    assert result.metadata["official_transport"] == "snapshot"
    assert runtime.fetch_calls == []


def test_acs_live_success_commits_validated_snapshot(runtime, snapshot_store):
    runtime.queue_html(ACS_FIXTURE)
    result = query_acs_issue_result("jcisd8", "66", "12", "JCIM", runtime)
    stored = snapshot_store.load(SnapshotKey("acs", "jcisd8", "66", "12"))
    assert result.outcome == SUCCESS
    assert stored is not None
    assert stored.validation.membership_dois == tuple(sorted(result.entries))
```

Also assert that challenge, zero issue items, missing DOI membership, and parser failure never replace an existing valid snapshot.

- [ ] **Step 2: Run ACS tests and verify failure before implementation**

Run: `PYTHONPATH=src /home/ansatz/soft/miniconda3/bin/conda run -n arxiv pytest -q tests/test_acs_browser_fallback.py tests/test_journal.py -k acs`

Expected: tests fail because `query_acs_issue_result` does not accept the runtime and still starts a subprocess.

- [ ] **Step 3: Replace subprocess fallback with runtime and snapshot validation**

Use this exact issue request:

```python
request = IssueRequest(
    canonical_url=ACS_ISSUE_URL.format(
        journal_code=journal_code,
        volume=volume,
        issue=issue,
    ),
    required_selector="div.issue-item.clearfix",
    challenge_markers=("Just a moment", "cf-chl", "Access denied"),
)
```

On snapshot load, parse the body and require the newly extracted DOI membership hash to match the manifest before returning it. On live success, require at least one issue item and at least one valid DOI, commit the raw page, and set metadata keys `official_transport`, `snapshot_path`, `selector_counts`, and `membership_hash`. Record blocked, circuit-open, fetch, and parse outcomes with the existing diagnostic context.

- [ ] **Step 4: Remove obsolete ACS subprocess helper and tests**

Delete `acs_browser.py`, `_fetch_acs_browser`, `subprocess`, `sys`, and `xvfb-run` assumptions. Keep parser tests independent of the browser runtime.

- [ ] **Step 5: Run ACS tests and commit**

Run: `PYTHONPATH=src /home/ansatz/soft/miniconda3/bin/conda run -n arxiv pytest -q tests/test_acs_browser_fallback.py tests/test_journal.py -k acs`

Expected: all ACS-selected tests pass.

```bash
git add src/papertrack/acs_fetch.py tests/test_acs_browser_fallback.py tests/test_journal.py
git rm src/papertrack/acs_browser.py
git commit -m "feat: fetch ACS issues through persistent official runtime"
```

---

### Task 5: APS official snapshot and persistent-browser adapter

**Files:**
- Modify: `src/papertrack/aps_fetch.py`
- Delete: `src/papertrack/aps_browser.py`
- Replace: `tests/test_aps_browser_fallback.py`
- Modify: `tests/test_journal.py`

**Interfaces:**
- Consumes: `ProviderRuntime.fetch_issue`, `OfficialSnapshotStore`, `SnapshotValidation`
- Changes: `query_aps_issue_result(..., provider_runtime) -> AdapterResult`

- [ ] **Step 1: Write failing APS cache/live/challenge tests**

```python
def test_aps_requires_issue_cards_before_snapshot_commit(runtime, snapshot_store):
    runtime.queue_html("<html><title>Physical Review Letters</title></html>")
    result = query_aps_issue_result("prl", "127", "27", "PRL", runtime)
    assert result.outcome == OFFICIAL_EMPTY_SUSPICIOUS
    assert snapshot_store.load(SnapshotKey("aps", "prl", "127", "27")) is None


def test_aps_snapshot_rebuild_preserves_official_doi_case(runtime, snapshot_store):
    seed_aps_snapshot(snapshot_store, APS_FIXTURE)
    result = query_aps_issue_result("prl", "127", "27", "PRL", runtime)
    assert "10.1103/PhysRevLett.127.270401" in result.entries
    assert runtime.fetch_calls == []
```

Assert that the canonical URL is exactly `https://journals.aps.org/prl/issues/127/27` and no requested URL contains `/abstract/` or `/articles/`.

- [ ] **Step 2: Run APS tests and verify runtime-signature failures**

Run: `PYTHONPATH=src /home/ansatz/soft/miniconda3/bin/conda run -n arxiv pytest -q tests/test_aps_browser_fallback.py tests/test_journal.py -k aps`

Expected: new tests fail before the adapter accepts `ProviderRuntime`.

- [ ] **Step 3: Implement runtime and snapshot flow**

Use:

```python
request = IssueRequest(
    canonical_url=APS_ISSUE_URL.format(
        journal_slug=journal_slug,
        volume=volume,
        issue=issue,
    ),
    required_selector=".browse-result-card.issue",
    challenge_markers=("Just a moment", "cf-chl", "Access denied", "Cloudflare"),
)
```

Require a nonzero issue-card count and DOI count before commit. Store the exact DOI strings parsed from the official page, including APS capitalization. Reparse and verify the membership hash on cache load. Return `CIRCUIT_OPEN` as a non-success adapter outcome with complete diagnostic fields.

- [ ] **Step 4: Remove obsolete APS subprocess helper and run tests**

Delete `aps_browser.py`, `_fetch_aps_browser`, and its subprocess imports.

Run: `PYTHONPATH=src /home/ansatz/soft/miniconda3/bin/conda run -n arxiv pytest -q tests/test_aps_browser_fallback.py tests/test_aps_loi.py tests/test_journal.py -k aps`

Expected: all APS-selected tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/papertrack/aps_fetch.py tests/test_aps_browser_fallback.py tests/test_journal.py
git rm src/papertrack/aps_browser.py
git commit -m "feat: fetch APS issues through persistent official runtime"
```

---

### Task 6: AIP issue and abstract requests in one persistent official session

**Files:**
- Modify: `src/papertrack/aip_fetch.py`
- Delete: `src/papertrack/aip_browser.py`
- Modify: `tests/test_aip_fetch.py`
- Modify: `tests/test_aip_browser.py`

**Interfaces:**
- Consumes: `ProviderRuntime.fetch_issue`, `ProviderRuntime.request_same_origin`, `OfficialSnapshotStore`
- Changes: `query_aip_issue_result(..., provider_runtime) -> AdapterResult`

- [ ] **Step 1: Write failing tests for one-session issue and abstract collection**

```python
def test_aip_fetches_issue_and_abstracts_in_same_runtime(runtime):
    runtime.queue_issue_html(AIP_ISSUE_FIXTURE)
    runtime.queue_json(
        "https://pubs.aip.org/Handlers/ArticleAbstractHandler.ashx",
        {"Success": True, "Html": "<p>Official abstract.</p>"},
    )
    result = query_aip_issue_result("jcp", "160", "4", "JCP", runtime)
    assert result.outcome == SUCCESS
    assert next(iter(result.entries.values())).abstract == "Official abstract."
    assert runtime.context_count == 1
    assert all(call.same_origin for call in runtime.request_calls)
```

Add tests for endpoint 403, invalid JSON, `Success: false`, absent official abstract control, and snapshot reload. Each outcome must remain distinguishable and must not call CrossRef.

- [ ] **Step 2: Run AIP tests and verify failures before implementation**

Run: `PYTHONPATH=src /home/ansatz/soft/miniconda3/bin/conda run -n arxiv pytest -q tests/test_aip_fetch.py tests/test_aip_browser.py`

Expected: new tests fail because AIP still starts a separate browser subprocess and direct requests.

- [ ] **Step 3: Fetch the AIP issue page through the provider runtime**

Use:

```python
request = IssueRequest(
    canonical_url=AIP_ISSUE_URL.format(
        journal_slug=journal_slug,
        volume=volume,
        issue=issue,
    ),
    required_selector=".al-article-items",
    challenge_markers=("Just a moment", "cf-chl", "Access denied", "Cloudflare"),
    capture_url_patterns=(re.compile(r"/Handlers/ArticleAbstractHandler\\.ashx"),),
)
```

Parse official `data-articleid` controls from the validated issue page. For IDs not already satisfied by a captured response, call `runtime.request_same_origin` against `aip_abstract_url(journal_slug)` using the existing official form fields. Retain the 10-second abstract-request interval inside the runtime.

- [ ] **Step 4: Commit a snapshot only after issue membership validates**

Commit the official issue page plus captured official abstract responses as `SnapshotAttachment` records. Name each attachment `abstract-{article_id}.json` after validating the article ID as a safe component. On cache load, verify every referenced file hash before using it. If an attachment is corrupt, `OfficialSnapshotStore.load` rejects that snapshot, the adapter fetches the official issue again, and the previous Markdown remains unchanged if refetching fails.

- [ ] **Step 5: Remove obsolete AIP helper, run tests, and commit**

Delete `aip_browser.py` and `_fetch_aip_browser_bundle`; retain the existing abstract parsing and diagnostic records inside `aip_fetch.py`.

Run: `PYTHONPATH=src /home/ansatz/soft/miniconda3/bin/conda run -n arxiv pytest -q tests/test_aip_fetch.py tests/test_aip_browser.py tests/test_journal.py -k 'aip or official_only'`

Expected: all AIP-selected tests pass.

```bash
git add src/papertrack/aip_fetch.py tests/test_aip_fetch.py tests/test_aip_browser.py tests/test_journal.py
git rm src/papertrack/aip_browser.py
git commit -m "feat: reuse persistent official session for AIP issues"
```

---

### Task 7: Official-only worker configuration and success-rate reporting

**Files:**
- Modify: `run_j.sh`
- Modify: `scripts/rebuild_acs_candidates.py`
- Modify: `scripts/rebuild_aip_candidates.py`
- Modify: `scripts/rebuild_aps_candidates.py`
- Modify: `src/papertrack/journal_rebuild.py`
- Modify: `tests/test_config.py`
- Modify: `tests/test_journal_rebuild_workers.py`
- Modify: `tests/test_journal_rebuild.py`
- Modify: `tests/test_run_j_wrapper.py`

**Interfaces:**
- Consumes: `RebuildSummary.runtime_stats`
- Produces: `RebuildSummary.official_fetch_rate` and `complete_report_rate`
- Adds worker arguments: `--profile-root` and `--snapshot-root`

- [ ] **Step 1: Write failing policy and rate tests**

```python
def test_run_j_is_official_only():
    content = Path("run_j.sh").read_text(encoding="utf-8")
    assert "export PAPERTRACK_CROSSREF_MODE=disabled" in content
    assert "PAPERTRACK_CROSSREF_MODE=metadata" not in content


def test_rebuild_summary_reports_separate_rates():
    summary = RebuildSummary(
        provider="acs",
        ordered_candidate_keys=("a", "b", "c", "d"),
        attempted_keys=("a", "b", "c", "d"),
        skipped_keys=(),
        outcomes={"a": RESOLVED, "b": OFFICIAL_SUCCESS_UNRESOLVED,
                  "c": FETCH_FAILED, "d": FETCH_FAILED},
        runtime_stats=ProviderRunStats(live_success=2, blocked=2),
    )
    assert summary.official_fetch_rate == 0.5
    assert summary.complete_report_rate == 0.25
```

- [ ] **Step 2: Run worker/configuration tests and verify failures**

Run: `PYTHONPATH=src /home/ansatz/soft/miniconda3/bin/conda run -n arxiv pytest -q tests/test_config.py tests/test_journal_rebuild_workers.py tests/test_journal_rebuild.py tests/test_run_j_wrapper.py`

Expected: tests fail because `run_j.sh` still enables CrossRef metadata and rates are absent.

- [ ] **Step 3: Change production policy and pass exact runtime roots**

Set these values in `run_j.sh`:

```bash
export PAPERTRACK_CROSSREF_MODE=disabled
PROFILE_ROOT="${PROJECT_DIR}/.papertrack_browser_profiles"
SNAPSHOT_ROOT="${PROJECT_DIR}/.papertrack_official_cache"
```

Add a preflight requiring `/usr/bin/google-chrome-stable` and `xvfb-run`. Start each worker command under its own `xvfb-run -a` display before `conda run`, then pass both paths to every rebuild worker. Add required `Path` arguments to all three scripts and forward them into `run_provider_rebuild`. The existing process-group supervisor must still own Xvfb, Chrome, conda, and the Python worker as one cleanup tree.

Extend `test_run_j_wrapper.py` to assert that TERM and INT sent to `run_j.sh` terminate the owned Xvfb/Chrome worker group and leave no owned child process, while an unrelated process remains untouched.

- [ ] **Step 4: Implement denominator-safe rates and structured summaries**

Add:

```python
@property
def official_fetch_rate(self) -> float:
    denominator = len(self.attempted_keys)
    if denominator == 0:
        return 0.0
    fetched = self.runtime_stats.live_success + self.runtime_stats.snapshot_hit
    return fetched / denominator

@property
def complete_report_rate(self) -> float:
    denominator = len(self.attempted_keys)
    if denominator == 0:
        return 0.0
    return len(self.resolved_keys) / denominator
```

Each worker prints rates with four decimal places plus `snapshot_hit`, `live_success`, `blocked`, `fetch_failed`, and `circuit_skipped`. Skipped progress records are excluded from denominators.

- [ ] **Step 5: Run shell and worker tests, then commit**

Run: `bash -n run_j.sh`

Run: `PYTHONPATH=src /home/ansatz/soft/miniconda3/bin/conda run -n arxiv pytest -q tests/test_config.py tests/test_journal_rebuild_workers.py tests/test_journal_rebuild.py tests/test_run_j_wrapper.py`

Expected: syntax check and all selected tests pass.

```bash
git add run_j.sh scripts/rebuild_acs_candidates.py scripts/rebuild_aip_candidates.py scripts/rebuild_aps_candidates.py src/papertrack/journal_rebuild.py tests/test_config.py tests/test_journal_rebuild_workers.py tests/test_journal_rebuild.py tests/test_run_j_wrapper.py
git commit -m "feat: report official journal fetch success rates"
```

---

### Task 8: End-to-end failure preservation and opt-in live verification

**Files:**
- Create: `scripts/verify_official_issue_runtime.py`
- Create: `tests/test_official_issue_integration.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: production `ProviderRuntime`, authoritative adapters, snapshot store, and report preservation
- Produces: read-only-by-default live verifier with `--provider`, `--journal`, `--volume`, `--issue`, `--profile-root`, `--snapshot-root`, and `--diagnostics-dir`

- [ ] **Step 1: Write a failing integration test for the complete state chain**

```python
def test_blocked_live_fetch_preserves_snapshot_report_and_progress(tmp_path):
    report = seed_valid_report(tmp_path)
    snapshot = seed_valid_snapshot(tmp_path)
    before_report = report.read_bytes()
    before_snapshot = snapshot.body_path.read_bytes()
    runtime = FakeRuntime(outcomes=[BLOCKED, BLOCKED, BLOCKED])
    summary = run_rebuild_with_runtime(tmp_path, runtime)
    assert report.read_bytes() == before_report
    assert snapshot.body_path.read_bytes() == before_snapshot
    assert summary.runtime_stats.blocked == 3
    assert summary.runtime_stats.circuit_skipped > 0
    assert load_progress(tmp_path).issues[failed_key].outcome == FETCH_FAILED
```

Add one success path asserting snapshot commit occurs before Markdown replacement and user-completed checkboxes survive the new rendering.

- [ ] **Step 2: Run the integration test and verify failure before final wiring**

Run: `PYTHONPATH=src /home/ansatz/soft/miniconda3/bin/conda run -n arxiv pytest -q tests/test_official_issue_integration.py`

Expected: tests fail until the final state ordering and verifier entry point exist.

- [ ] **Step 3: Implement a bounded live verifier**

The verifier accepts exactly one issue, defaults to no Markdown write, uses official-only policy, and prints one JSON object containing provider, issue key, transport, outcome, selector counts, membership count, cache commit status, and diagnostic path. Add `--write-report` as an explicit opt-in flag. It must reject more than one issue per invocation.

- [ ] **Step 4: Document production and verification commands**

Add these commands to `README.md`:

```bash
./run_j.sh --retry-unresolved

/home/ansatz/soft/miniconda3/bin/conda run -n arxiv \
  python scripts/verify_official_issue_runtime.py \
  --provider acs --journal jcim --volume 66 --issue 12 \
  --profile-root .papertrack_browser_profiles \
  --snapshot-root .papertrack_official_cache \
  --diagnostics-dir .papertrack_diagnostics
```

Document that the verifier contacts only the canonical official issue, does not write Markdown unless requested, and may legitimately return `blocked`.

- [ ] **Step 5: Run the full deterministic verification suite**

Run: `bash -n run.sh run_j.sh`

Run: `PYTHONPATH=src /home/ansatz/soft/miniconda3/bin/conda run -n arxiv python -m compileall -q src scripts`

Run: `PYTHONPATH=src /home/ansatz/soft/miniconda3/bin/conda run -n arxiv pytest -q`

Expected: shell syntax and compile checks succeed; all pytest tests pass without network access.

- [ ] **Step 6: Commit the integration boundary**

```bash
git add README.md scripts/verify_official_issue_runtime.py tests/test_official_issue_integration.py
git commit -m "test: verify unattended official issue state chain"
```

---

## Execution Checkpoints

After Tasks 1-3, review snapshot atomicity, explicit runtime ownership, process cleanup, and exception preservation before changing any provider adapter.

After each of Tasks 4-6, run that provider's focused suite and obtain a fresh review before starting the next provider. Do not use a successful provider implementation as evidence that another publisher's selectors or endpoints are correct.

After Task 8, run the live verifier once per publisher, sequentially, before running all candidate issues. Inspect diagnostics and success-rate output after each single issue. A live `blocked` result is evidence about publisher behavior, not a reason to weaken validation or enable CrossRef.

# arXiv Category Membership Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore primary-plus-cross-list membership for `chem-ph`, `cs.AI`, and `cs.LG`, then enforce exact official subject membership before report generation.

**Architecture:** Advanced search supplies a high-recall candidate set. `query_arxiv_result(..., required_category=...)` owns exact membership filtering and pagination accounting, while the report layer supplies the ledger category. A read-only audit command compares durable Markdown IDs with official accepted IDs without rewriting reports.

**Tech Stack:** Python 3.10, TOML, BeautifulSoup/feedparser, pytest, argparse, official arXiv advanced search.

## Global Constraints

- Existing Markdown is durable state; fetch or parse failure must not overwrite it.
- Production membership comes only from official arXiv subject tags.
- Primary and cross-listed papers both belong to the category ledger.
- Automated tests use mocked network results.
- Historical audit is read-only and rate-limited.

---

### Task 1: Restore High-Recall Query Configuration

**Files:**
- Modify: `src/papertrack/categories.toml`
- Modify: `src/papertrack/codex.py`
- Modify: `tests/test_config.py`

**Interfaces:**
- Consumes: `get_query_args() -> dict[str, dict[str, str]]`
- Produces: matching TOML and fallback query dictionaries for all eight categories.

- [ ] **Step 1: Write failing semantic-shape tests**

Assert that `chem-ph`, `cs.AI`, and `cs.LG` have an empty first title term plus a second exact category term with `field="all"`, and contain no `cross_list_category`. Assert the five archive configurations remain archive-backed.

- [ ] **Step 2: Verify RED**

Run `PYTHONPATH=src /home/ansatz/soft/miniconda3/bin/conda run -n arxiv pytest -q tests/test_config.py`.

Expected: failures showing the three configs still use `cross_list_category`.

- [ ] **Step 3: Implement the proven query form**

For each affected TOML and fallback dictionary, produce:

```python
{
    "terms-0-term": "",
    "terms-0-field": "title",
    "terms-1-operator": "AND",
    "terms-1-term": category,
    "terms-1-field": "all",
}
```

- [ ] **Step 4: Verify GREEN and commit**

Run the Task 1 test command, then commit configuration and tests.

---

### Task 2: Enforce Exact Subject Membership In The Adapter

**Files:**
- Modify: `src/papertrack/arxiv_index_fetch.py`
- Modify: `tests/test_adapter_result.py`

**Interfaces:**
- Consumes: `query_arxiv_result(date_from_date, date_to_date, query_args, required_category=None)`
- Produces: `AdapterResult` containing only entries whose `subjects` include `required_category`, plus candidate/accepted/rejected counts in metadata.

- [ ] **Step 1: Write failing membership tests**

Use mocked pages containing a primary member, a cross-listed member, and a false positive. Assert primary and cross-listed entries remain, the false positive is removed, and metadata counts are `3/2/1`.

- [ ] **Step 2: Write the failing pagination test**

Set `size=1`; make page one a false positive and page two a valid member. Assert both pages are fetched and the valid member is returned. This proves pagination uses raw candidate count.

- [ ] **Step 3: Write the failing missing-subject test**

Return an otherwise valid entry with no subject tags while `required_category` is set. Assert `outcome == PARSE_FAILED`, not successful empty data.

- [ ] **Step 4: Verify RED**

Run the three exact tests and confirm failures arise from absent membership behavior.

- [ ] **Step 5: Implement minimal filtering**

Add the optional argument, validate non-empty subjects in membership mode, filter after parsing but before aggregation, use unfiltered page length for pagination, and aggregate diagnostic counts.

- [ ] **Step 6: Verify GREEN and commit**

Run `tests/test_adapter_result.py` and `tests/test_report.py`, then commit adapter and tests.

---

### Task 3: Propagate Ledger Category From The Report Layer

**Files:**
- Modify: `src/papertrack/report.py`
- Modify: `tests/test_report.py`

**Interfaces:**
- Consumes: `_filter_one_arxiv_day_to_md(..., category: str, ...)`
- Produces: every production arXiv fetch calls `query_arxiv_result(..., required_category=category)`.

- [ ] **Step 1: Update the existing exact-day mock test to require the keyword**

The fake query captures `required_category`; assert it equals the requested ledger category.

- [ ] **Step 2: Verify RED**

Run the exact test and confirm the captured value is missing.

- [ ] **Step 3: Pass the category explicitly**

Change the adapter call only; do not alter refresh/backfill calendar behavior.

- [ ] **Step 4: Verify GREEN and commit**

Run report and CLI tests, then commit.

---

### Task 4: Read-Only Historical Membership Audit

**Files:**
- Create: `scripts/audit_arxiv_category_membership.py`
- Create: `tests/test_arxiv_category_audit.py`

**Interfaces:**
- Consumes: data directory, categories, date range, request interval, official `query_arxiv_result`.
- Produces: JSON manifest with `exact`, `missing_official`, `non_member_local`, and `fetch_failed` day records; never writes Markdown.

- [ ] **Step 1: Write failing classification and no-write tests**

Mock official results for exact, missing, extra, and failed days. Assert deterministic JSON records and assert report bytes remain unchanged.

- [ ] **Step 2: Verify RED**

Run `tests/test_arxiv_category_audit.py`; expect import failure because the script does not exist.

- [ ] **Step 3: Implement the audit command**

Parse local heading IDs, fetch exact official category membership, classify set differences, print or atomically write only the requested JSON manifest, and honor the configured interval between official requests.

- [ ] **Step 4: Verify GREEN and commit**

Run the audit tests, then commit the script and tests.

---

### Task 5: Final Verification And Review

**Files:**
- Verify all modified files.

- [ ] **Step 1: Run complete verification**

Run `PYTHONPATH=src /home/ansatz/soft/miniconda3/bin/conda run -n arxiv pytest -q` and `git diff --check`.

Expected: all tests pass and no whitespace errors.

- [ ] **Step 2: Run a bounded official smoke check**

Use `chem-ph 2026-02-16`, `cs.AI 2026-07-15`, and `cs.LG 2026-07-15`; verify both primary and cross-listed membership with a 10-second request interval. Do not write Markdown.

- [ ] **Step 3: Request independent code review**

Review configuration parity, membership semantics, pagination, failure preservation, and audit no-write behavior. Resolve all Critical and Important findings before integration.

- [ ] **Step 4: Finish the branch**

Use the previously selected local-merge workflow, rerun the complete suite on `main`, then remove the owned worktree.

# Dataview Tag Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generate valid Obsidian page tags and parseable Dataview sources, then safely migrate every existing PaperTrack Markdown report without changing reading state.

**Architecture:** A focused `report_tags` module owns canonical generated tags. Rendering and migration both use it; current v2 reports are repaired in place through the existing validated backup/write path, while ambiguous generated regions fail closed.

**Tech Stack:** Python 3.10, Jinja2, regular expressions, pytest, Obsidian YAML properties, Dataview DQL.

## Global Constraints

- `papertrack_category` remains the exact ledger identity such as `cs.AI`.
- Canonical arXiv tags lowercase categories and replace `.` with `-`.
- YAML tag values omit `#`; Dataview `FROM` sources retain `#`.
- `WHERE completed` remains unchanged.
- Preserve task checkboxes, completion suffixes, anchors, paper metadata, unrelated user tags, and user prose.
- Ambiguous generated tag/query regions must leave the file byte-for-byte unchanged.
- Retain `arxiv-v2` and `journal-v2`; this repairs invalid serialization rather than changing schema.

---

### Task 1: Canonical Tag Helpers And New Report Rendering

**Files:**
- Create: `src/papertrack/report_tags.py`
- Modify: `src/papertrack/report.py`
- Modify: `src/papertrack/templates/report.md.j2`
- Modify: `src/papertrack/templates/journal_report.md.j2`
- Modify: `tests/test_report.py`

**Interfaces:**
- Produces: `arxiv_report_tag(category: str, report_date: str) -> str`
- Produces: `journal_report_tag(journal_slug: str, volume: str, issue: str) -> str`
- Rendering supplies `report_tag` to each report template.

- [ ] **Step 1: Write failing rendering tests**

Assert exact frontmatter and DQL forms:

```python
assert "tags:\n  - cs-ai-2026-07-07" in rendered
assert "FROM #cs-ai-2026-07-07" in rendered
assert "WHERE completed" in rendered
assert "#cs.AI-2026-07-07" not in rendered
```

Also assert `quant-ph` remains `quant-ph`, and a journal report contains
`tags:\n  - jcp-v164-i16` plus `FROM #jcp-v164-i16`.

- [ ] **Step 2: Verify RED**

Run:

```bash
PYTHONPATH=src /home/ansatz/soft/miniconda3/bin/conda run -n arxiv pytest -q \
  tests/test_report.py -k 'markdown_structure or dataview_tag'
```

Expected: generated YAML still contains `- #...`, and `cs.AI` remains dotted.

- [ ] **Step 3: Implement canonical helpers and render inputs**

Create:

```python
def _tag_component(value: str) -> str:
    return value.lower().replace(".", "-")


def arxiv_report_tag(category: str, report_date: str) -> str:
    return f"{_tag_component(category)}-{report_date}"


def journal_report_tag(journal_slug: str, volume: str, issue: str) -> str:
    return f"{_tag_component(journal_slug)}-v{volume}-i{issue}"
```

Pass the result as `report_tag` from `_render_report` and
`_render_journal_report`. Templates render:

```jinja2
tags:
  - {{ report_tag }}
```

and:

```jinja2
FROM #{{ report_tag }}
```

- [ ] **Step 4: Verify GREEN and commit**

Run all `tests/test_report.py`, then commit helper, templates, report code, and tests:

```bash
git commit -m "fix: generate valid Dataview report tags"
```

---

### Task 2: Safe Legacy And Current Report Migration

**Files:**
- Modify: `src/papertrack/migration.py`
- Modify: `tests/test_migration.py`

**Interfaces:**
- Consumes: canonical helper functions from Task 1.
- Produces: current arXiv and journal migration repairs canonical YAML/DQL tags.

- [ ] **Step 1: Write failing arXiv migration tests**

Use a current `arxiv-v2` file at `arxiv/cs.AI/2026/07/07.md` with:

```yaml
tags:
  - personal
  - #cs.AI-2026-07-07
```

and a generated task query using `#cs.AI-2026-07-07`. Assert migration returns
`repaired`, produces `cs-ai-2026-07-07` and `FROM #cs-ai-2026-07-07`, retains
`personal`, and preserves both `[x] ... ✅ date` and `[ ]` paper tasks exactly.
Run migration twice and assert the second result is `current` with identical
bytes.

- [ ] **Step 2: Verify arXiv RED**

Run the new exact tests. Expected: dotted tag remains and/or the YAML value still begins with `#`.

- [ ] **Step 3: Implement the arXiv repair**

Change legacy frontmatter to use `arxiv_report_tag`. Refactor current identity
repair so it distinguishes:

```python
canonical_tag = arxiv_report_tag(category, report_date)
canonical_source = f"#{canonical_tag}"
legacy_sources = {
    f"#{old_category}-{old_date}",
    f"#{category}-{report_date}",
}
```

Replace only the generated identity tag line and generated task-query source.
Continue using path identity as authoritative. Preserve `WHERE completed` or
legacy `WHERE completion` exactly.

- [ ] **Step 4: Write failing journal migration tests**

Create current and legacy journal reports with `- #jcp-v164-i16`,
`FROM #jcp-v164-i16`, one unrelated user tag, and completed/incomplete DOI
tasks. Assert canonical YAML is `- jcp-v164-i16`, the DQL source remains
`#jcp-v164-i16`, and all unrelated lines are byte-identical.

- [ ] **Step 5: Verify journal RED**

Run the new exact tests. Expected: current journal migration returns `current`
without fixing YAML.

- [ ] **Step 6: Implement current and legacy journal repair**

For current reports, read `papertrack_journal`, `papertrack_volume`, and
`papertrack_issue` from frontmatter, derive the canonical tag, repair the
generated tag/query, then remove obsolete Markdown images if present. If
identity fields or generated regions are ambiguous, raise `ValueError` before
writing and return `failed` with the original file unchanged.

- [ ] **Step 7: Add ambiguity and idempotence tests**

Assert two generated candidate tag lines or two task-query blocks return
`failed`, preserve original bytes, and leave no `.bak`. Assert canonical files
return `current` unchanged.

- [ ] **Step 8: Verify GREEN and commit**

Run:

```bash
PYTHONPATH=src /home/ansatz/soft/miniconda3/bin/conda run -n arxiv pytest -q \
  tests/test_migration.py tests/test_report.py
```

Commit:

```bash
git commit -m "fix: migrate generated Dataview tags safely"
```

---

### Task 3: Validation, Review, And Code Integration

**Files:**
- Modify: `src/papertrack/migration.py`
- Modify: `tests/test_migration.py`

**Interfaces:**
- Produces: validation rejects newly generated reports with invalid generated YAML or dotted arXiv DQL tags.

- [ ] **Step 1: Write failing validation tests**

Assert generated-looking v2 reports fail validation when their canonical tag
line begins with `#`, or when an arXiv generated Dataview source contains
`#cs.AI-...`; valid canonical reports pass. Do not reject unrelated user tags
or user Dataview blocks.

- [ ] **Step 2: Verify RED**

Run the exact validation tests. Expected: invalid generated tags currently pass
because validation checks only skeleton and paper headings.

- [ ] **Step 3: Implement scoped validation**

Add `_generated_tag_layout_is_valid(content: str, source: str) -> bool`. It
extracts the canonical identity from current frontmatter, computes the expected
tag with Task 1 helpers, and requires exactly one generated YAML item and one
generated task-query source:

```python
yaml_pattern = rf"^  - {re.escape(expected_tag)}[ \t]*$"
source_pattern = rf"^(?:FROM|from|TASK FROM) #{re.escape(expected_tag)}[ \t]*$"
```

Return false for `  - #<expected>`, dotted arXiv sources, missing generated
identity, or ambiguous generated task blocks. Search only the frontmatter and
the task Dataview block so unrelated user tags/queries remain allowed. Call the
helper from the current arXiv and journal branches of `validate_report_content`.

- [ ] **Step 4: Verify GREEN**

Run `tests/test_migration.py` and `tests/test_report.py`; expected: all pass.

- [ ] **Step 5: Run complete verification**

```bash
PYTHONPATH=src /home/ansatz/soft/miniconda3/bin/conda run -n arxiv pytest -q
git diff --check
```

- [ ] **Step 6: Request independent code review**

Review canonicalization, collisions, user-content preservation, current/legacy
journal coverage, ambiguity handling, and validation scope. Resolve every
Critical and Important issue.

- [ ] **Step 7: Commit review fixes, locally merge to main, and rerun the full suite**

Use a fast-forward local merge only after the isolated branch is clean and all
tests pass.

---

### Task 4: Bulk Durable Markdown Migration And Audit

**Files:**
- Modify outside Git: `/home/ansatz/data/obsidian/1/papertrack_datas/**/*.md`
- Create diagnostic snapshots under: `/home/ansatz/data/code/arxiv_reading/.papertrack_diagnostics/`

**Interfaces:**
- Consumes: merged `main.py migrate --data-dir ...` and canonical migration logic.
- Produces: repaired local Markdown plus before/after audit manifests; no network requests.

- [ ] **Step 1: Capture read-only preflight evidence**

Record, by file, the SHA-256 of content after excluding only the generated tag
line and generated Dataview source, plus paper heading IDs and exact task lines.
Write the manifest under `.papertrack_diagnostics`, never inside the data tree.
Count Markdown files, invalid YAML tag lines, dotted DQL sources, completed
tasks, incomplete tasks, and existing `.bak`/`.tmp` files.

- [ ] **Step 2: Run the local-only migration**

```bash
/home/ansatz/soft/miniconda3/bin/conda run --no-capture-output -n arxiv \
  python main.py migrate \
  --data-dir /home/ansatz/data/obsidian/1/papertrack_datas
```

This command performs no publisher or arXiv fetches.

- [ ] **Step 3: Audit post-migration invariants**

Require all of the following:

- migration exits zero with zero `failed` results;
- generated YAML tag lines beginning with `#`: zero;
- dotted `cs.AI`/`cs.LG` generated DQL sources: zero;
- missing expected canonical report tags or sources: zero;
- `WHERE completed` count unchanged;
- per-file paper heading IDs and exact task lines unchanged;
- normalized-content SHA-256 matches the preflight manifest;
- newly left `.bak` or `.tmp` files: zero.

- [ ] **Step 4: Report operational results**

Report totals by source and migration status, the exact number of changed files,
the preservation checks, and diagnostic manifest paths. Do not commit Markdown
data or diagnostic JSON/cache files.

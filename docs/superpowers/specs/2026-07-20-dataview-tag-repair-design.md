# Dataview Tag Repair Design

## Goal

Make every generated PaperTrack Dataview task query parse and select its own
report page while preserving the existing completed-task view and all durable
user reading state.

## Root Causes

Generated frontmatter currently serializes tags as unquoted values beginning
with `#`. YAML treats that text as a comment, so the list contains a null value
instead of an Obsidian tag. In addition, arXiv ledger categories such as
`cs.AI` and `cs.LG` are copied directly into Dataview tag sources even though
Obsidian tags do not permit a dot.

`WHERE completed` is intentional and remains unchanged. It is a boolean filter
for completed tasks, not the cause of the parser failure.

## Canonical Generated Tags

PaperTrack will distinguish stable ledger identity from presentation tags:

- `papertrack_category` keeps the exact ledger category, such as `cs.AI`.
- The generated arXiv tag lowercases the category and replaces each dot with a
  hyphen: `cs.AI` becomes `cs-ai`; `quant-ph` and `chem-ph` remain unchanged.
- The date suffix remains unchanged.
- Journal slugs already use valid characters and remain unchanged.
- YAML tag values never include a leading `#`.
- Dataview `FROM` sources include the required leading `#`.

For example:

```yaml
tags:
  - cs-ai-2026-07-07
```

```dataview
TASK
FROM #cs-ai-2026-07-07
WHERE completed
```

## Code Boundaries

A small shared tag helper owns canonical arXiv and journal report tag strings.
Templates and migration code call that helper instead of independently
constructing tags. Metadata identity fields and filesystem paths are not
normalized.

The arXiv and journal templates emit the canonical YAML and Dataview forms.
The current `arxiv-v2` and `journal-v2` identifiers remain unchanged because
this is a repair of invalid serialization, not a report-schema redesign.

## Local Migration

Migration handles both legacy and current reports and is idempotent. It may
change only:

1. the one generated tag list item associated with the report identity; and
2. the generated task query's `FROM` tag.

It preserves unrelated user tags, task checkboxes, completion dates, paper
anchors, abstracts, headings, and arbitrary user edits. A file with ambiguous
generated tag identity or multiple candidate task-query blocks fails closed
and remains byte-for-byte unchanged. Successful writes use the existing
validated atomic-write and backup behavior.

Current reports are repaired even though their `papertrack_format` remains v2.
Legacy reports receive the canonical tag when upgraded to v2.

## Verification

Automated tests cover:

- rendering valid arXiv tags for `cs.AI`, `cs.LG`, and hyphenated categories;
- rendering valid journal tags;
- migrating legacy and current reports;
- preserving completed and incomplete task lines plus unrelated user tags;
- idempotence and fail-closed ambiguous input;
- validation that generated YAML tag items omit `#` and Dataview sources contain
  no dot.

Before the bulk migration, record Markdown file bytes and task-state counts.
After migration, verify:

- no generated frontmatter tag line starts with `#`;
- no generated arXiv Dataview source contains a dot;
- every report retains its expected generated tag and `WHERE completed`;
- paper heading IDs and checkbox states are unchanged;
- no backup or temporary files remain after successful migration;
- the complete test suite passes.


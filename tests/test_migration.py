import os
import select
import signal
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from papertrack.reports.migration import (
    CURRENT_JOURNAL_FORMAT,
    classify_report_file,
    has_journal_quality_failure,
    migrate_report_file,
    migrate_reports,
    preserve_task_state,
    validate_report_content,
)


def _current_journal_report(title: str) -> str:
    return f"""---
papertrack_format: journal-v2
papertrack_source: journal
papertrack_provider: aip
papertrack_journal: jcp
papertrack_volume: "164"
papertrack_issue: "16"
tags:
  - jcp-v164-i16
---

```dataview
TASK
```

## collected

## not collected

### 10.1063/example

Title:  {title}
"""


def test_prl_doi_casing_does_not_trigger_quality_failure():
    # PubMed (an official fallback tier) exposes lowercase PRL DOIs; they are
    # canonical (DOI matching is case-insensitive) and must not be treated as
    # quality failures.  Malformed/truncated PRL DOIs are guarded at the
    # capture validation layer instead.
    official = """### 10.1103/PhysRevLett.127.270401

Title:  Quantum Vacuum Sagnac Effect
"""
    pubmed_cased = """### 10.1103/physrevlett.127.270401

Title:  Quantum Vacuum Sagnac Effect
"""

    assert has_journal_quality_failure(official) is False
    assert has_journal_quality_failure(pubmed_cased) is False


def _legacy_journal_report() -> str:
    return """# Journal of Chemical Physics Vol. 164, Iss. 16 — 2026

---
tags:
  - #jcp-v164-i16
---

```dataview
TASK
from #jcp-v164-i16

WHERE completed

```

## collected

## not collected

### 10.1063/example

Links:

- [x] [10.1063/example](https://doi.org/10.1063/example) ✅ 2026-06-09

Title:  Test article

![|700|700x24](https://example.test/toc.gif)
Authors:  A Author

Abstract:
> [!quote]- Abstract
> Dense metadata matters.
"""


@pytest.mark.parametrize('relative', ['aip/jcp/164/16.md', 'aip/jcp/164/16/report.md'])
def test_legacy_journal_migration_removes_toc_image_and_preserves_task(tmp_path, relative):
    report_file = tmp_path / relative
    report_file.parent.mkdir(parents=True)
    report_file.write_text(_legacy_journal_report(), encoding="utf-8")

    result = migrate_report_file(str(report_file), data_dir=str(tmp_path))

    migrated = report_file.read_text(encoding="utf-8")
    assert result.status == "migrated"
    assert result.to_format == CURRENT_JOURNAL_FORMAT
    assert migrated.startswith("---\npapertrack_format: journal-v2")
    assert "papertrack_provider: aip" in migrated
    assert "![|700|700x24]" not in migrated
    assert "- [x] [10.1063/example](https://doi.org/10.1063/example) ✅ 2026-06-09" in migrated
    assert validate_report_content(migrated, source="journal") is True
    assert not report_file.with_suffix(".md.bak").exists()


def test_legacy_journal_migration_preserves_user_frontmatter(tmp_path):
    report_file = tmp_path / "aip" / "jcp" / "164" / "16.md"
    report_file.parent.mkdir(parents=True)
    original = _legacy_journal_report().replace(
        "tags:\n  - #jcp-v164-i16",
        "tags:\n  - personal\n  - #jcp-v164-i16\naliases:\n  - weekly reading\nrating: 5\npapertrack_user_note: keep me",
    )
    report_file.write_text(original, encoding="utf-8")

    result = migrate_report_file(str(report_file), data_dir=str(tmp_path))

    migrated = report_file.read_text(encoding="utf-8")
    assert result.status == "migrated"
    assert "tags:\n  - personal\n  - jcp-v164-i16" in migrated
    assert "aliases:\n  - weekly reading" in migrated
    assert "rating: 5" in migrated
    assert "papertrack_user_note: keep me" in migrated


def test_legacy_arxiv_migration_preserves_user_frontmatter(tmp_path):
    report_file = tmp_path / "arxiv" / "cs.AI" / "2026" / "07" / "07.md"
    report_file.parent.mkdir(parents=True)
    original = """# 2026-07-07 preprint by arxiv_tools

---
tags:
  - personal
  - #cs.AI-2026-07-07
aliases:
  - ai reading
review_status: queued
---

```dataview
TASK
from #cs.AI-2026-07-07
WHERE completed
```

## collected

## not collected

### arXiv:2607.00001

- [ ] [arXiv:2607.00001](https://arxiv.org/abs/2607.00001)
"""
    report_file.write_text(original, encoding="utf-8")

    result = migrate_report_file(str(report_file), data_dir=str(tmp_path))

    migrated = report_file.read_text(encoding="utf-8")
    assert result.status == "migrated"
    assert "tags:\n  - personal\n  - cs-ai-2026-07-07" in migrated
    assert "aliases:\n  - ai reading" in migrated
    assert "review_status: queued" in migrated


def test_legacy_arxiv_path_identity_cannot_be_overridden_by_personal_tag(tmp_path):
    report_file = tmp_path / "arxiv" / "cs.AI" / "2026" / "07" / "07.md"
    report_file.parent.mkdir(parents=True)
    original = """# 2026-07-07 preprint by arxiv_tools

---
tags:
  - #personal-2026-07-07
  - #cs.AI-2026-07-07
---

```dataview
TASK
from #cs.AI-2026-07-07
```

## collected

## not collected

### arXiv:2607.00001
"""
    report_file.write_text(original, encoding="utf-8")

    result = migrate_report_file(str(report_file), data_dir=str(tmp_path))

    migrated = report_file.read_text(encoding="utf-8")
    assert result.status == "migrated"
    assert "papertrack_category: cs.AI" in migrated
    assert "tags:\n  - #personal-2026-07-07\n  - cs-ai-2026-07-07" in migrated


def test_legacy_arxiv_without_stable_path_fails_on_multiple_identity_candidates(
    tmp_path,
):
    report_file = tmp_path / "misc" / "report.md"
    report_file.parent.mkdir(parents=True)
    original = """# 2026-07-07 preprint by arxiv_tools

---
tags:
  - #personal-2026-07-07
  - #cs.AI-2026-07-07
---

```dataview
TASK
from #cs.AI-2026-07-07
```

## collected

## not collected

### arXiv:2607.00001
"""
    report_file.write_text(original, encoding="utf-8")

    result = migrate_report_file(str(report_file), data_dir=str(tmp_path))

    assert result.status == "failed"
    assert "ambiguous" in result.message
    assert report_file.read_text(encoding="utf-8") == original


def test_legacy_arxiv_without_stable_path_fails_without_identity_candidate(tmp_path):
    report_file = tmp_path / "misc" / "report.md"
    report_file.parent.mkdir(parents=True)
    original = """# 2026-07-07 preprint by arxiv_tools

```dataview
TASK
```

## collected

## not collected

### arXiv:2607.00001
"""
    report_file.write_text(original, encoding="utf-8")

    result = migrate_report_file(str(report_file), data_dir=str(tmp_path))

    assert result.status == "failed"
    assert "missing generated identity" in result.message
    assert report_file.read_text(encoding="utf-8") == original


def test_legacy_journal_without_stable_path_fails_without_identity_candidate(
    tmp_path,
):
    report_file = tmp_path / "misc" / "report.md"
    report_file.parent.mkdir(parents=True)
    original = """# Journal of Chemical Physics Vol. 164, Iss. 16 — 2026

```dataview
TASK
```

## collected

## not collected

### 10.1063/example
"""
    report_file.write_text(original, encoding="utf-8")

    result = migrate_report_file(str(report_file), data_dir=str(tmp_path))

    assert result.status == "failed"
    assert "missing generated identity" in result.message
    assert report_file.read_text(encoding="utf-8") == original


def test_legacy_journal_path_identity_cannot_be_overridden_by_personal_tag(tmp_path):
    report_file = tmp_path / "aip" / "jcp" / "164" / "16.md"
    report_file.parent.mkdir(parents=True)
    original = _legacy_journal_report().replace(
        "tags:\n  - #jcp-v164-i16",
        "tags:\n  - #personal-v164-i16\n  - #jcp-v164-i16",
    )
    report_file.write_text(original, encoding="utf-8")

    result = migrate_report_file(str(report_file), data_dir=str(tmp_path))

    migrated = report_file.read_text(encoding="utf-8")
    assert result.status == "migrated"
    assert "papertrack_journal: jcp" in migrated
    assert "tags:\n  - #personal-v164-i16\n  - jcp-v164-i16" in migrated


def test_migration_validation_failure_restores_backup(tmp_path, monkeypatch):
    from papertrack.reports import migration

    report_file = tmp_path / "aip" / "jcp" / "164" / "16.md"
    report_file.parent.mkdir(parents=True)
    original = _legacy_journal_report()
    report_file.write_text(original, encoding="utf-8")

    monkeypatch.setattr(migration, "validate_report_content", lambda *args, **kwargs: False)

    result = migrate_report_file(str(report_file), data_dir=str(tmp_path))

    assert result.status == "failed"
    assert report_file.read_text(encoding="utf-8") == original


def test_migration_atomic_replace_failure_restores_original(tmp_path, monkeypatch):
    from papertrack.reports import migration

    report_file = tmp_path / "arxiv" / "cs.AI" / "2026" / "07" / "07.md"
    report_file.parent.mkdir(parents=True)
    original = """---
papertrack_format: arxiv-v2
papertrack_source: arxiv
papertrack_category: cs.AI
papertrack_date: "2026-07-07"
tags:
  - #cs.AI-2026-07-07
---

```dataview
TASK
from #cs.AI-2026-07-07
```

## collected

## not collected

### arXiv:2607.00001
"""
    report_file.write_text(original, encoding="utf-8")
    real_replace = migration.os.replace
    failed_once = False

    def fail_temporary_replacement(source, target):
        nonlocal failed_once
        if Path(target) == report_file and str(source).endswith(".tmp") and not failed_once:
            failed_once = True
            raise OSError("simulated replacement failure")
        return real_replace(source, target)

    monkeypatch.setattr(migration.os, "replace", fail_temporary_replacement)

    result = migrate_report_file(str(report_file), data_dir=str(tmp_path))

    assert failed_once is True
    assert result.status == "failed"
    assert report_file.read_text(encoding="utf-8") == original
    assert not report_file.with_suffix(".md.bak").exists()
    assert list(report_file.parent.glob(f".{report_file.name}.*.tmp")) == []


def test_write_report_file_validates_before_touching_filesystem(tmp_path):
    from papertrack.reports import migration

    report_file = tmp_path / "missing-parent" / "report.md"

    with pytest.raises(ValueError, match="failed format validation"):
        migration.write_report_file(
            str(report_file), "invalid rendered Markdown", source="journal"
        )

    assert not report_file.parent.exists()


@pytest.mark.parametrize("replacement_phase", ["before", "after"])
def test_write_report_file_sigterm_leaves_complete_report_and_backup(
    tmp_path, replacement_phase
):
    report_file = tmp_path / "report.md"
    content_file = tmp_path / "new-content.md"
    original = _current_journal_report("Original")
    replacement = _current_journal_report("Replacement")
    report_file.write_text(original, encoding="utf-8")
    content_file.write_text(replacement, encoding="utf-8")
    worker = textwrap.dedent(
        """
        import signal
        import sys
        from pathlib import Path

        from papertrack.reports import migration

        report_path = Path(sys.argv[1])
        content_path = Path(sys.argv[2])
        replacement_phase = sys.argv[3]
        real_replace = migration.os.replace

        def pause_at_report_replacement(source, target):
            if Path(target) != report_path:
                return real_replace(source, target)
            if replacement_phase == "before":
                print("replacement-boundary", flush=True)
                signal.pause()
            real_replace(source, target)
            print("replacement-boundary", flush=True)
            signal.pause()

        migration.os.replace = pause_at_report_replacement
        migration.write_report_file(
            str(report_path),
            content_path.read_text(encoding="utf-8"),
            source="journal",
        )
        """
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            worker,
            str(report_file),
            str(content_file),
            replacement_phase,
        ],
        cwd=os.getcwd(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        ready, _, _ = select.select([process.stdout], [], [], 10)
        assert ready, process.stderr.read()
        boundary = process.stdout.readline().strip()
        assert boundary == "replacement-boundary", process.stderr.read()
        process.terminate()
        _, stderr = process.communicate(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()

    assert process.returncode == -signal.SIGTERM
    expected = original if replacement_phase == "before" else replacement
    assert report_file.read_text(encoding="utf-8") == expected
    assert report_file.with_suffix(".md.bak").read_text(encoding="utf-8") == original
    assert stderr == ""


def test_current_journal_report_with_bad_issue_metadata_is_corrupt(tmp_path):
    report_file = tmp_path / "acs" / "jcim" / "66" / "11.md"
    report_file.parent.mkdir(parents=True)
    report_file.write_text(
        """---
papertrack_format: journal-v2
papertrack_source: journal
papertrack_provider: acs
papertrack_journal: jcim
papertrack_volume: "66"
papertrack_issue: "11"
---

```dataview
TASK
from #jcim-v66-i11
```

## collected

## not collected

### 10.1021/civ066i011_2087001

Title:  Issue Editorial Masthead
""",
        encoding="utf-8",
    )

    result = classify_report_file(str(report_file), data_dir=str(tmp_path))

    assert result.status == "corrupt"
    assert result.source == "journal"


def test_migrate_reports_scans_markdown_and_skips_backups(tmp_path):
    report_file = tmp_path / "aip" / "jcp" / "164" / "16.md"
    report_file.parent.mkdir(parents=True)
    report_file.write_text(_legacy_journal_report(), encoding="utf-8")
    report_file.with_suffix(".md.bak").write_text("old backup", encoding="utf-8")

    results = migrate_reports(str(tmp_path))

    assert [result.status for result in results] == ["migrated"]


def test_current_journal_report_with_image_is_cleaned(tmp_path):
    report_file = tmp_path / "aip" / "jcp" / "164" / "16.md"
    report_file.parent.mkdir(parents=True)
    report_file.write_text(
        """---
papertrack_format: journal-v2
papertrack_source: journal
papertrack_provider: aip
papertrack_journal: jcp
papertrack_volume: "164"
papertrack_issue: "16"
tags:
  - #jcp-v164-i16
---

# Journal of Chemical Physics Vol. 164, Iss. 16 — 2026

```dataview
TASK
from #jcp-v164-i16

WHERE completed

```

## collected

## not collected

### 10.1063/example

Links:

- [ ] [10.1063/example](https://doi.org/10.1063/example)

Title:  Test article

![|700|700x24](https://example.test/toc.gif)
Authors:  A Author

![personal plot](attachments/result.png)
""",
        encoding="utf-8",
    )

    result = migrate_report_file(str(report_file), data_dir=str(tmp_path))

    migrated = report_file.read_text(encoding="utf-8")
    assert result.status == "repaired"
    assert "![|700|700x24]" not in migrated
    assert "Authors:  A Author" in migrated
    assert "![personal plot](attachments/result.png)" in migrated
    assert not report_file.with_suffix(".md.bak").exists()


def test_current_arxiv_migration_repairs_identity_from_stable_path(tmp_path):
    report_file = tmp_path / "arxiv" / "chem-ph" / "2025" / "03" / "01.md"
    report_file.parent.mkdir(parents=True)
    original = """---
papertrack_format: arxiv-v2
papertrack_source: arxiv
papertrack_category: quant-ph
papertrack_date: "2025-03-01"
tags:
  - #quant-ph-2025-03-01
---

# 2025-03-01 preprint by arxiv_tools

```dataview
TASK
from #quant-ph-2025-03-01

WHERE completed
```

## collected

## not collected

### arXiv:2503.00001

Links:

- [x] [arXiv:2503.00001](https://arxiv.org/abs/2503.00001) ✅ 2025-03-02

Title:  Chemistry paper

Authors:  A Author,

Abstract:
> [!quote]- Abstract
> The literal text quant-ph-2025-03-01 in a user-visible field stays unchanged.
"""
    report_file.write_text(original, encoding="utf-8")

    result = migrate_report_file(str(report_file), data_dir=str(tmp_path))

    repaired = report_file.read_text(encoding="utf-8")
    expected = original.replace(
        "papertrack_category: quant-ph", "papertrack_category: chem-ph"
    ).replace(
        "  - #quant-ph-2025-03-01", "  - chem-ph-2025-03-01"
    ).replace(
        "from #quant-ph-2025-03-01", "from #chem-ph-2025-03-01"
    )
    assert result.status == "repaired"
    assert repaired == expected.replace("Abstract:\n> [!quote]", "> [!quote]")
    assert "- [x] [arXiv:2503.00001]" in repaired
    assert "literal text quant-ph-2025-03-01" in repaired
    assert not report_file.with_suffix(".md.bak").exists()


def test_current_cs_arxiv_migration_canonicalizes_tag_and_preserves_user_state(
    tmp_path,
):
    report_file = tmp_path / "arxiv" / "cs.AI" / "2026" / "07" / "07.md"
    report_file.parent.mkdir(parents=True)
    original = """---
papertrack_format: arxiv-v2
papertrack_source: arxiv
papertrack_category: cs.AI
papertrack_date: "2026-07-07"
tags:
  - personal
  - #cs.AI-2026-07-07
---

```dataview
TASK
from #cs.AI-2026-07-07

WHERE completed
```

User prose containing #cs.AI-2026-07-07 must stay unchanged.

## collected

### arXiv:2607.00001

- [x] [arXiv:2607.00001](https://arxiv.org/abs/2607.00001) ✅ 2026-07-08

## not collected

### arXiv:2607.00002

- [ ] [arXiv:2607.00002](https://arxiv.org/abs/2607.00002)
"""
    report_file.write_text(original, encoding="utf-8")

    first = migrate_report_file(str(report_file), data_dir=str(tmp_path))
    repaired = report_file.read_text(encoding="utf-8")
    second = migrate_report_file(str(report_file), data_dir=str(tmp_path))

    assert first.status == "repaired"
    assert second.status == "current"
    assert repaired == report_file.read_text(encoding="utf-8")
    assert "papertrack_category: cs.AI" in repaired
    assert "tags:\n  - personal\n  - cs-ai-2026-07-07" in repaired
    assert "from #cs-ai-2026-07-07" in repaired
    assert "WHERE completed" in repaired
    assert "User prose containing #cs.AI-2026-07-07 must stay unchanged." in repaired
    assert "- [x] [arXiv:2607.00001]" in repaired
    assert "✅ 2026-07-08" in repaired
    assert "- [ ] [arXiv:2607.00002]" in repaired
    assert not report_file.with_suffix(".md.bak").exists()


def test_current_journal_migration_canonicalizes_yaml_tag_only(tmp_path):
    report_file = tmp_path / "aip" / "jcp" / "164" / "16.md"
    report_file.parent.mkdir(parents=True)
    original = """---
papertrack_format: journal-v2
papertrack_source: journal
papertrack_provider: aip
papertrack_journal: jcp
papertrack_volume: "164"
papertrack_issue: "16"
tags:
  - personal
  - #jcp-v164-i16
---

```dataview
TASK
from #jcp-v164-i16

WHERE completed
```

## collected

### 10.1063/example-one

- [x] [10.1063/example-one](https://doi.org/10.1063/example-one) ✅ 2026-07-08

## not collected

### 10.1063/example-two

- [ ] [10.1063/example-two](https://doi.org/10.1063/example-two)
"""
    report_file.write_text(original, encoding="utf-8")

    first = migrate_report_file(str(report_file), data_dir=str(tmp_path))
    repaired = report_file.read_text(encoding="utf-8")
    second = migrate_report_file(str(report_file), data_dir=str(tmp_path))

    assert first.status == "repaired"
    assert second.status == "current"
    assert repaired == report_file.read_text(encoding="utf-8")
    assert "tags:\n  - personal\n  - jcp-v164-i16" in repaired
    assert "from #jcp-v164-i16" in repaired
    assert "WHERE completed" in repaired
    assert "- [x] [10.1063/example-one]" in repaired
    assert "✅ 2026-07-08" in repaired
    assert "- [ ] [10.1063/example-two]" in repaired
    assert not report_file.with_suffix(".md.bak").exists()


def test_current_journal_tag_repair_preserves_known_quality_failure(tmp_path):
    report_file = tmp_path / "aps" / "prl" / "127" / "27.md"
    report_file.parent.mkdir(parents=True)
    original = """---
papertrack_format: journal-v2
papertrack_source: journal
papertrack_provider: aps
papertrack_journal: prl
papertrack_volume: "127"
papertrack_issue: "27"
tags:
  - #prl-v127-i27
---

```dataview
TASK
from #prl-v127-i27
WHERE completed
```

## collected

## not collected

### 10.1103/physrevlett.127.270401

- [ ] [10.1103/physrevlett.127.270401](https://doi.org/10.1103/physrevlett.127.270401)

Title:  Raw <mml:math></mml:math> title
"""
    report_file.write_text(original, encoding="utf-8")

    result = migrate_report_file(str(report_file), data_dir=str(tmp_path))

    repaired = report_file.read_text(encoding="utf-8")
    assert result.status == "repaired"
    assert repaired == original.replace("  - #prl-v127-i27", "  - prl-v127-i27")
    assert has_journal_quality_failure(repaired) is True
    assert not report_file.with_suffix(".md.bak").exists()


def test_current_arxiv_migration_fails_closed_on_ambiguous_generated_tags(tmp_path):
    report_file = tmp_path / "arxiv" / "cs.AI" / "2026" / "07" / "07.md"
    report_file.parent.mkdir(parents=True)
    original = """---
papertrack_format: arxiv-v2
papertrack_source: arxiv
papertrack_category: cs.AI
papertrack_date: "2026-07-07"
tags:
  - #cs.AI-2026-07-07
  - cs-ai-2026-07-07
---

```dataview
TASK
from #cs.AI-2026-07-07
WHERE completed
```

## collected

## not collected

### arXiv:2607.00001
"""
    report_file.write_text(original, encoding="utf-8")

    result = migrate_report_file(str(report_file), data_dir=str(tmp_path))

    assert result.status == "failed"
    assert "ambiguous" in result.message
    assert report_file.read_text(encoding="utf-8") == original
    assert not report_file.with_suffix(".md.bak").exists()


def test_validation_rejects_hash_prefixed_generated_yaml_tag():
    content = """---
papertrack_format: arxiv-v2
papertrack_source: arxiv
papertrack_category: cs.AI
papertrack_date: "2026-07-07"
tags:
  - #cs.AI-2026-07-07
---

```dataview
TASK
from #cs-ai-2026-07-07
WHERE completed
```

## collected

## not collected

### arXiv:2607.00001
"""

    assert validate_report_content(content, source="arxiv") is False


def test_validation_rejects_dotted_generated_arxiv_dataview_source():
    content = """---
papertrack_format: arxiv-v2
papertrack_source: arxiv
papertrack_category: cs.AI
papertrack_date: "2026-07-07"
tags:
  - cs-ai-2026-07-07
---

```dataview
TASK
from #cs.AI-2026-07-07
WHERE completed
```

## collected

## not collected

### arXiv:2607.00001
"""

    assert validate_report_content(content, source="arxiv") is False


def test_validation_accepts_canonical_tag_with_unrelated_user_sources():
    content = """---
papertrack_format: arxiv-v2
papertrack_source: arxiv
papertrack_category: cs.AI
papertrack_date: "2026-07-07"
tags:
  - personal
  - cs-ai-2026-07-07
---

```dataview
LIST
from #personal
```

```dataview
TASK
from #personal
from #cs-ai-2026-07-07
WHERE completed
```

## collected

## not collected

### arXiv:2607.00001
"""

    assert validate_report_content(content, source="arxiv") is True


def test_validation_rejects_hash_prefixed_generated_journal_yaml_tag():
    content = """---
papertrack_format: journal-v2
papertrack_source: journal
papertrack_provider: aip
papertrack_journal: jcp
papertrack_volume: "164"
papertrack_issue: "16"
tags:
  - #jcp-v164-i16
---

```dataview
TASK
from #jcp-v164-i16
WHERE completed
```

## collected

## not collected

### 10.1063/example
"""

    assert validate_report_content(content, source="journal") is False


def test_validation_rejects_missing_or_duplicate_generated_identity_fields():
    missing = """---
papertrack_format: arxiv-v2
papertrack_source: arxiv
tags:
  - cs-ai-2026-07-07
---

```dataview
TASK
```

## collected

## not collected

### arXiv:2607.00001
"""
    duplicate = """---
papertrack_format: arxiv-v2
papertrack_source: arxiv
papertrack_category: cs.AI
papertrack_category: cs.LG
papertrack_date: "2026-07-07"
tags:
  - cs-ai-2026-07-07
---

```dataview
TASK
```

## collected

## not collected

### arXiv:2607.00001
"""

    assert validate_report_content(missing, source="arxiv") is False
    assert validate_report_content(duplicate, source="arxiv") is False


def test_validation_does_not_count_alias_as_generated_tag():
    content = """---
papertrack_format: arxiv-v2
papertrack_source: arxiv
papertrack_category: cs.AI
papertrack_date: "2026-07-07"
aliases:
  - cs-ai-2026-07-07
tags:
  - personal
---

```dataview
TASK
```

## collected

## not collected

### arXiv:2607.00001
"""

    assert validate_report_content(content, source="arxiv") is False


def test_validation_rejects_mixed_inline_and_block_tags_properties():
    content = """---
papertrack_format: arxiv-v2
papertrack_source: arxiv
papertrack_category: cs.AI
papertrack_date: "2026-07-07"
tags: [personal]
tags:
  - cs-ai-2026-07-07
---

```dataview
TASK
```

## collected

## not collected

### arXiv:2607.00001
"""

    assert validate_report_content(content, source="arxiv") is False


def test_legacy_migration_fails_closed_on_inline_tags_property(tmp_path):
    report_file = tmp_path / "arxiv" / "cs.AI" / "2026" / "07" / "07.md"
    report_file.parent.mkdir(parents=True)
    original = """# 2026-07-07 preprint by arxiv_tools

---
tags: [personal]
aliases:
  - ai reading
---

```dataview
TASK
from #cs.AI-2026-07-07
```

## collected

## not collected

### arXiv:2607.00001
"""
    report_file.write_text(original, encoding="utf-8")

    result = migrate_report_file(str(report_file), data_dir=str(tmp_path))

    assert result.status == "failed"
    assert "tags" in result.message
    assert report_file.read_text(encoding="utf-8") == original


def test_current_report_with_missing_identity_is_failed_not_current(tmp_path):
    report_file = tmp_path / "arxiv" / "cs.AI" / "2026" / "07" / "07.md"
    report_file.parent.mkdir(parents=True)
    original = """---
papertrack_format: arxiv-v2
papertrack_source: arxiv
papertrack_date: "2026-07-07"
tags:
  - cs-ai-2026-07-07
---

```dataview
TASK
```

## collected

## not collected

### arXiv:2607.00001
"""
    report_file.write_text(original, encoding="utf-8")

    result = migrate_report_file(str(report_file), data_dir=str(tmp_path))

    assert result.status == "failed"
    assert "validation" in result.message
    assert report_file.read_text(encoding="utf-8") == original


def test_current_arxiv_migration_leaves_canonical_identity_unchanged(tmp_path):
    report_file = tmp_path / "arxiv" / "chem-ph" / "2025" / "03" / "01.md"
    report_file.parent.mkdir(parents=True)
    original = """---
papertrack_format: arxiv-v2
papertrack_source: arxiv
papertrack_category: chem-ph
papertrack_date: "2025-03-01"
tags:
  - chem-ph-2025-03-01
---

```dataview
TASK
from #chem-ph-2025-03-01
```

## collected

## not collected

### arXiv:2503.00001
"""
    report_file.write_text(original, encoding="utf-8")

    result = migrate_report_file(str(report_file), data_dir=str(tmp_path))

    assert result.status == "current"
    assert report_file.read_text(encoding="utf-8") == original


def test_current_arxiv_migration_repairs_tag_without_inserting_missing_query_source(
    tmp_path,
):
    report_file = tmp_path / "arxiv" / "chem-ph" / "2025" / "03" / "01.md"
    report_file.parent.mkdir(parents=True)
    original = """---
papertrack_format: arxiv-v2
papertrack_source: arxiv
papertrack_category: chem-ph
papertrack_date: "2025-03-01"
tags:
  - #chem-ph-2025-03-01
---

```dataview
TASK
```

## collected

## not collected

### arXiv:2503.00001
"""
    report_file.write_text(original, encoding="utf-8")

    result = migrate_report_file(str(report_file), data_dir=str(tmp_path))

    assert result.status == "repaired"
    assert report_file.read_text(encoding="utf-8") == original.replace(
        "  - #chem-ph-2025-03-01", "  - chem-ph-2025-03-01"
    )


def test_current_arxiv_migration_repairs_legacy_inline_dataview_identity(tmp_path):
    report_file = tmp_path / "arxiv" / "chem-ph" / "2025" / "04" / "01.md"
    report_file.parent.mkdir(parents=True)
    original = """---
papertrack_format: arxiv-v2
papertrack_source: arxiv
papertrack_category: quant-ph
papertrack_date: "2025-04-01"
tags:
  - #quant-ph-2025-04-01
---

```dataview
TASK FROM #quant-ph-2025-04-01
WHERE completion
```

## collected

## not collected

### arXiv:2504.00001
"""
    report_file.write_text(original, encoding="utf-8")

    result = migrate_report_file(str(report_file), data_dir=str(tmp_path))

    repaired = report_file.read_text(encoding="utf-8")
    assert result.status == "repaired"
    assert "TASK FROM #chem-ph-2025-04-01" in repaired
    assert "TASK\nfrom" not in repaired


def test_arxiv_identity_migration_does_not_touch_user_tag_or_query(tmp_path):
    report_file = tmp_path / "arxiv" / "chem-ph" / "2025" / "03" / "01.md"
    report_file.parent.mkdir(parents=True)
    original = """---
papertrack_format: arxiv-v2
papertrack_source: arxiv
papertrack_category: quant-ph
papertrack_date: "2025-03-01"
tags:
  - #personal-2025-03-01
  - #quant-ph-2025-03-01
---

```dataview
TASK
from #personal-2025-03-01
from #quant-ph-2025-03-01
```

## collected

## not collected

### arXiv:2503.00001
"""
    report_file.write_text(original, encoding="utf-8")

    result = migrate_report_file(str(report_file), data_dir=str(tmp_path))

    repaired = report_file.read_text(encoding="utf-8")
    assert result.status == "repaired"
    assert "  - #personal-2025-03-01" in repaired
    assert "from #personal-2025-03-01" in repaired
    assert "  - chem-ph-2025-03-01" in repaired
    assert "from #chem-ph-2025-03-01" in repaired


def test_arxiv_identity_migration_fails_when_tag_ownership_is_unknown(
    tmp_path,
):
    report_file = tmp_path / "arxiv" / "chem-ph" / "2025" / "03" / "01.md"
    report_file.parent.mkdir(parents=True)
    original = """---
papertrack_format: arxiv-v2
papertrack_source: arxiv
papertrack_category: chem-ph
papertrack_date: "2025-03-01"
tags:
  - #quant-ph-2025-03-01
---

```dataview
TASK
from #chem-ph-2025-03-01
```

## collected

## not collected

### arXiv:2503.00001
"""
    report_file.write_text(original, encoding="utf-8")

    result = migrate_report_file(str(report_file), data_dir=str(tmp_path))

    assert result.status == "failed"
    assert "generated tag" in result.message
    assert report_file.read_text(encoding="utf-8") == original


def test_current_arxiv_migration_only_repairs_generated_item_inside_tags(tmp_path):
    report_file = tmp_path / "arxiv" / "chem-ph" / "2025" / "03" / "01.md"
    report_file.parent.mkdir(parents=True)
    original = """---
papertrack_format: arxiv-v2
papertrack_source: arxiv
papertrack_category: quant-ph
papertrack_date: "2025-03-01"
aliases:
  - quant-ph-2025-03-01
tags:
  - personal-2025-03-01
  - #quant-ph-2025-03-01
---

```dataview
TASK
from #quant-ph-2025-03-01
```

## collected

## not collected

### arXiv:2503.00001
"""
    report_file.write_text(original, encoding="utf-8")

    result = migrate_report_file(str(report_file), data_dir=str(tmp_path))

    repaired = report_file.read_text(encoding="utf-8")
    assert result.status == "repaired"
    assert "aliases:\n  - quant-ph-2025-03-01" in repaired
    assert "tags:\n  - personal-2025-03-01\n  - chem-ph-2025-03-01" in repaired


def test_current_journal_migration_does_not_repair_alias_with_issue_suffix(tmp_path):
    report_file = tmp_path / "aip" / "jcp" / "164" / "16.md"
    report_file.parent.mkdir(parents=True)
    original = """---
papertrack_format: journal-v2
papertrack_source: journal
papertrack_provider: aip
papertrack_journal: jcp
papertrack_volume: "164"
papertrack_issue: "16"
aliases:
  - personal-v164-i16
tags:
  - personal
---

```dataview
TASK
from #jcp-v164-i16
```

## collected

## not collected

### 10.1063/example
"""
    report_file.write_text(original, encoding="utf-8")

    result = migrate_report_file(str(report_file), data_dir=str(tmp_path))

    assert result.status == "failed"
    assert "generated tag" in result.message
    assert report_file.read_text(encoding="utf-8") == original


def test_arxiv_identity_migration_selects_generated_dataview_block(tmp_path):
    report_file = tmp_path / "arxiv" / "chem-ph" / "2025" / "03" / "01.md"
    report_file.parent.mkdir(parents=True)
    original = """---
papertrack_format: arxiv-v2
papertrack_source: arxiv
papertrack_category: quant-ph
papertrack_date: "2025-03-01"
tags:
  - #quant-ph-2025-03-01
---

```dataview
LIST
from #personal-2025-03-01
```

```dataview
TASK
from #quant-ph-2025-03-01
WHERE completed
```

## collected

## not collected

### arXiv:2503.00001
"""
    report_file.write_text(original, encoding="utf-8")

    result = migrate_report_file(str(report_file), data_dir=str(tmp_path))

    repaired = report_file.read_text(encoding="utf-8")
    assert result.status == "repaired"
    assert "LIST\nfrom #personal-2025-03-01" in repaired
    assert "TASK\nfrom #chem-ph-2025-03-01" in repaired


def test_migrate_reports_isolates_invalid_utf8_and_continues(tmp_path):
    bad = tmp_path / "arxiv" / "chem-ph" / "2025" / "03" / "01.md"
    good = tmp_path / "arxiv" / "chem-ph" / "2025" / "03" / "02.md"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"\xff\xfe")
    good.write_text(
        """---
papertrack_format: arxiv-v2
papertrack_source: arxiv
papertrack_category: chem-ph
papertrack_date: "2025-03-02"
tags:
  - #chem-ph-2025-03-02
---

```dataview
TASK
from #chem-ph-2025-03-02
```

## collected

## not collected

### arXiv:2503.00002
""",
        encoding="utf-8",
    )

    results = migrate_reports(str(tmp_path))

    assert [(result.status, result.path) for result in results] == [
        ("failed", str(bad)),
        ("repaired", str(good)),
    ]


def test_migrate_reports_isolates_ambiguous_legacy_query_and_continues(tmp_path):
    bad = tmp_path / "aip" / "jcp" / "164" / "16.md"
    good = tmp_path / "arxiv" / "chem-ph" / "2025" / "03" / "02.md"
    bad.parent.mkdir(parents=True)
    good.parent.mkdir(parents=True)
    bad_content = _legacy_journal_report().replace(
        "```dataview\nTASK\nfrom #jcp-v164-i16\n\nWHERE completed\n\n```",
        """```dataview
TASK
from #jcp-v164-i16
```

```dataview
TASK
from #jcp-v164-i16
```""",
    )
    bad.write_text(bad_content, encoding="utf-8")
    good.write_text(
        """---
papertrack_format: arxiv-v2
papertrack_source: arxiv
papertrack_category: chem-ph
papertrack_date: "2025-03-02"
tags:
  - #chem-ph-2025-03-02
---

```dataview
TASK
from #chem-ph-2025-03-02
```

## collected

## not collected

### arXiv:2503.00002
""",
        encoding="utf-8",
    )

    results = migrate_reports(str(tmp_path))

    by_path = {result.path: result for result in results}
    assert by_path[str(bad)].status == "failed"
    assert "ambiguous" in by_path[str(bad)].message
    assert bad.read_text(encoding="utf-8") == bad_content
    assert by_path[str(good)].status == "repaired"


def test_preserve_task_state_keeps_checkbox_and_plugin_suffix():
    old = "- [x] [10.1063/example](https://doi.org/10.1063/example) [completion:: 2026-06-09]\n"
    new = "- [ ] [10.1063/example](https://doi.org/10.1063/example)\n"

    assert preserve_task_state(new, old) == old


def test_preserve_task_state_transfers_completed_update_task_to_paper_link():
    old = """- [ ] [arXiv:2606.00001](https://arxiv.org/abs/2606.00001)
- [x] [[#arXiv:2606.00001]] ✅ 2026-06-09
"""
    new = "- [ ] [arXiv:2606.00001](https://arxiv.org/abs/2606.00001)\n"

    assert preserve_task_state(new, old) == (
        "- [x] [arXiv:2606.00001](https://arxiv.org/abs/2606.00001)"
        " ✅ 2026-06-09\n"
    )

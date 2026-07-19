import os
import select
import signal
import subprocess
import sys
import textwrap

import pytest

from papertrack.migration import (
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
---

```dataview
TASK
```

## collected

## not collected

### 10.1063/example

Title:  {title}
"""


def test_aps_official_doi_casing_is_not_crossref_provenance():
    official = """### 10.1103/PhysRevLett.127.270401

Title:  Quantum Vacuum Sagnac Effect
"""
    old_crossref = """### 10.1103/physrevlett.127.270401

Title:  Quantum Vacuum Sagnac Effect
"""

    assert has_journal_quality_failure(official) is False
    assert has_journal_quality_failure(old_crossref) is True


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


def test_legacy_journal_migration_removes_toc_image_and_preserves_task(tmp_path):
    report_file = tmp_path / "aip" / "jcp" / "164" / "16.md"
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


def test_migration_validation_failure_restores_backup(tmp_path, monkeypatch):
    from papertrack import migration

    report_file = tmp_path / "aip" / "jcp" / "164" / "16.md"
    report_file.parent.mkdir(parents=True)
    original = _legacy_journal_report()
    report_file.write_text(original, encoding="utf-8")

    monkeypatch.setattr(migration, "validate_report_content", lambda *args, **kwargs: False)

    result = migrate_report_file(str(report_file), data_dir=str(tmp_path))

    assert result.status == "failed"
    assert report_file.read_text(encoding="utf-8") == original


def test_write_report_file_validates_before_touching_filesystem(tmp_path):
    from papertrack import migration

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

        from papertrack import migration

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
""",
        encoding="utf-8",
    )

    result = migrate_report_file(str(report_file), data_dir=str(tmp_path))

    migrated = report_file.read_text(encoding="utf-8")
    assert result.status == "cleaned"
    assert "![|700|700x24]" not in migrated
    assert "Authors:  A Author" in migrated
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
        "  - #quant-ph-2025-03-01", "  - #chem-ph-2025-03-01"
    ).replace(
        "from #quant-ph-2025-03-01", "from #chem-ph-2025-03-01"
    )
    assert result.status == "repaired"
    assert repaired == expected
    assert "- [x] [arXiv:2503.00001]" in repaired
    assert "literal text quant-ph-2025-03-01" in repaired
    assert not report_file.with_suffix(".md.bak").exists()


def test_current_arxiv_migration_leaves_matching_identity_unchanged(tmp_path):
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


def test_current_arxiv_migration_does_not_normalize_incomplete_matching_report(
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

    assert result.status == "current"
    assert report_file.read_text(encoding="utf-8") == original


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


def test_preserve_task_state_keeps_checkbox_and_plugin_suffix():
    old = "- [x] [10.1063/example](https://doi.org/10.1063/example) [completion:: 2026-06-09]\n"
    new = "- [ ] [10.1063/example](https://doi.org/10.1063/example)\n"

    assert preserve_task_state(new, old) == old

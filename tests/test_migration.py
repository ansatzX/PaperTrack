from papertrack.migration import (
    CURRENT_JOURNAL_FORMAT,
    classify_report_file,
    has_journal_quality_failure,
    migrate_report_file,
    migrate_reports,
    preserve_task_state,
    validate_report_content,
)


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


def test_preserve_task_state_keeps_checkbox_and_plugin_suffix():
    old = "- [x] [10.1063/example](https://doi.org/10.1063/example) [completion:: 2026-06-09]\n"
    new = "- [ ] [10.1063/example](https://doi.org/10.1063/example)\n"

    assert preserve_task_state(new, old) == old

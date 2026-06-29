from papertrack.migration import (
    CURRENT_JOURNAL_FORMAT,
    migrate_report_file,
    migrate_reports,
    preserve_task_state,
    validate_report_content,
)


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

![|700](https://example.test/toc.jpg)
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
    assert "![|700]" not in migrated
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


def test_migrate_reports_scans_markdown_and_skips_backups(tmp_path):
    report_file = tmp_path / "aip" / "jcp" / "164" / "16.md"
    report_file.parent.mkdir(parents=True)
    report_file.write_text(_legacy_journal_report(), encoding="utf-8")
    report_file.with_suffix(".md.bak").write_text("old backup", encoding="utf-8")

    results = migrate_reports(str(tmp_path))

    assert [result.status for result in results] == ["migrated"]


def test_preserve_task_state_keeps_checkbox_and_plugin_suffix():
    old = "- [x] [10.1063/example](https://doi.org/10.1063/example) [completion:: 2026-06-09]\n"
    new = "- [ ] [10.1063/example](https://doi.org/10.1063/example)\n"

    assert preserve_task_state(new, old) == old

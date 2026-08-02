from scripts.repair_all_aps_reports import _candidates


def test_candidates_filter_at_or_after_formal_issue(tmp_path):
    root = tmp_path / "aps" / "prl"
    for volume, issue in (("119", "52"), ("120", "1"), ("120", "2"), ("121", "1")):
        folder = root / volume
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{issue}.md").write_text("report")

    assert _candidates(tmp_path, "prl", from_issue=(120, 1)) == [
        ("120", "1"), ("120", "2"), ("121", "1"),
    ]

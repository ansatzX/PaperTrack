from pathlib import Path


def test_aps_issue_cache_runner_is_issue_only_and_uses_dedicated_chrome():
    content = Path("run_aps_issue_cache.sh").read_text(encoding="utf-8")

    assert "--launch-browser" in content
    assert "--browser-user-data-dir" in content
    assert "google-chrome-papertrack" in content
    assert "--phase issue" in content
    assert "--issue-interval 120" in content
    assert "--from-issue" in content

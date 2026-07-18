import json
from pathlib import Path

import pytest

from papertrack.journal_rebuild import (
    OFFICIAL_SUCCESS_UNRESOLVED,
    RESOLVED,
    RebuildCandidate,
    load_provider_candidates,
    materialize_manifest,
    run_provider_rebuild,
)
from papertrack.source_policy import CrossrefMode


def _candidate(*reasons: str, priority: int = 10) -> RebuildCandidate:
    return RebuildCandidate(
        "acs",
        "jcim",
        "66",
        "12",
        "acs/jcim/66/12.md",
        "ambiguous_review",
        priority,
        reasons or ("ordinary_article_missing_abstract",),
    )


def _report(*entries: tuple[str, str, str]) -> str:
    papers = []
    for doi, title, abstract in entries:
        abstract_block = (
            f"\nAbstract:\n> [!quote]- Abstract\n> {abstract}\n" if abstract else "\n"
        )
        papers.append(
            f"### {doi}\n\nLinks:\n\n- [ ] [{doi}](https://doi.org/{doi})\n\n"
            f"Title:  {title}\n\nAuthors:  A. Author\n{abstract_block}"
        )
    return (
        "---\npapertrack_format: journal-v2\n---\n\n"
        "```dataview\nTASK\n```\n\n## collected\n\n"
        + "\n".join(papers)
        + "\n## not collected\n"
    )


def _configure(monkeypatch):
    from papertrack import journal_rebuild

    monkeypatch.setattr(
        journal_rebuild,
        "journal_configs",
        {
            "jcim": {
                "name": "Journal of Chemical Information and Modeling",
                "slug": "jcim",
                "issn": "1549-9596",
                "acs_code": "jcisd8",
            }
        },
    )
    return journal_rebuild


def test_candidate_manifest_round_trip_is_stably_priority_ordered(tmp_path):
    later = _candidate("raw_html", priority=20)
    first = RebuildCandidate(
        "aip",
        "jcp",
        "160",
        "4",
        "aip/jcp/160/4.md",
        "must_rebuild",
        10,
        ("whole_issue_missing_abstracts",),
    )
    second = RebuildCandidate(
        "aps",
        "prl",
        "127",
        "27",
        "aps/prl/127/27.md",
        "must_rebuild",
        10,
        ("lowercase_crossref_doi",),
    )
    manifest = tmp_path / "diagnostics" / "manifest.json"

    materialize_manifest(manifest, (later, first, second))

    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["audit"] == {"kind": "offline_markdown_quality"}
    assert [issue["relative_path"] for issue in payload["issues"]] == [
        first.relative_path,
        second.relative_path,
        later.relative_path,
    ]
    assert load_provider_candidates(manifest, "acs") == [later]
    assert load_provider_candidates(manifest, "aip") == [first]
    assert first.expected_relative_path == first.relative_path


def test_candidate_rejects_path_disagreeing_with_structured_identity():
    with pytest.raises(ValueError, match="relative_path"):
        RebuildCandidate(
            "acs",
            "jcim",
            "66",
            "12",
            "acs/jcim/66/13.md",
            "ambiguous_review",
            10,
            ("raw_html",),
        )


def test_resolved_progress_skips_a_later_run(tmp_path, monkeypatch):
    module = _configure(monkeypatch)
    calls = []

    def rebuild(**kwargs):
        calls.append(kwargs)
        path = tmp_path / "data" / "acs" / "jcim" / "66" / "12.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            _report(("10.1021/acs.jcim.1c00001", "Ordinary article", "Abstract")),
            encoding="utf-8",
        )
        return True

    monkeypatch.setattr(module, "filter_journal_to_md", rebuild)
    candidate = _candidate()

    first = run_provider_rebuild(
        "acs", (candidate,), tmp_path / "data", tmp_path / "diagnostics"
    )
    second = run_provider_rebuild(
        "acs", (candidate,), tmp_path / "data", tmp_path / "diagnostics"
    )

    assert first.outcomes == {candidate.key: RESOLVED}
    assert second.skipped_keys == (candidate.key,)
    assert len(calls) == 1
    assert calls[0]["source_policy"].crossref_mode is CrossrefMode.METADATA
    assert calls[0]["md_folder"] == str(tmp_path / "data" / "acs")


def test_unresolved_skips_unless_explicitly_retried(tmp_path, monkeypatch):
    module = _configure(monkeypatch)
    calls = 0

    def rebuild(**kwargs):
        nonlocal calls
        calls += 1
        path = tmp_path / "data" / "acs" / "jcim" / "66" / "12.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            _report(("10.1021/acs.jcim.1c00001", "Ordinary article", "")),
            encoding="utf-8",
        )
        return True

    monkeypatch.setattr(module, "filter_journal_to_md", rebuild)
    candidate = _candidate()

    first = run_provider_rebuild(
        "acs", (candidate,), tmp_path / "data", tmp_path / "diagnostics"
    )
    skipped = run_provider_rebuild(
        "acs", (candidate,), tmp_path / "data", tmp_path / "diagnostics"
    )
    retried = run_provider_rebuild(
        "acs",
        (candidate,),
        tmp_path / "data",
        tmp_path / "diagnostics",
        retry_unresolved=True,
    )

    assert first.outcomes == {candidate.key: OFFICIAL_SUCCESS_UNRESOLVED}
    assert skipped.skipped_keys == (candidate.key,)
    assert skipped.outcomes == {candidate.key: OFFICIAL_SUCCESS_UNRESOLVED}
    assert skipped.unresolved_keys == (candidate.key,)
    assert retried.outcomes == {candidate.key: OFFICIAL_SUCCESS_UNRESOLVED}
    assert calls == 2


def test_reset_progress_retries_a_previously_resolved_candidate(tmp_path, monkeypatch):
    module = _configure(monkeypatch)
    path = tmp_path / "data" / "acs" / "jcim" / "66" / "12.md"
    calls = 0

    def rebuild(**kwargs):
        nonlocal calls
        calls += 1
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            _report(("10.1021/acs.jcim.1c00001", "Ordinary article", "Abstract")),
            encoding="utf-8",
        )
        return True

    monkeypatch.setattr(module, "filter_journal_to_md", rebuild)
    candidate = _candidate()
    run_provider_rebuild(
        "acs", (candidate,), tmp_path / "data", tmp_path / "diagnostics"
    )

    summary = run_provider_rebuild(
        "acs",
        (candidate,),
        tmp_path / "data",
        tmp_path / "diagnostics",
        reset_progress=True,
    )

    assert summary.attempted_keys == (candidate.key,)
    assert calls == 2


@pytest.mark.parametrize("failure", [False, RuntimeError("partial failure")])
def test_fetch_failure_restores_original_bytes_and_remains_eligible(
    tmp_path, monkeypatch, failure
):
    module = _configure(monkeypatch)
    path = tmp_path / "data" / "acs" / "jcim" / "66" / "12.md"
    path.parent.mkdir(parents=True)
    original = b"\xffdurable user bytes\n"
    path.write_bytes(original)
    calls = 0

    def rebuild(**kwargs):
        nonlocal calls
        calls += 1
        path.write_text("partial replacement", encoding="utf-8")
        if failure is False:
            return False
        raise failure

    monkeypatch.setattr(module, "filter_journal_to_md", rebuild)
    candidate = _candidate()

    first = run_provider_rebuild(
        "acs", (candidate,), tmp_path / "data", tmp_path / "diagnostics"
    )
    second = run_provider_rebuild(
        "acs", (candidate,), tmp_path / "data", tmp_path / "diagnostics"
    )

    assert first.fetch_failed_keys == (candidate.key,)
    assert second.fetch_failed_keys == (candidate.key,)
    assert path.read_bytes() == original
    assert calls == 2


def test_failed_new_report_removes_partial_file(tmp_path, monkeypatch):
    module = _configure(monkeypatch)
    path = tmp_path / "data" / "acs" / "jcim" / "66" / "12.md"

    def rebuild(**kwargs):
        path.parent.mkdir(parents=True)
        path.write_text("partial replacement", encoding="utf-8")
        return False

    monkeypatch.setattr(module, "filter_journal_to_md", rebuild)

    run_provider_rebuild(
        "acs", (_candidate(),), tmp_path / "data", tmp_path / "diagnostics"
    )

    assert not path.exists()


def test_progress_is_persisted_with_replace(tmp_path, monkeypatch):
    module = _configure(monkeypatch)
    replaced = []
    real_replace = module.os.replace

    def track_replace(source, target):
        replaced.append((Path(source), Path(target)))
        real_replace(source, target)

    monkeypatch.setattr(module.os, "replace", track_replace)
    monkeypatch.setattr(module, "filter_journal_to_md", lambda **kwargs: False)

    run_provider_rebuild(
        "acs", (_candidate(),), tmp_path / "data", tmp_path / "diagnostics"
    )

    progress = tmp_path / "diagnostics" / "journal_rebuild_acs_state.json"
    assert replaced
    assert replaced[-1][1] == progress
    assert replaced[-1][0].parent == progress.parent
    assert not replaced[-1][0].exists()


def test_non_dry_run_materializes_the_runtime_manifest(tmp_path, monkeypatch):
    module = _configure(monkeypatch)
    monkeypatch.setattr(module, "filter_journal_to_md", lambda **kwargs: False)
    candidate = _candidate()

    run_provider_rebuild(
        "acs", (candidate,), tmp_path / "data", tmp_path / "diagnostics"
    )

    manifest = tmp_path / "diagnostics" / "journal_rebuild_manifest.json"
    assert load_provider_candidates(manifest, "acs") == [candidate]


def test_success_is_assessed_only_against_generated_entry_fields(tmp_path, monkeypatch):
    module = _configure(monkeypatch)
    path = tmp_path / "data" / "acs" / "jcim" / "66" / "12.md"

    def rebuild(**kwargs):
        path.parent.mkdir(parents=True)
        content = _report(
            ("10.1021/acs.jcim.1c00001", "Correction to: Prior work", ""),
            ("10.1021/acs.jcim.1c00002", "Ordinary article", "Abstract"),
        )
        content = content.replace("papertrack_format", "<template> papertrack_format")
        path.write_text(content, encoding="utf-8")
        return True

    monkeypatch.setattr(module, "filter_journal_to_md", rebuild)
    candidate = _candidate("ordinary_article_missing_abstract", "raw_html")

    summary = run_provider_rebuild(
        "acs", (candidate,), tmp_path / "data", tmp_path / "diagnostics"
    )

    assert summary.outcomes == {candidate.key: RESOLVED}


@pytest.mark.parametrize(
    ("reason", "entries"),
    [
        (
            "raw_html",
            (("10.1021/acs.jcim.1c00001", "Ordinary <i>article</i>", "Abstract"),),
        ),
        (
            "raw_mathml",
            (("10.1021/acs.jcim.1c00001", "Ordinary article", "<mml:math>x</mml:math>"),),
        ),
        (
            "lowercase_crossref_doi",
            (("10.1103/physrevlett.127.000001", "Ordinary article", "Abstract"),),
        ),
        (
            "whole_issue_missing_abstracts",
            (("10.1021/acs.jcim.1c00001", "Ordinary article", ""),),
        ),
    ],
)
def test_targeted_defects_remain_unresolved(tmp_path, monkeypatch, reason, entries):
    module = _configure(monkeypatch)
    path = tmp_path / "data" / "acs" / "jcim" / "66" / "12.md"

    def rebuild(**kwargs):
        path.parent.mkdir(parents=True)
        path.write_text(_report(*entries), encoding="utf-8")
        return True

    monkeypatch.setattr(module, "filter_journal_to_md", rebuild)
    candidate = _candidate(reason)

    summary = run_provider_rebuild(
        "acs", (candidate,), tmp_path / "data", tmp_path / "diagnostics"
    )

    assert summary.outcomes == {candidate.key: OFFICIAL_SUCCESS_UNRESOLVED}


def test_dry_run_is_ordered_and_strictly_side_effect_free(tmp_path, monkeypatch):
    module = _configure(monkeypatch)
    calls = []
    monkeypatch.setattr(
        module, "filter_journal_to_md", lambda **kwargs: calls.append(kwargs)
    )
    later = _candidate(priority=20)
    first = RebuildCandidate(
        "acs",
        "jctc",
        "22",
        "12",
        "acs/jctc/22/12.md",
        "must_rebuild",
        10,
        ("raw_html",),
    )
    data_dir = tmp_path / "data"
    diagnostics_dir = tmp_path / "diagnostics"

    summary = run_provider_rebuild(
        "acs", (later, first), data_dir, diagnostics_dir, dry_run=True
    )

    assert summary.ordered_candidate_keys == (first.key, later.key)
    assert summary.attempted_keys == ()
    assert calls == []
    assert not data_dir.exists()
    assert not diagnostics_dir.exists()

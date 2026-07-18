from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from papertrack.journal_rebuild import (
    FETCH_FAILED,
    OFFICIAL_SUCCESS_UNRESOLVED,
    RESOLVED,
    RebuildSummary,
)


ACS_PATHS = (
    "acs/jcim/66/12.md",
    "acs/jctc/22/12.md",
)
AIP_REVIEW_PATHS = (
    "aip/jcp/148/10.md",
    "aip/jcp/148/11.md",
    "aip/jcp/148/13.md",
    "aip/jcp/148/14.md",
    "aip/jcp/148/15.md",
    "aip/jcp/148/24.md",
    "aip/jcp/149/7.md",
    "aip/jcp/149/24.md",
    "aip/jcp/150/4.md",
    "aip/jcp/150/20.md",
    "aip/jcp/153/12.md",
    "aip/jcp/153/21.md",
    "aip/jcp/154/9.md",
    "aip/jcp/154/22.md",
    "aip/jcp/155/5.md",
    "aip/jcp/155/9.md",
    "aip/jcp/157/16.md",
    "aip/jcp/158/5.md",
    "aip/jcp/158/23.md",
    "aip/jcp/159/14.md",
    "aip/jcp/160/4.md",
    "aip/jcp/160/5.md",
    "aip/jcp/160/6.md",
    "aip/jcp/160/9.md",
    "aip/jcp/161/4.md",
    "aip/jcp/161/7.md",
    "aip/jcp/161/19.md",
    "aip/jcp/162/19.md",
    "aip/jcp/162/22.md",
    "aip/jcp/163/12.md",
    "aip/jcp/163/21.md",
    "aip/jcp/164/22.md",
)
AIP_PRIORITY_PATHS = (
    "aip/jcp/160/4.md",
    "aip/jcp/160/5.md",
    "aip/jcp/160/6.md",
    "aip/jcp/160/9.md",
)
APS_MUST_REBUILD_PATHS = (
    "aps/prl/127/27.md",
    "aps/prl/129/27.md",
)
APS_REVIEW_PATHS = (
    "aps/prl/122/10.md",
    "aps/prl/123/25.md",
    "aps/prl/124/22.md",
    "aps/prl/125/12.md",
    "aps/prl/131/6.md",
    "aps/prl/132/13.md",
    "aps/prl/133/20.md",
)


def _worker(provider: str):
    return importlib.import_module(f"scripts.rebuild_{provider}_candidates")


def test_workers_define_exact_audited_candidate_sets_and_priority_order():
    acs = _worker("acs").default_candidates()
    aip = _worker("aip").default_candidates()
    aps = _worker("aps").default_candidates()

    assert tuple(candidate.relative_path for candidate in acs) == ACS_PATHS
    assert {candidate.relative_path for candidate in aip} == set(AIP_REVIEW_PATHS)
    assert tuple(candidate.relative_path for candidate in aip[:4]) == AIP_PRIORITY_PATHS
    assert tuple(candidate.relative_path for candidate in aps) == (
        *APS_MUST_REBUILD_PATHS,
        *APS_REVIEW_PATHS,
    )
    assert (len(acs), len(aip), len(aps)) == (2, 32, 9)
    assert len(acs) + len(aip) + len(aps) == 43


def test_workers_define_exact_classifications_priorities_and_reasons():
    acs = _worker("acs").default_candidates()
    assert [
        (candidate.classification, candidate.priority, candidate.reasons)
        for candidate in acs
    ] == [
        (
            "ambiguous_review",
            10,
            ("jcim_priority", "ordinary_article_missing_abstract"),
        ),
        ("must_rebuild", 20, ("whole_issue_missing_abstracts", "raw_html")),
    ]

    aip = _worker("aip").default_candidates()
    assert all(candidate.classification == "ambiguous_review" for candidate in aip)
    assert all(
        candidate.priority == (10 if candidate.relative_path in AIP_PRIORITY_PATHS else 20)
        for candidate in aip
    )
    assert {candidate.reasons for candidate in aip} == {
        ("ordinary_article_missing_abstract",)
    }

    aps = _worker("aps").default_candidates()
    assert all(candidate.classification == "must_rebuild" for candidate in aps[:2])
    assert all(candidate.priority == 10 for candidate in aps[:2])
    assert {candidate.reasons for candidate in aps[:2]} == {
        ("whole_issue_missing_abstracts", "lowercase_crossref_doi", "raw_mathml")
    }
    assert all(candidate.classification == "ambiguous_review" for candidate in aps[2:])
    assert all(candidate.priority == 20 for candidate in aps[2:])
    assert {candidate.reasons for candidate in aps[2:]} == {
        ("ordinary_article_missing_abstract",)
    }


@pytest.mark.parametrize("provider", ["acs", "aip", "aps"])
def test_worker_main_forwards_only_its_candidates_and_cli_options(
    provider, tmp_path, monkeypatch, capsys
):
    worker = _worker(provider)
    calls = []

    def fake_run_provider_rebuild(*args, **kwargs):
        calls.append((args, kwargs))
        keys = tuple(candidate.relative_path for candidate in args[1])
        return RebuildSummary(provider, keys, keys[:1], keys[1:], {})

    monkeypatch.setattr(worker, "run_provider_rebuild", fake_run_provider_rebuild)
    data_dir = tmp_path / "data"
    diagnostics_dir = tmp_path / "diagnostics"

    exit_code = worker.main(
        [
            "--data-dir",
            str(data_dir),
            "--diagnostics-dir",
            str(diagnostics_dir),
            "--retry-unresolved",
            "--reset-progress",
            "--dry-run",
        ]
    )

    assert exit_code == 0
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0] == provider
    assert args[1] == worker.default_candidates()
    assert {candidate.provider for candidate in args[1]} == {provider}
    assert args[2:] == ()
    assert kwargs == {
        "data_dir": Path(data_dir),
        "diagnostics_dir": Path(diagnostics_dir),
        "retry_unresolved": True,
        "reset_progress": True,
        "dry_run": True,
    }
    assert capsys.readouterr().out.startswith(f"{provider}: candidates=")


@pytest.mark.parametrize(
    ("outcome", "expected_exit"),
    [
        (RESOLVED, 0),
        (OFFICIAL_SUCCESS_UNRESOLVED, 1),
        (FETCH_FAILED, 1),
    ],
)
def test_worker_exit_status_follows_rebuild_summary_semantics(
    outcome, expected_exit, tmp_path, monkeypatch
):
    worker = _worker("acs")
    candidate = worker.default_candidates()[0]
    summary = RebuildSummary(
        "acs",
        (candidate.key,),
        (candidate.key,),
        (),
        {candidate.key: outcome},
    )
    monkeypatch.setattr(worker, "run_provider_rebuild", lambda *args, **kwargs: summary)

    assert worker.main(
        [
            "--data-dir",
            str(tmp_path / "data"),
            "--diagnostics-dir",
            str(tmp_path / "diagnostics"),
        ]
    ) == expected_exit


def test_provider_workers_do_not_import_each_other():
    scripts_dir = Path(__file__).parents[1] / "scripts"
    for provider in ("acs", "aip", "aps"):
        content = (scripts_dir / f"rebuild_{provider}_candidates.py").read_text()
        other_providers = {"acs", "aip", "aps"} - {provider}
        assert all(
            f"rebuild_{other}_candidates" not in content
            for other in other_providers
        )

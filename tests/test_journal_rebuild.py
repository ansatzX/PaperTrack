import json
from pathlib import Path

import pytest

from papertrack.journal_rebuild import (
    FETCH_FAILED,
    OFFICIAL_SUCCESS_UNRESOLVED,
    RESOLVED,
    RebuildCandidate,
    _load_progress,
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


def test_equal_priority_manifest_order_is_deterministic_by_path(tmp_path):
    later_path = RebuildCandidate(
        "aps",
        "prl",
        "129",
        "27",
        "aps/prl/129/27.md",
        "must_rebuild",
        10,
        ("raw_mathml",),
    )
    earlier_path = RebuildCandidate(
        "aps",
        "prl",
        "127",
        "27",
        "aps/prl/127/27.md",
        "must_rebuild",
        10,
        ("raw_mathml",),
    )
    manifest = tmp_path / "manifest.json"

    materialize_manifest(manifest, (later_path, earlier_path))

    assert load_provider_candidates(manifest, "aps") == [earlier_path, later_path]


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


@pytest.mark.parametrize("reasons", [(), ("unknown_reason",), "raw_html"])
def test_candidate_rejects_empty_unknown_or_non_sequence_reasons(reasons):
    with pytest.raises(ValueError, match="reasons"):
        RebuildCandidate(
            "acs",
            "jcim",
            "66",
            "12",
            "acs/jcim/66/12.md",
            "ambiguous_review",
            10,
            reasons,
        )


def test_candidate_accepts_approved_informational_reason():
    candidate = RebuildCandidate(
        "acs",
        "jcim",
        "66",
        "12",
        "acs/jcim/66/12.md",
        "ambiguous_review",
        10,
        ("jcim_priority", "ordinary_article_missing_abstract"),
    )

    assert candidate.reasons[0] == "jcim_priority"


@pytest.mark.parametrize(
    "payload",
    [
        {"schema_version": 1, "audit": {"kind": "offline_markdown_quality"}},
        {
            "schema_version": 1,
            "audit": {"kind": "offline_markdown_quality"},
            "issues": {},
        },
        {
            "schema_version": 1,
            "audit": {"kind": "offline_markdown_quality"},
            "issues": [{"provider": "acs"}],
        },
    ],
)
def test_manifest_loader_rejects_missing_wrong_type_or_malformed_issues(
    tmp_path, payload
):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError) as error:
        load_provider_candidates(manifest, "acs")
    message = str(error.value)
    assert "journal rebuild manifest" in message
    assert str(manifest) in message
    assert "issues" in message


@pytest.mark.parametrize(
    "payload",
    [
        {
            "schema_version": 1,
            "audit": {"kind": "offline_markdown_quality"},
            "issues": [],
            "extra": True,
        },
        {"audit": {"kind": "offline_markdown_quality"}, "issues": []},
        {
            "schema_version": "1",
            "audit": {"kind": "offline_markdown_quality"},
            "issues": [],
        },
        {"schema_version": 1, "audit": {"kind": "wrong"}, "issues": []},
        {
            "schema_version": 1,
            "audit": {"kind": "offline_markdown_quality", "extra": True},
            "issues": [],
        },
    ],
)
def test_manifest_loader_requires_exact_contextual_top_level_schema(tmp_path, payload):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError) as error:
        load_provider_candidates(manifest, "acs")

    assert "journal rebuild manifest" in str(error.value)
    assert str(manifest) in str(error.value)


def test_manifest_loader_wraps_malformed_json_with_path_context(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{broken", encoding="utf-8")

    with pytest.raises(RuntimeError, match=r"manifest.*manifest\.json.*JSON"):
        load_provider_candidates(manifest, "acs")


@pytest.mark.parametrize(
    "payload",
    [
        {"schema_version": 1, "provider": "acs"},
        {"schema_version": 1, "provider": "acs", "issues": []},
        {
            "schema_version": 1,
            "provider": "acs",
            "issues": {"acs/jcim/66/12.md": {}},
        },
        {
            "schema_version": 1,
            "provider": "acs",
            "issues": {"acs/jcim/66/12.md": {"outcome": 3}},
        },
        {
            "schema_version": 1,
            "provider": "acs",
            "issues": {"not/a/candidate": {"outcome": RESOLVED}},
        },
    ],
)
def test_progress_loader_rejects_missing_wrong_type_or_malformed_issues(
    tmp_path, payload
):
    progress = tmp_path / "journal_rebuild_acs_state.json"
    progress.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError) as error:
        _load_progress(progress, "acs")
    message = str(error.value)
    assert "journal rebuild progress" in message
    assert str(progress) in message
    assert "issues" in message


@pytest.mark.parametrize(
    "payload",
    [
        {"schema_version": 1, "provider": "acs", "issues": {}, "extra": True},
        {"provider": "acs", "issues": {}},
        {"schema_version": "1", "provider": "acs", "issues": {}},
        {"schema_version": 1, "provider": 3, "issues": {}},
        {"schema_version": 1, "provider": "aip", "issues": {}},
    ],
)
def test_progress_loader_requires_exact_contextual_top_level_schema(tmp_path, payload):
    progress = tmp_path / "journal_rebuild_acs_state.json"
    progress.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError) as error:
        _load_progress(progress, "acs")

    assert "journal rebuild progress" in str(error.value)
    assert str(progress) in str(error.value)


def test_progress_loader_wraps_malformed_json_with_path_context(tmp_path):
    progress = tmp_path / "journal_rebuild_acs_state.json"
    progress.write_text("not-json", encoding="utf-8")

    with pytest.raises(RuntimeError, match=r"progress.*journal_rebuild_acs_state\.json.*JSON"):
        _load_progress(progress, "acs")


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

    monkeypatch.setenv("PAPERTRACK_CROSSREF_MODE", "metadata")
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


def test_rebuild_runner_defaults_to_official_only_without_crossref_environment(
    tmp_path, monkeypatch
):
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

    monkeypatch.delenv("PAPERTRACK_CROSSREF_MODE", raising=False)
    monkeypatch.setattr(module, "filter_journal_to_md", rebuild)

    run_provider_rebuild(
        "acs", (_candidate(),), tmp_path / "data", tmp_path / "diagnostics"
    )

    assert calls[0]["source_policy"].crossref_mode is CrossrefMode.DISABLED


def test_successful_rebuild_preserves_exact_collected_doi_membership(
    tmp_path, monkeypatch
):
    from papertrack import report
    from papertrack.adapter_result import AdapterResult, SUCCESS
    from papertrack.journal_entry import JournalEntry

    _configure(monkeypatch)
    path = tmp_path / "data" / "acs" / "jcim" / "66" / "12.md"
    path.parent.mkdir(parents=True)
    collected_doi = "10.1021/acs.jcim.1c00001"
    new_doi = "10.1021/acs.jcim.1c00002"
    path.write_text(
        _report((collected_doi, "Previously collected", "Old abstract")),
        encoding="utf-8",
    )
    entries = {
        collected_doi: JournalEntry(
            doi=collected_doi,
            title="Updated collected article",
            authors=["A. Author"],
            abstract="Updated abstract",
            date="2026",
        ),
        new_doi: JournalEntry(
            doi=new_doi,
            title="New ordinary article",
            authors=["B. Author"],
            abstract="New abstract",
            date="2026",
        ),
    }
    monkeypatch.setattr(
        report,
        "query_journal_issue_full_result",
        lambda *args, **kwargs: AdapterResult(
            entries=entries,
            outcome=SUCCESS,
            provider="acs",
            source="journal",
        ),
    )

    summary = run_provider_rebuild(
        "acs",
        (_candidate("raw_html"),),
        tmp_path / "data",
        tmp_path / "diagnostics",
    )

    rendered = path.read_text(encoding="utf-8")
    collected = rendered.split("## collected", 1)[1].split("## not collected", 1)[0]
    not_collected = rendered.split("## not collected", 1)[1]
    assert f"### {collected_doi}" in collected
    assert f"### {new_doi}" not in collected
    assert f"### {new_doi}" in not_collected
    assert summary.resolved_keys == (_candidate("raw_html").key,)


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


def test_failed_rebuild_atomically_restores_from_same_directory_backup(
    tmp_path, monkeypatch
):
    module = _configure(monkeypatch)
    path = tmp_path / "data" / "acs" / "jcim" / "66" / "12.md"
    path.parent.mkdir(parents=True)
    original = b"durable exact bytes\n"
    path.write_bytes(original)
    report_replacements = []
    events = []
    real_replace = module.os.replace

    def track_replace(source, target):
        if Path(target) == path:
            report_replacements.append((Path(source), Path(target)))
            events.append(("replace", path.parent))
        real_replace(source, target)

    def rebuild(**kwargs):
        events.append(("dispatch", path.parent))
        path.write_text("partial replacement", encoding="utf-8")
        return False

    monkeypatch.setattr(
        module,
        "_fsync_directory",
        lambda directory: events.append(("directory_fsync", Path(directory))),
        raising=False,
    )
    monkeypatch.setattr(module.os, "replace", track_replace)
    monkeypatch.setattr(module, "filter_journal_to_md", rebuild)

    run_provider_rebuild(
        "acs", (_candidate(),), tmp_path / "data", tmp_path / "diagnostics"
    )

    assert len(report_replacements) == 1
    backup, target = report_replacements[0]
    assert backup.parent == path.parent
    assert target == path
    assert path.read_bytes() == original
    assert not backup.exists()
    assert events == [
        ("directory_fsync", path.parent),
        ("dispatch", path.parent),
        ("replace", path.parent),
        ("directory_fsync", path.parent),
    ]


def test_restore_replace_failure_keeps_durable_backup_and_aborts_progress(
    tmp_path, monkeypatch
):
    module = _configure(monkeypatch)
    path = tmp_path / "data" / "acs" / "jcim" / "66" / "12.md"
    path.parent.mkdir(parents=True)
    original = b"recoverable original bytes\n"
    path.write_bytes(original)
    directory_fsyncs = []
    real_replace = module.os.replace

    def fail_report_restore(source, target):
        if Path(target) == path:
            raise OSError("simulated atomic restore failure")
        real_replace(source, target)

    def rebuild(**kwargs):
        path.write_text("partial replacement", encoding="utf-8")
        return False

    monkeypatch.setattr(module.os, "replace", fail_report_restore)
    monkeypatch.setattr(
        module,
        "_fsync_directory",
        lambda directory: directory_fsyncs.append(Path(directory)),
        raising=False,
    )
    monkeypatch.setattr(module, "filter_journal_to_md", rebuild)

    with pytest.raises(OSError, match="simulated atomic restore failure"):
        run_provider_rebuild(
            "acs", (_candidate(),), tmp_path / "data", tmp_path / "diagnostics"
        )

    backups = list(path.parent.glob(f".{path.name}.papertrack-rebuild-*.bak"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original
    assert path.read_text(encoding="utf-8") == "partial replacement"
    assert not (tmp_path / "diagnostics" / "journal_rebuild_acs_state.json").exists()
    assert directory_fsyncs == [path.parent]


def test_verified_success_removes_outer_rebuild_backup(tmp_path, monkeypatch):
    module = _configure(monkeypatch)
    path = tmp_path / "data" / "acs" / "jcim" / "66" / "12.md"
    path.parent.mkdir(parents=True)
    path.write_text(_report(("10.1021/old", "Old", "Abstract")), encoding="utf-8")
    events = []

    def rebuild(**kwargs):
        events.append("dispatch")
        path.write_text(
            _report(("10.1021/new", "New", "Abstract")),
            encoding="utf-8",
        )
        return True

    monkeypatch.setattr(module, "filter_journal_to_md", rebuild)
    monkeypatch.setattr(
        module,
        "_fsync_directory",
        lambda directory: events.append(("directory_fsync", Path(directory))),
        raising=False,
    )

    run_provider_rebuild(
        "acs", (_candidate("raw_html"),), tmp_path / "data", tmp_path / "diagnostics"
    )

    assert not list(path.parent.glob(f".{path.name}.papertrack-rebuild-*.bak"))
    assert events == [
        ("directory_fsync", path.parent),
        "dispatch",
        ("directory_fsync", path.parent),
    ]


def test_truthy_rebuild_without_output_restores_original_and_retries(
    tmp_path, monkeypatch, caplog
):
    module = _configure(monkeypatch)
    path = tmp_path / "data" / "acs" / "jcim" / "66" / "12.md"
    path.parent.mkdir(parents=True)
    original = b"original report bytes\n"
    path.write_bytes(original)
    calls = 0

    def rebuild(**kwargs):
        nonlocal calls
        calls += 1
        path.unlink()
        return True

    monkeypatch.setattr(module, "filter_journal_to_md", rebuild)
    candidate = _candidate()
    caplog.set_level("WARNING")

    first = run_provider_rebuild(
        "acs", (candidate,), tmp_path / "data", tmp_path / "diagnostics"
    )
    progress_path = tmp_path / "diagnostics" / "journal_rebuild_acs_state.json"
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    second = run_provider_rebuild(
        "acs", (candidate,), tmp_path / "data", tmp_path / "diagnostics"
    )

    assert first.outcomes == {candidate.key: FETCH_FAILED}
    assert second.outcomes == {candidate.key: FETCH_FAILED}
    assert progress["issues"][candidate.key]["outcome"] == FETCH_FAILED
    assert path.read_bytes() == original
    assert calls == 2
    assert str(path) in caplog.text
    assert "did not produce the expected report" in caplog.text
    assert not list(path.parent.glob(f".{path.name}.papertrack-rebuild-*.bak"))


def test_truthy_rebuild_without_new_output_records_failure_and_retries(
    tmp_path, monkeypatch
):
    module = _configure(monkeypatch)
    path = tmp_path / "data" / "acs" / "jcim" / "66" / "12.md"
    calls = 0

    def rebuild(**kwargs):
        nonlocal calls
        calls += 1
        assert not path.exists()
        return True

    monkeypatch.setattr(module, "filter_journal_to_md", rebuild)
    candidate = _candidate()

    first = run_provider_rebuild(
        "acs", (candidate,), tmp_path / "data", tmp_path / "diagnostics"
    )
    progress_path = tmp_path / "diagnostics" / "journal_rebuild_acs_state.json"
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    second = run_provider_rebuild(
        "acs", (candidate,), tmp_path / "data", tmp_path / "diagnostics"
    )

    assert first.outcomes == {candidate.key: FETCH_FAILED}
    assert second.outcomes == {candidate.key: FETCH_FAILED}
    assert progress["issues"][candidate.key]["outcome"] == FETCH_FAILED
    assert not path.exists()
    assert calls == 2


def test_assessment_exception_restores_before_recording_fetch_failure(
    tmp_path, monkeypatch
):
    module = _configure(monkeypatch)
    path = tmp_path / "data" / "acs" / "jcim" / "66" / "12.md"
    path.parent.mkdir(parents=True)
    original = b"assessment recovery bytes\n"
    path.write_bytes(original)

    def rebuild(**kwargs):
        path.write_text(
            _report(("10.1021/new", "New article", "Abstract")),
            encoding="utf-8",
        )
        return True

    monkeypatch.setattr(module, "filter_journal_to_md", rebuild)
    monkeypatch.setattr(
        module,
        "_assess_report",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("assessment exploded")
        ),
    )
    candidate = _candidate("raw_html")

    summary = run_provider_rebuild(
        "acs", (candidate,), tmp_path / "data", tmp_path / "diagnostics"
    )

    assert summary.outcomes == {candidate.key: FETCH_FAILED}
    assert path.read_bytes() == original
    assert not list(path.parent.glob(f".{path.name}.papertrack-rebuild-*.bak"))


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
            "raw_mathml",
            (("10.1021/acs.jcim.1c00001", "Ordinary article", "<mml:mi>x</mml:mi>"),),
        ),
        (
            "raw_mathml",
            (("10.1021/acs.jcim.1c00001", "Ordinary article", "x<msub>i</msub>"),),
        ),
        (
            "raw_mathml",
            (("10.1021/acs.jcim.1c00001", "Ordinary article", "x<mi>i</mi>"),),
        ),
        *[
            (
                "raw_mathml",
                ((
                    "10.1021/acs.jcim.1c00001",
                    "Ordinary article",
                    f"x<{tag}>i</{tag}>",
                ),),
            )
            for tag in (
                "mglyph",
                "mstack",
                "mscarries",
                "mscarry",
                "msgroup",
                "msline",
                "msrow",
            )
        ],
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


def test_raw_mathml_check_does_not_flag_ordinary_prose(tmp_path, monkeypatch):
    module = _configure(monkeypatch)
    path = tmp_path / "data" / "acs" / "jcim" / "66" / "12.md"

    def rebuild(**kwargs):
        path.parent.mkdir(parents=True)
        path.write_text(
            _report(
                (
                    "10.1021/acs.jcim.1c00001",
                    "Ordinary article",
                    "We use mi, mo, and msub as prose with <mark>highlighting</mark>.",
                )
            ),
            encoding="utf-8",
        )
        return True

    monkeypatch.setattr(module, "filter_journal_to_md", rebuild)
    candidate = _candidate("raw_mathml")

    summary = run_provider_rebuild(
        "acs", (candidate,), tmp_path / "data", tmp_path / "diagnostics"
    )

    assert summary.resolved_keys == (candidate.key,)


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

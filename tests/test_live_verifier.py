import importlib.util
from pathlib import Path

from papertrack.adapter_result import AdapterResult, BLOCKED, SUCCESS
from papertrack.source_policy import CrossrefMode


def _load_verifier():
    path = Path(__file__).resolve().parents[1] / "scripts" / "verify_official_journal_issues.py"
    spec = importlib.util.spec_from_file_location("papertrack_live_verifier", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_verifier_runs_only_selected_provider(capsys):
    verifier = _load_verifier()
    calls = []
    fetchers = {
        provider: (
            lambda provider=provider: calls.append(provider)
            or AdapterResult(
                entries={"10.example/article": object()},
                outcome=SUCCESS,
                provider=provider,
                source="journal",
            )
        )
        for provider in ("acs", "aip", "aps")
    }

    exit_code = verifier.verify(["aps"], fetchers=fetchers)

    assert exit_code == 0
    assert calls == ["aps"]
    assert verifier.SOURCE_POLICY.crossref_mode is CrossrefMode.DISABLED
    assert '"provider": "aps"' in capsys.readouterr().out


def test_verifier_returns_nonzero_for_block_or_empty_entries():
    verifier = _load_verifier()
    blocked = AdapterResult(
        entries={},
        outcome=BLOCKED,
        provider="aip",
        source="journal",
    )
    empty_success = AdapterResult(
        entries={},
        outcome=SUCCESS,
        provider="acs",
        source="journal",
    )

    assert verifier.verify(["aip"], fetchers={"aip": lambda: blocked}) == 1
    assert verifier.verify(["acs"], fetchers={"acs": lambda: empty_success}) == 1


def test_verifier_source_has_no_report_state_crossref_or_zotero_imports():
    path = Path(__file__).resolve().parents[1] / "scripts" / "verify_official_journal_issues.py"
    source = path.read_text(encoding="utf-8")

    assert "papertrack.report" not in source
    assert "import papertrack.crossref" not in source
    assert "from papertrack import crossref" not in source
    assert "zotero" not in source.lower()
    assert ".papertrack_state" not in source

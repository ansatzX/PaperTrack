import pytest

from papertrack.diagnostics import configure_diagnostics


@pytest.fixture(autouse=True)
def isolate_diagnostics(tmp_path, monkeypatch):
    monkeypatch.delenv("PAPERTRACK_DIAGNOSTICS_DIR", raising=False)
    monkeypatch.delenv("PAPERTRACK_DIAGNOSTICS_ALL_ATTEMPTS", raising=False)
    monkeypatch.delenv("PAPERTRACK_NO_FAILURE_SNAPSHOTS", raising=False)
    configure_diagnostics(diagnostics_dir=str(tmp_path / "diagnostics"))
    yield
    configure_diagnostics()


@pytest.fixture(autouse=True)
def isolate_metadata_network(monkeypatch):
    from papertrack import metadata_sources, journal_fetch
    monkeypatch.setattr(metadata_sources, "request_json", lambda *args, **kwargs: {})
    monkeypatch.setattr(journal_fetch, "_crossref_get", lambda *args, **kwargs: {})
    from papertrack.adapter_result import AdapterResult, FETCH_FAILED
    monkeypatch.setattr(journal_fetch, "_pubmed_issue_result", lambda *args, **kwargs:
                        AdapterResult(entries={}, outcome=FETCH_FAILED, provider="pubmed", source="journal"))

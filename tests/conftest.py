import pytest

from papertrack.diagnostics import configure_diagnostics


@pytest.fixture(autouse=True)
def isolate_diagnostics(tmp_path, monkeypatch):
    # Runtime caches are cwd-owned; never let offline tests inspect production runs.
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PAPERTRACK_DIAGNOSTICS_DIR", raising=False)
    monkeypatch.delenv("PAPERTRACK_DIAGNOSTICS_ALL_ATTEMPTS", raising=False)
    monkeypatch.delenv("PAPERTRACK_NO_FAILURE_SNAPSHOTS", raising=False)
    configure_diagnostics(diagnostics_dir=str(tmp_path / "diagnostics"))
    yield
    configure_diagnostics()

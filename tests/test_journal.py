from papertrack.cli import _journal_output_root


def test_jcp_provider_outputs_under_aip_folder(tmp_path):
    cfg = {"name": "Journal of Chemical Physics", "slug": "jcp", "provider": "aip"}

    assert _journal_output_root(str(tmp_path), cfg) == str(tmp_path / "aip")


def test_acs_journal_outputs_under_acs_folder(tmp_path):
    cfg = {"name": "Journal of Chemical Theory and Computation", "slug": "jctc", "acs_code": "jctcce"}

    assert _journal_output_root(str(tmp_path), cfg) == str(tmp_path / "acs")


def test_aps_journal_outputs_under_aps_folder(tmp_path):
    cfg = {"name": "Physical Review Letters", "slug": "prl", "provider": "aps"}

    assert _journal_output_root(str(tmp_path), cfg) == str(tmp_path / "aps")


def test_builtin_journal_configs_include_jcim_jacs_and_prl():
    from papertrack.config import get_journals

    journals = get_journals()

    assert journals["jcim"]["acs_code"] == "jcisd8"
    assert journals["jacs"]["acs_code"] == "jacsat"
    assert journals["prl"]["provider"] == "aps"
    assert journals["prl"]["issn"] == "0031-9007"

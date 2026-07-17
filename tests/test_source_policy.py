import pytest

from papertrack.source_policy import CrossrefMode, SourcePolicy


def test_source_policy_defaults_to_official_only():
    policy = SourcePolicy.from_environment({})

    assert policy.crossref_mode is CrossrefMode.DISABLED
    assert policy.crossref_metadata_enabled is False
    assert policy.crossref_discovery_enabled is False


def test_source_policy_metadata_mode_enables_crossref_services():
    policy = SourcePolicy.from_environment(
        {"PAPERTRACK_CROSSREF_MODE": "metadata"}
    )

    assert policy.crossref_mode is CrossrefMode.METADATA
    assert policy.crossref_metadata_enabled is True
    assert policy.crossref_discovery_enabled is True


def test_source_policy_rejects_unknown_mode():
    with pytest.raises(RuntimeError, match="PAPERTRACK_CROSSREF_MODE"):
        SourcePolicy.from_environment(
            {"PAPERTRACK_CROSSREF_MODE": "membership"}
        )


def test_source_policy_accepts_blank_as_disabled():
    policy = SourcePolicy.from_environment({"PAPERTRACK_CROSSREF_MODE": ""})

    assert policy.crossref_mode is CrossrefMode.DISABLED

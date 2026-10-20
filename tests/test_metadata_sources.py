import pytest

from papertrack.journals import metadata as sources
from papertrack.journals.entry import JournalEntry


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (" HTTPS://DOI.ORG/10.1234/ABC ", "10.1234/abc"),
        ("http://dx.doi.org/10.1234/ABC", "10.1234/abc"),
        ("10.1234/a(b).c", "10.1234/a(b).c"),
        ("10.1234/with space", ""),
        ("https://publisher.example/article", ""),
        (None, ""),
    ],
)
def test_capture_doi_normalization(value, expected):
    assert sources.normalize_doi(value) == expected


def test_stamp_preserves_field_sources_and_deduplicates_membership():
    entry = JournalEntry(
        doi="10.1234/a", title="Official title", authors=["Author"],
        field_sources={"title": "official_issue"},
    )

    sources.stamp(entry, "official_browser")
    sources.stamp(entry, "official_browser")

    assert entry.field_sources == {
        "title": "official_issue", "authors": "official_browser",
    }
    assert entry.membership_sources == ["official_browser"]

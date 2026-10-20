from papertrack.arxiv.entry import ArxivEntry
from papertrack.identity import (
    EvidenceHint,
    Identity,
    MatchResult,
    WorkIdentitySet,
    identities_for_arxiv_entry,
    identities_for_journal_entry,
)
from papertrack.journals.entry import JournalEntry


def test_arxiv_entry_identity_set_contains_exact_identities_and_hints():
    entry = ArxivEntry(
        arxiv_id="arXiv:2502.07673",
        title="A preprint",
        authors=["A Author"],
        abstract="Abstract",
        external_doi="10.1103/PhysRevLett.134.010201",
        journal_ref="Phys. Rev. Lett. 134, 010201 (2025)",
    )

    identity_set = identities_for_arxiv_entry(entry)

    assert Identity("arxiv_doi", "10.48550/arXiv.2502.07673", source="arxiv") in (
        identity_set.exact_identities
    )
    assert Identity("external_doi", "10.1103/PhysRevLett.134.010201", source="arxiv") in (
        identity_set.exact_identities
    )
    assert Identity("arxiv_url", "https://arxiv.org/abs/2502.07673", source="arxiv") in (
        identity_set.exact_identities
    )
    assert Identity("arxiv_id", "2502.07673", source="arxiv") in identity_set.exact_identities
    assert EvidenceHint("journal_ref", "Phys. Rev. Lett. 134, 010201 (2025)", source="arxiv") in (
        identity_set.evidence_hints
    )
    assert EvidenceHint("title", "A preprint", source="arxiv") in identity_set.evidence_hints


def test_journal_entry_identity_set_uses_publisher_doi():
    entry = JournalEntry(
        doi="10.1063/5.0323573",
        title="A journal paper",
        authors=["A Author"],
        volume="164",
        issue="16",
        pages="164101",
    )

    identity_set = identities_for_journal_entry(entry)

    assert identity_set.exact_identities == [
        Identity("publisher_doi", "10.1063/5.0323573", source="journal")
    ]
    assert EvidenceHint("title", "A journal paper", source="journal") in identity_set.evidence_hints
    assert EvidenceHint("pages", "164101", source="journal") in identity_set.evidence_hints


def test_match_result_collected_requires_exact_auto_match():
    exact = MatchResult(
        matched=True,
        auto_classifiable=True,
        confidence="exact",
        match_kind="external_doi",
        zotero_key="A1",
        zotero_item_type="journalArticle",
        canonical_identity_used="10.1103/example",
    )
    hint = MatchResult(
        matched=True,
        auto_classifiable=False,
        confidence="hint",
        match_kind="title",
        zotero_key="B2",
        zotero_item_type="journalArticle",
        canonical_identity_used="A title",
    )
    none = MatchResult()

    assert exact.is_collected is True
    assert hint.is_collected is False
    assert none.is_collected is False


def test_work_identity_set_evidence_hints_do_not_create_exact_identities():
    identity_set = WorkIdentitySet(
        exact_identities=[],
        evidence_hints=[EvidenceHint("title", "Possible same title", source="crossref")],
    )

    assert identity_set.exact_identities == []
    assert identity_set.evidence_hints[0].kind == "title"

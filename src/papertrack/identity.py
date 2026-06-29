"""Work identity extraction and collection-match result types.

This module is intentionally independent from arXiv, journal, and Zotero
fetching. Source-specific entries become a WorkIdentitySet first; collection
systems such as Zotero then match exact identities and may record weak hints
without letting those hints automatically mark a work as collected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .arxiv_entry import ArxivEntry
    from .journal_entry import JournalEntry


@dataclass(frozen=True)
class Identity:
    kind: str
    value: str
    source: str = ""


@dataclass(frozen=True)
class EvidenceHint:
    kind: str
    value: str
    source: str = ""


@dataclass
class WorkIdentitySet:
    exact_identities: list[Identity] = field(default_factory=list)
    evidence_hints: list[EvidenceHint] = field(default_factory=list)


@dataclass
class MatchResult:
    matched: bool = False
    auto_classifiable: bool = False
    confidence: str = "none"
    match_kind: str = ""
    zotero_key: str = ""
    zotero_item_type: str = ""
    canonical_identity_used: str = ""

    @property
    def is_collected(self) -> bool:
        return self.matched and self.auto_classifiable and self.confidence == "exact"


def normalize_doi(value: str) -> str:
    return value.strip().lower()


def identities_for_arxiv_entry(entry: "ArxivEntry") -> WorkIdentitySet:
    exact = [
        Identity("arxiv_doi", entry.arxiv_doi, source="arxiv"),
        Identity("arxiv_url", entry.arxiv_url, source="arxiv"),
        Identity("arxiv_id", entry.bare_id, source="arxiv"),
    ]
    if entry.external_doi:
        # Put the publisher DOI before weaker URL/ID identities when matching.
        exact.insert(1, Identity("external_doi", entry.external_doi, source="arxiv"))

    hints = [
        EvidenceHint("title", entry.title, source="arxiv"),
    ]
    if entry.authors:
        hints.append(EvidenceHint("authors", ", ".join(entry.authors), source="arxiv"))
    if entry.journal_ref:
        hints.append(EvidenceHint("journal_ref", entry.journal_ref, source="arxiv"))
    if entry.subjects:
        hints.append(EvidenceHint("subjects", ", ".join(entry.subjects), source="arxiv"))
    if entry.comments:
        hints.append(EvidenceHint("comments", entry.comments, source="arxiv"))

    return WorkIdentitySet(exact_identities=exact, evidence_hints=hints)


def identities_for_journal_entry(entry: "JournalEntry") -> WorkIdentitySet:
    exact = []
    if entry.doi:
        exact.append(Identity("publisher_doi", entry.doi, source="journal"))

    hints = [
        EvidenceHint("title", entry.title, source="journal"),
    ]
    if entry.authors:
        hints.append(EvidenceHint("authors", ", ".join(entry.authors), source="journal"))
    if entry.journal_name:
        hints.append(EvidenceHint("journal_name", entry.journal_name, source="journal"))
    if entry.volume:
        hints.append(EvidenceHint("volume", entry.volume, source="journal"))
    if entry.issue:
        hints.append(EvidenceHint("issue", entry.issue, source="journal"))
    if entry.pages:
        hints.append(EvidenceHint("pages", entry.pages, source="journal"))
    if entry.date:
        hints.append(EvidenceHint("date", entry.date, source="journal"))

    return WorkIdentitySet(exact_identities=exact, evidence_hints=hints)

"""Local DOI normalization and official capture provenance; no network access."""
import re

from papertrack.journals.entry import JournalEntry

FIELDS = ("title", "authors", "abstract", "journal_name", "volume", "issue", "pages", "date")


def normalize_doi(value: str) -> str:
    if not isinstance(value, str):
        return ""
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", (value or "").strip(), flags=re.I).lower()
    return value if re.fullmatch(r"10\.\d{4,9}/\S+", value) else ""


def stamp(entry: JournalEntry, source: str) -> None:
    for field in FIELDS:
        if getattr(entry, field):
            entry.field_sources.setdefault(field, source)
    if source not in entry.membership_sources:
        entry.membership_sources.append(source)

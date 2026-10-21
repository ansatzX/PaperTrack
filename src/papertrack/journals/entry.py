import re
from dataclasses import dataclass, field


def validate_issue_placement(volume: str, issue: str) -> None:
    """Formal volume/issue identifiers are numeric path components."""
    if any(not re.fullmatch(r'[0-9]+', str(value)) or int(value) < 1
           for value in (volume, issue)):
        raise ValueError('Formal volume and issue must be positive ASCII numbers')


@dataclass
class JournalEntry:
    doi: str
    title: str
    authors: list[str]
    abstract: str = ""
    journal_name: str = ""
    volume: str = ""
    issue: str = ""
    pages: str = ""
    date: str = ""
    toc_image_url: str = ""

    field_sources: dict[str, str] = field(default_factory=dict)
    membership_sources: list[str] = field(default_factory=list)

    @property
    def bare_doi(self) -> str:
        return self.doi.strip()

    @property
    def doi_url(self) -> str:
        return f"https://doi.org/{self.doi}"

    @staticmethod
    def is_non_article(doi: str, title: str = "") -> bool:
        """Exclude issue mastheads, retaining DOI-bearing correction notices."""
        if re.search(r"(?:ctv|civ)\d{3}i\d{3}", doi):
            return True
        if re.search(r"issue\s+(?:editorial\s+masthead|publication\s+information)", title, re.IGNORECASE):
            return True
        if re.search(r"\beditorial\s+masthead\b", title, re.IGNORECASE):
            return True
        return False

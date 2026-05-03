import re
from html import unescape
from dataclasses import dataclass


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

    @property
    def bare_doi(self) -> str:
        return self.doi.strip()

    @property
    def doi_url(self) -> str:
        return f"https://doi.org/{self.doi}"

    @classmethod
    def from_crossref(cls, item: dict, journal_name: str = "") -> "JournalEntry":
        title = ""
        titles = item.get("title", [])
        if titles:
            title = titles[0]

        authors = []
        for a in item.get("author", []):
            given = a.get("given", "")
            family = a.get("family", "")
            name = f"{given} {family}".strip()
            if name:
                authors.append(name)

        abstract = cls._clean_crossref_abstract(item.get("abstract", "") or "")

        volume = item.get("volume", "") or ""
        issue = item.get("issue", "") or ""
        pages = item.get("page", "") or ""

        date = ""
        pub = item.get("published-print") or item.get("published-online") or {}
        parts = pub.get("date-parts", [[]])[0]
        if len(parts) == 3:
            date = f"{parts[0]}-{parts[1]:02d}-{parts[2]:02d}"
        elif parts:
            date = "-".join(str(p) for p in parts)

        return cls(
            doi=item.get("DOI", ""),
            title=title,
            authors=authors,
            abstract=abstract,
            journal_name=journal_name,
            volume=volume,
            issue=issue,
            pages=pages,
            date=date,
        )

    @staticmethod
    def is_non_article(doi: str, title: str = "") -> bool:
        """Filter out issue metadata and correction notices."""
        if re.search(r"ctv\d{3}i\d{3}", doi):
            return True
        return bool(re.match(r"\s*erratum\b", title, re.IGNORECASE))

    @staticmethod
    def _clean_crossref_abstract(raw: str) -> str:
        """Convert CrossRef JATS/HTML abstracts into plain text."""
        text = re.sub(r"</?jats:p[^>]*>", " ", raw)
        text = re.sub(r"<[^>]+>", " ", text)
        return re.sub(r"\s+", " ", unescape(text)).strip()

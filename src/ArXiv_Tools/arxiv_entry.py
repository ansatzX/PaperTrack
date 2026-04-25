import re
from dataclasses import dataclass, field

_ARXIV_URL_RE = re.compile(r"arxiv\.org/abs/(\d+\.\d+)")


@dataclass
class ArxivEntry:
    arxiv_id: str
    title: str
    authors: list[str]
    abstract: str
    external_doi: str = ""
    external_doi_url: str = ""
    journal_ref: str = ""
    comments: str = ""
    subjects: list[str] = field(default_factory=list)

    @property
    def bare_id(self) -> str:
        """arXiv ID without the 'arXiv:' prefix (e.g. '2502.07673').

        Used as the lookup key when matching against Zotero items that store
        the arXiv URL but not the DOI.
        """
        return self.arxiv_id.replace("arXiv:", "").strip()

    @property
    def arxiv_doi(self) -> str:
        """The canonical arXiv DOI: 10.48550/arXiv.XXXX.YYYYY"""
        return f"10.48550/arXiv.{self.bare_id}"

    @property
    def arxiv_url(self) -> str:
        """Full abs page URL: https://arxiv.org/abs/XXXX.YYYYY"""
        return f"https://arxiv.org/abs/{self.bare_id}"

    @staticmethod
    def bare_id_from_url(url: str) -> str | None:
        """Extract bare arXiv ID from an arxiv.org/abs/ URL.

        Used when building the URL→ID index from Zotero items,
        since many are stored with the abs URL rather than the DOI.
        """
        m = _ARXIV_URL_RE.search(url)
        return m.group(1) if m else None

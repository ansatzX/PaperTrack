import os
import logging
from typing import TYPE_CHECKING

from pyzotero import zotero as zotero_lib

if TYPE_CHECKING:
    from .arxiv_entry import ArxivEntry

logger = logging.getLogger(__name__)


class ZoteroQuery:

    def __init__(self, library_id: str = "000000", library_type: str = "user",
                 local: bool = True, api_key: str | None = None,
                 api_key_env: str = "ZOTERO_API_KEY"):
        zot = zotero_lib.Zotero(library_id=library_id, library_type=library_type, local=local)
        if not local and api_key is None:
            api_key = os.environ.get(api_key_env)
            if api_key:
                zot.session.headers["Zotero-API-Key"] = api_key
        self.zot = zot
        # Both indexes are lazily built on first lookup to avoid I/O at
        # construction time and to skip the work entirely if the script
        # produces no arXiv results for any day.
        self._doi_index: dict[str, str] | None = None
        self._url_arxiv_index: dict[str, str] | None = None

    def _ensure_index(self):
        """Build in-memory lookup indexes from Zotero top-level items.

        Uses zot.top() rather than zot.items() to exclude child attachments
        and notes, roughly halving the data volume. Two indexes are built:

        _doi_index:     DOI → item_key   (exact match, most reliable)
        _url_arxiv_index: bare_id → item_key  (catches webpage-type items
                        that were added via browser connector without a DOI)
        """
        if self._doi_index is not None:
            return
        logger.info("Building Zotero index from top-level items...")
        self._doi_index = {}
        self._url_arxiv_index = {}
        try:
            items = self.zot.everything(self.zot.top())
        except Exception:
            logger.exception("Failed to fetch Zotero items")
            return
        for item in items:
            data = item.get("data", {})
            key = item.get("key", "")
            doi = data.get("DOI", "")
            if doi:
                self._doi_index[doi] = key
            url = data.get("url", "")
            if url:
                from .arxiv_entry import ArxivEntry
                bare_id = ArxivEntry.bare_id_from_url(url)
                if bare_id:
                    self._url_arxiv_index[bare_id] = key
        logger.info("Zotero index built: %d DOIs, %d arXiv URLs",
                    len(self._doi_index), len(self._url_arxiv_index))

    def find_by_entry(self, entry: "ArxivEntry") -> bool:
        """Check if an arXiv paper exists in the Zotero library.

        Three-layer fallback strategy, ordered by reliability:

        1. arXiv DOI (10.48550/arXiv.XXXX.YYYYY) — exact match
        2. External DOI (publisher DOI from arXiv metadata) — exact match;
           only populated after the paper is formally published.
        3. arXiv ID extracted from Zotero url field — catches items saved
           as webpage/preprint types that lack a DOI field entirely.

        Returns True if any layer matches, False otherwise.
        """
        self._ensure_index()
        if self._doi_index is None:
            return False

        if entry.arxiv_doi and entry.arxiv_doi in self._doi_index:
            return True

        if entry.external_doi and entry.external_doi in self._doi_index:
            return True

        if entry.bare_id and entry.bare_id in self._url_arxiv_index:
            return True

        return False

    def find_by_doi(self, doi: str) -> bool:
        """Check if a DOI exists in the Zotero library.

        Direct DOI lookup for journal articles that already have a publisher DOI.
        """
        self._ensure_index()
        if self._doi_index is None:
            return False
        return bool(doi and doi in self._doi_index)

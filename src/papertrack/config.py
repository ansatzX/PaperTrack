import logging
import os
import re
from copy import deepcopy

from papertrack.reports.tags import arxiv_report_tag, journal_report_tag

try:
    import tomllib
except ImportError:
    import tomli as tomllib

logger = logging.getLogger(__name__)

_TOML_PATH = os.path.join(os.path.dirname(__file__), "categories.toml")
_ARXIV_OFFICIAL_CATEGORY = {"chem-ph": "physics.chem-ph"}


def official_arxiv_category(category: str) -> str:
    """Return the official subject identifier for a ledger category name."""
    return _ARXIV_OFFICIAL_CATEGORY.get(category, category)

# Template for arXiv advanced search parameters. Each category config is a
# dict that gets URL-encoded into the advanced search query string. The
# date-from_date and date-to_date keys are filled in per-query by
# query_arxiv_dict().
quant_ph: dict[str, str] = {
    "advanced": "",
    "terms-0-term": "",
    "terms-0-operator": "AND",
    "terms-0-field": "title",
    "classification-physics": "y",
    "classification-physics_archives": "quant-ph",
    "classification-include_cross_list": "include",
    "date-filter_by": "date_range",
    "date-year": "",
    "date-from_date": "2025-02-01",
    "date-to_date": "2025-02-02",
    "date-date_type": "submitted_date_first",
    "abstracts": "show",
    "size": "200",
    "order": "submitted_date",
}


def _load_toml_categories() -> dict[str, dict[str, str]]:
    """Load arXiv query parameters from the [arxiv.*] sections of categories.toml.

    Returns a flat dict keyed by category name (e.g. 'quant-ph'), not
    by the full TOML path.  Returns empty dict on failure so the caller
    falls back to hardcoded defaults.
    """
    try:
        with open(_TOML_PATH, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        logger.warning("categories.toml not found at %s, using hardcoded defaults", _TOML_PATH)
        return {}
    except Exception:
        logger.exception("Failed to load categories.toml, using defaults")
        return {}

    arxiv = data.get("arxiv", {})
    result: dict[str, dict[str, str]] = {}
    for name, section in arxiv.items():
        result[name] = {str(k): str(v) for k, v in section.items()}
    if result:
        logger.info("Loaded %d arXiv categories from %s", len(result), _TOML_PATH)
    return result


def build_query_args() -> dict[str, dict[str, str]]:
    """Assemble the category → query_args mapping.

    Prefers TOML config over hardcoded defaults so users can add or modify
    categories without editing Python source.
    """
    toml_cats = _load_toml_categories()
    if toml_cats:
        logger.debug("Using TOML categories")
        _validate_category_tag_uniqueness(toml_cats)
        return toml_cats

    logger.debug("Using hardcoded category defaults")
    hep_ex = deepcopy(quant_ph)
    hep_ex["classification-physics_archives"] = "hep-ex"

    hep_lat = deepcopy(quant_ph)
    hep_lat["classification-physics_archives"] = "hep-lat"

    hep_ph = deepcopy(quant_ph)
    hep_ph["classification-physics_archives"] = "hep-ph"

    hep_th = deepcopy(quant_ph)
    hep_th["classification-physics_archives"] = "hep-th"

    chem_ph = deepcopy(quant_ph)
    chem_ph["classification-physics_archives"] = "physics"
    chem_ph["terms-1-term"] = "physics.chem-ph"
    chem_ph["terms-1-operator"] = "AND"
    chem_ph["terms-1-field"] = "all"

    cs_ai = deepcopy(quant_ph)
    cs_ai.pop("classification-physics", None)
    cs_ai.pop("classification-physics_archives", None)
    cs_ai["terms-1-term"] = "cs.AI"
    cs_ai["terms-1-operator"] = "AND"
    cs_ai["terms-1-field"] = "all"
    cs_ai["classification-computer_science"] = "y"

    cs_lg = deepcopy(cs_ai)
    cs_lg["terms-1-term"] = "cs.LG"

    categories = {
        "quant-ph": quant_ph,
        "hep-ex": hep_ex,
        "hep-lat": hep_lat,
        "hep-ph": hep_ph,
        "hep-th": hep_th,
        "chem-ph": chem_ph,
        "cs.AI": cs_ai,
        "cs.LG": cs_lg,
    }
    _validate_category_tag_uniqueness(categories)
    return categories


def _validate_category_tag_uniqueness(
    categories: dict[str, dict[str, str]],
) -> None:
    owners: dict[str, str] = {}
    for category in categories:
        normalized = arxiv_report_tag(category, "2000-01-01")
        _validate_generated_tag(normalized, f"arXiv category {category!r}")
        previous = owners.get(normalized)
        if previous is not None:
            raise ValueError(
                "generated report tag collision between "
                f"arXiv categories {previous!r} and {category!r}"
            )
        owners[normalized] = category


def _validate_generated_tag(tag: str, owner: str) -> None:
    if not re.fullmatch(r"[a-z0-9_/-]+", tag):
        raise ValueError(f"invalid generated report tag for {owner}: {tag!r}")


# Categories are lazily loaded so that importing papertrack.config (e.g. for
# tests or utility functions) doesn't trigger filesystem I/O. The proxy
# preserves backwards-compatible query_args[cat_] dict-like access.
_query_args: dict[str, dict[str, str]] | None = None


def get_query_args() -> dict[str, dict[str, str]]:
    global _query_args
    if _query_args is None:
        _query_args = build_query_args()
    return _query_args


class _QueryArgsProxy:
    """Dict-like proxy that defers to get_query_args() on every access.

    Exists so that callers can write query_args['quant-ph'] without
    knowing about the lazy-initialisation machinery underneath.
    """

    def __getitem__(self, key: str) -> dict[str, str]:
        return get_query_args()[key]

    def keys(self):
        return get_query_args().keys()

    def values(self):
        return get_query_args().values()

    def items(self):
        return get_query_args().items()

    def __iter__(self):
        return iter(get_query_args())


query_args = _QueryArgsProxy()


# ── Journal configs ────────────────────────────────────────────────────


def _load_toml_journals() -> dict[str, dict[str, str]]:
    """Load journal entries from the [journals] section of categories.toml."""
    try:
        with open(_TOML_PATH, "rb") as f:
            data = tomllib.load(f)
    except (FileNotFoundError, Exception):
        return {}

    journals = data.get("journals", {})
    result: dict[str, dict[str, str]] = {}
    for name, section in journals.items():
        result[name] = {str(k): str(v) for k, v in section.items()}
    if result:
        logger.info("Loaded %d journals from %s", len(result), _TOML_PATH)
    return result


_journals: dict[str, dict[str, str]] | None = None


def get_journals() -> dict[str, dict[str, str]]:
    global _journals
    if _journals is None:
        loaded = _load_toml_journals()
        _validate_journal_tag_uniqueness(loaded)
        _journals = loaded
    return _journals


def _validate_journal_tag_uniqueness(
    journals: dict[str, dict[str, str]],
) -> None:
    owners: dict[str, str] = {}
    for name, config in journals.items():
        slug = config.get("slug", name)
        normalized = journal_report_tag(slug, "1", "1")
        _validate_generated_tag(normalized, f"journal {name!r}")
        previous = owners.get(normalized)
        if previous is not None:
            raise ValueError(
                "generated report tag collision between "
                f"journals {previous!r} and {name!r}"
            )
        owners[normalized] = name


class _JournalProxy:
    def __getitem__(self, key: str) -> dict[str, str]:
        return get_journals()[key]

    def keys(self):
        return get_journals().keys()

    def values(self):
        return get_journals().values()

    def items(self):
        return get_journals().items()

    def __iter__(self):
        return iter(get_journals())


journal_configs = _JournalProxy()

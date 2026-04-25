import logging
import os
from copy import deepcopy

try:
    import tomllib
except ImportError:
    import tomli as tomllib

logger = logging.getLogger(__name__)

_TOML_PATH = os.path.join(os.path.dirname(__file__), "categories.toml")

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
    """Load category query parameters from the packaged categories.toml.

    Returns an empty dict if the file is missing or malformed, which
    signals the caller to fall back to hardcoded defaults. This ensures
    the tool still works even if the TOML file is somehow deleted.
    """
    try:
        with open(_TOML_PATH, "rb") as f:
            data = tomllib.load(f)
        result: dict[str, dict[str, str]] = {}
        for name, section in data.items():
            result[name] = {str(k): str(v) for k, v in section.items()}
        logger.info("Loaded %d categories from %s", len(result), _TOML_PATH)
        return result
    except FileNotFoundError:
        logger.warning("categories.toml not found at %s, using hardcoded defaults", _TOML_PATH)
        return {}
    except Exception:
        logger.exception("Failed to load categories.toml, using defaults")
        return {}


def build_query_args() -> dict[str, dict[str, str]]:
    """Assemble the category → query_args mapping.

    Prefers TOML config over hardcoded defaults so users can add or modify
    categories without editing Python source.
    """
    toml_cats = _load_toml_categories()
    if toml_cats:
        logger.debug("Using TOML categories")
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
    chem_ph["terms-1-operator"] = "AND"
    chem_ph["terms-1-term"] = "physics.chem-ph"
    chem_ph["terms-1-field"] = "all"

    return {
        "quant-ph": quant_ph,
        "hep-ex": hep_ex,
        "hep-lat": hep_lat,
        "hep-ph": hep_ph,
        "hep-th": hep_th,
        "chem-ph": chem_ph,
    }


# Categories are lazily loaded so that importing ArXiv_Tools.codex (e.g. for
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

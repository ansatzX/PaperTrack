import os
import re
import calendar
import logging
from datetime import datetime, timedelta

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .arxiv_entry import ArxivEntry
from .arxiv_index_fetch import query_arxiv_dict
from .zotero_query import ZoteroQuery

logger = logging.getLogger(__name__)

# Jinja2 environment is lazily initialised to avoid filesystem access at
# import time — useful when the module is imported for tests or utilities.
_jinja_env: Environment | None = None


def _get_jinja_env() -> Environment:
    global _jinja_env
    if _jinja_env is None:
        _jinja_env = Environment(
            loader=FileSystemLoader(os.path.join(os.path.dirname(__file__), "templates")),
            autoescape=select_autoescape(),
        )
    return _jinja_env


def _render_paper(entry: ArxivEntry) -> str:
    return _get_jinja_env().get_template("paper.md.j2").render(entry=entry)


def _render_report(date_string: str, category: str,
                   collected_papers: list[str],
                   not_collected_papers: list[str],
                   new_data: list[str]) -> str:
    tmpl = _get_jinja_env().get_template("report.md.j2")
    return tmpl.render(
        date_string=date_string,
        category=category,
        collected_papers=collected_papers,
        not_collected_papers=not_collected_papers,
        new_data=new_data,
    )


def _classify_papers(arxiv_dict: dict[str, ArxivEntry],
                     zotero: ZoteroQuery | None) -> tuple[list[str], list[str]]:
    """Split papers into 'collected' (found in Zotero) and 'not collected'.

    When Zotero is unavailable (zotero=None), all papers go to not_collected
    rather than failing — the user still gets a report, just without the
    collection-status annotations.
    """
    collected: list[str] = []
    not_collected: list[str] = []

    for arxiv_id, entry in arxiv_dict.items():
        if zotero is not None:
            try:
                found = zotero.find_by_entry(entry)
            except Exception:
                logger.exception("Zotero lookup failed for %s", arxiv_id)
                found = False
            if found:
                collected.append(_render_paper(entry))
            else:
                not_collected.append(_render_paper(entry))
        else:
            not_collected.append(_render_paper(entry))

    return collected, not_collected


def _gen_oneday_markdown(date_string: str, category: str,
                         oneday_arxiv_dict: dict[str, ArxivEntry],
                         zotero: ZoteroQuery | None,
                         old_data: list[str] | None = None) -> str:
    """Generate a single-day Markdown report.

    If old_data (arxiv IDs from a previous report run) is provided, any
    paper present in the new fetch that wasn't in the old report is listed
    in an 'update' section. This catches:

    - New submissions that were cross-listed to this category after the
      original query date.
    - Papers whose metadata (external DOI, journal_ref) was updated by
      arXiv after formal publication.
    """
    collected, not_collected = _classify_papers(oneday_arxiv_dict, zotero)

    new_data: list[str] = []
    if old_data is not None:
        for arxiv_id in oneday_arxiv_dict:
            if arxiv_id not in old_data:
                new_data.append(arxiv_id)

    return _render_report(date_string, category, collected, not_collected, new_data)


def parse_old_report(file_path: str) -> list[str] | None:
    """Extract arXiv IDs from a previously generated report.

    Returns None if the file doesn't exist (first run for that day).

    Collects arxiv IDs from TWO sources in the old report:
    - ### arXiv:XXXX headers (all papers that were listed)
    - - [x] [[#arXiv:XXXX]] completed tasks (papers the user read)

    Both are treated as 'existing' IDs; any ID in the new fetch that isn't
    in this list is flagged as a new/updated entry.
    """
    if not os.path.exists(file_path):
        return None

    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()

    old_ids: list[str] = []

    arxiv_pattern = r"^###\s*(arXiv:\S+)"
    for match in re.finditer(arxiv_pattern, content, re.MULTILINE):
        old_ids.append(match.group(1))

    completed_pattern = r"-\s*\[x\]\s*\[\[#(arXiv:\S+)\]\]"
    for match in re.finditer(completed_pattern, content):
        old_ids.append(match.group(1))

    return old_ids


def filter_arxiv_to_md(year: int, month: int, md_folder: str,
                       query_args: dict | None = None,
                       category: str = "quant-ph",
                       output_format: str = "category/year/month/day",
                       zotero: ZoteroQuery | None = None):
    """Fetch arXiv papers for every day in a month and write Markdown reports.

    Each day is queried individually rather than as a single range. This is
    an intentional design choice: re-running a day's query later may return
    new papers (cross-listed after the fact) and updated metadata (external
    DOIs, journal references added after formal publication). Comparing
    against the previously written report detects these changes.
    """
    _, num_days = calendar.monthrange(year, month)
    year_str = str(year)
    month_str = f"{month:02}"

    for day in range(1, num_days + 1):
        date_string = f"{year_str}-{month_str}-{day:02}"
        try:
            datetime(year, month, day)
        except ValueError:
            continue
        next_date = datetime(year, month, day) + timedelta(days=1)
        date_to_date = next_date.strftime("%Y-%m-%d")
        arxiv_dict = query_arxiv_dict(date_string, date_to_date, query_args)

        if not arxiv_dict:
            continue

        logger.info("processing %s, papers: %d", date_string, len(arxiv_dict))

        day_str = f"{day:02}"
        if output_format == "category/year/month/day":
            file_dir = os.path.join(md_folder, year_str, month_str)
        elif output_format == "year/month/day/category":
            file_dir = os.path.join(md_folder, year_str, month_str, day_str, category)
        elif output_format == "year/month/category/day":
            file_dir = os.path.join(md_folder, year_str, month_str, category, day_str)
        else:
            file_dir = os.path.join(md_folder, year_str, month_str)

        os.makedirs(file_dir, exist_ok=True)
        oneday_report_file = os.path.join(file_dir, f"{day_str}.md")

        parse_old = parse_old_report(oneday_report_file)
        markdown_str = _gen_oneday_markdown(date_string, category, arxiv_dict, zotero, parse_old)

        with open(oneday_report_file, "w", encoding="utf-8") as f:
            f.write(markdown_str)


def parse_md_to_arxiv_dict(md_file: str) -> dict[str, list]:
    """Parse a generated Markdown report back into a dict of arXiv IDs → data.

    Returns the legacy list format [title, authors, abstract, ()] for
    backwards compatibility with code that predates the ArxivEntry dataclass.
    """
    old_arxiv_dict: dict[str, list] = {}
    with open(md_file) as f:
        content = f.read()

    entry_pattern = r"###\s*(arXiv:\S+.*?)(?=###\s*arXiv:|## update)"
    for match in re.finditer(entry_pattern, content, re.DOTALL):
        entry_content = match.group(0)

        arxiv_match = re.search(r"^###\s*(arXiv:\S+)", entry_content)
        if not arxiv_match:
            continue
        arxiv_id = arxiv_match.group(1)

        title_match = re.search(r"Title:\s*(.+)", entry_content)
        title = title_match.group(1).strip() if title_match else ""

        authors_match = re.search(r"Authors:\s*(.+)", entry_content)
        if authors_match:
            authors_text = authors_match.group(1).strip()
            authors = [a.strip() for a in authors_text.split(",") if a.strip()]
        else:
            authors = []

        abstract_match = re.search(
            r"Abstract:\s*> \[!quote\]- Abstract\s*>\s*(.+?)(?=###\s*arXiv:|## update|\Z)",
            entry_content, re.DOTALL)
        if abstract_match:
            abstract_text = abstract_match.group(1).strip()
            abstract_text = re.sub(r">\s*", " ", abstract_text).strip()
        else:
            abstract_text = ""

        old_arxiv_dict[arxiv_id] = [title, authors, abstract_text, ()]

    return old_arxiv_dict

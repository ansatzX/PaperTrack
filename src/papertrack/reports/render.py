import os
import re
import calendar
import logging
import json
import hashlib
from pathlib import Path
from copy import deepcopy
from datetime import date, timedelta

from jinja2 import Environment, FileSystemLoader, select_autoescape

from papertrack.reports.markdown import abstract_markdown, repair_presentation
from papertrack.adapter_result import RATE_LIMITED
from papertrack.arxiv.entry import ArxivEntry
from papertrack.arxiv.fetch import query_arxiv_result
from papertrack.config import official_arxiv_category
from papertrack.journals.entry import JournalEntry
from papertrack.identity import normalize_doi
from papertrack.reports.migration import (has_journal_quality_failure, preserve_task_state,
                        validate_report_content, write_report_file)
from papertrack.reports.tags import arxiv_report_tag, journal_report_tag
from papertrack.zotero_query import ZoteroQuery

logger = logging.getLogger(__name__)


class ArxivRateLimitError(RuntimeError):
    """Stop all category requests when the shared arXiv endpoint is throttled."""

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
        _jinja_env.filters["abstract_markdown"] = abstract_markdown
    return _jinja_env


def _render_paper(entry: ArxivEntry) -> str:
    return repair_presentation(_get_jinja_env().get_template("paper.md.j2").render(entry=entry))


def _render_report(date_string: str, category: str,
                   collected_papers: list[str],
                   not_collected_papers: list[str],
                   new_data: list[str]) -> str:
    tmpl = _get_jinja_env().get_template("report.md.j2")
    return tmpl.render(
        date_string=date_string,
        category=category,
        report_tag=arxiv_report_tag(category, date_string),
        collected_papers=collected_papers,
        not_collected_papers=not_collected_papers,
        new_data=new_data,
    )


def _classify_papers(arxiv_dict: dict[str, ArxivEntry],
                     zotero: ZoteroQuery | None,
                     previously_collected: set[str] | None = None,
                     ) -> tuple[list[str], list[str]]:
    """Split papers into 'collected' (found in Zotero) and 'not collected'.

    When Zotero is unavailable (zotero=None), all papers go to not_collected
    rather than failing — the user still gets a report, just without the
    collection-status annotations.
    """
    collected: list[str] = []
    not_collected: list[str] = []
    previously_collected = previously_collected or set()

    for arxiv_id, entry in arxiv_dict.items():
        if zotero is not None:
            try:
                match = zotero.match_entry(entry)
                if getattr(zotero, "index_available", True) is False:
                    found = arxiv_id in previously_collected
                else:
                    found = match.is_collected
            except Exception:
                logger.exception("Zotero lookup failed for %s", arxiv_id)
                found = arxiv_id in previously_collected
            if found:
                collected.append(_render_paper(entry))
            else:
                not_collected.append(_render_paper(entry))
        else:
            if arxiv_id in previously_collected:
                collected.append(_render_paper(entry))
            else:
                not_collected.append(_render_paper(entry))

    return collected, not_collected


def _gen_oneday_markdown(date_string: str, category: str,
                         oneday_arxiv_dict: dict[str, ArxivEntry],
                         zotero: ZoteroQuery | None,
                         old_data: list[str] | None = None,
                         previously_collected: set[str] | None = None) -> str:
    """Generate a single-day Markdown report.

    If old_data (arxiv IDs from a previous report run) is provided, any
    paper present in the new fetch that wasn't in the old report is listed
    in an 'update' section. This catches:

    - New submissions that were cross-listed to this category after the
      original query date.
    - Papers whose metadata (external DOI, journal_ref) was updated by
      arXiv after formal publication.
    """
    collected, not_collected = _classify_papers(
        oneday_arxiv_dict,
        zotero,
        previously_collected,
    )

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

    if not _has_report_skeleton(content):
        logger.warning("Existing arXiv report looks corrupt, rebuilding: %s", file_path)
        return None

    old_ids: list[str] = []

    arxiv_pattern = r"^###\s*(arXiv:\S+)"
    for match in re.finditer(arxiv_pattern, content, re.MULTILINE):
        old_ids.append(match.group(1))

    completed_pattern = r"-\s*\[x\]\s*\[\[#(arXiv:\S+)\]\]"
    for match in re.finditer(completed_pattern, content):
        old_ids.append(match.group(1))

    return old_ids


def _has_report_skeleton(content: str) -> bool:
    return "## collected" in content and "## not collected" in content and "### " in content


def _read_existing_report(file_path: str) -> str | None:
    if not os.path.exists(file_path):
        return None
    with open(file_path, "r", encoding="utf-8") as f:
        return f.read()


def _report_entry_ids(content: str | None) -> set[str]:
    if not content:
        return set()
    return set(re.findall(r"^###\s*(arXiv:\S+)", content, re.MULTILINE))


def _collected_entry_ids(content: str | None) -> set[str]:
    if not content or "## collected" not in content:
        return set()
    collected = content.split("## collected", 1)[1].split("## not collected", 1)[0]
    return _report_entry_ids(collected)


def _arxiv_report_file(md_folder: str,
                       day_date: date,
                       category: str,
                       output_format: str = "category/year/month/day") -> str:
    year_str = f"{day_date.year:04d}"
    month_str = f"{day_date.month:02d}"
    day_str = f"{day_date.day:02d}"
    if output_format == "category/year/month/day":
        file_dir = os.path.join(md_folder, year_str, month_str)
    elif output_format == "year/month/day/category":
        file_dir = os.path.join(md_folder, year_str, month_str, day_str, category)
    elif output_format == "year/month/category/day":
        file_dir = os.path.join(md_folder, year_str, month_str, category, day_str)
    else:
        file_dir = os.path.join(md_folder, year_str, month_str)
    return os.path.join(file_dir, f"{day_str}.md")


def _existing_arxiv_report_is_written(report_file: str) -> bool:
    return parse_old_report(report_file) is not None


def _filter_one_arxiv_day_to_md(day_date: date, md_folder: str,
                                query_args: dict | None,
                                category: str,
                                zotero: ZoteroQuery | None,
                                output_format: str = "category/year/month/day",
                                refresh_existing: bool = False):
    date_string = day_date.strftime("%Y-%m-%d")
    next_date = day_date + timedelta(days=1)
    date_to_date = next_date.strftime("%Y-%m-%d")
    oneday_report_file = _arxiv_report_file(md_folder, day_date, category, output_format)

    if not refresh_existing and _existing_arxiv_report_is_written(oneday_report_file):
        logger.info("Skipping existing arXiv report: %s %s", category, date_string)
        return

    arxiv_fetch = query_arxiv_result(
        date_string,
        date_to_date,
        query_args,
        required_category=official_arxiv_category(category),
    )
    if not arxiv_fetch.ok:
        if arxiv_fetch.outcome == RATE_LIMITED:
            raise ArxivRateLimitError(
                f"arXiv rate limited while fetching {category} {date_string}; "
                "stopping this run to avoid worsening the throttle"
            )
        logger.warning(
            "Skipping arXiv %s %s because fetch outcome was %s",
            category,
            date_string,
            arxiv_fetch.outcome,
        )
        return
    arxiv_dict = arxiv_fetch.entries
    if not arxiv_dict:
        return

    old_content = _read_existing_report(oneday_report_file)
    missing_old_ids = _report_entry_ids(old_content) - set(arxiv_dict)
    if missing_old_ids:
        logger.warning(
            "Preserving existing arXiv report because refreshed result lost %d "
            "paper(s): %s %s",
            len(missing_old_ids),
            category,
            date_string,
        )
        return

    logger.info("processing %s, papers: %d", date_string, len(arxiv_dict))

    os.makedirs(os.path.dirname(oneday_report_file), exist_ok=True)

    parse_old = parse_old_report(oneday_report_file)
    markdown_str = _gen_oneday_markdown(
        date_string,
        category,
        arxiv_dict,
        zotero,
        parse_old,
        _collected_entry_ids(old_content),
    )
    markdown_str = preserve_task_state(markdown_str, old_content)

    write_report_file(oneday_report_file, markdown_str, source="arxiv")


def filter_arxiv_days_to_md(days: list[date], md_folder: str,
                            query_args: dict | None = None,
                            category: str = "quant-ph",
                            zotero: ZoteroQuery | None = None,
                            refresh_existing: bool = False):
    """Fetch exact arXiv day buckets and write Markdown reports.

    This is the production path for rolling refresh. The caller owns calendar
    planning; this function owns per-day fetch, old-report parsing, rendering,
    and writing.
    """
    for day_date in days:
        _filter_one_arxiv_day_to_md(
            day_date,
            md_folder,
            query_args,
            category,
            zotero,
            refresh_existing=refresh_existing,
        )


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

    for day in range(1, num_days + 1):
        day_date = date(year, month, day)
        _filter_one_arxiv_day_to_md(
            day_date,
            md_folder,
            query_args,
            category,
            zotero,
            output_format,
        )


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
            r"(?:Abstract:\s*)?> \[!quote\]- Abstract\s*>\s*(.+?)(?=###\s*arXiv:|## update|\Z)",
            entry_content, re.DOTALL)
        if abstract_match:
            abstract_text = abstract_match.group(1).strip()
            abstract_text = re.sub(r">\s*", " ", abstract_text).strip()
        else:
            abstract_text = ""

        old_arxiv_dict[arxiv_id] = [title, authors, abstract_text, ()]

    return old_arxiv_dict


# ── Journal (issue-based) report functions ──────────────────────────────


def _render_journal_paper(entry: JournalEntry) -> str:
    return repair_presentation(_get_jinja_env().get_template("journal_paper.md.j2").render(entry=entry))


def _render_journal_report(journal_name: str, journal_slug: str,
                           volume: str, issue: str, date: str,
                           provider: str,
                           collected_papers: list[str],
                           not_collected_papers: list[str],
                           new_data: list[str]) -> str:
    tmpl = _get_jinja_env().get_template("journal_report.md.j2")
    return tmpl.render(
        journal_name=journal_name,
        journal_slug=journal_slug,
        volume=volume,
        issue=issue,
        date=date,
        provider=provider,
        report_tag=journal_report_tag(journal_slug, volume, issue),
        collected_papers=collected_papers,
        not_collected_papers=not_collected_papers,
        new_data=new_data,
    )


def _classify_journal_papers(entries: dict[str, JournalEntry],
                              zotero: ZoteroQuery | None,
                              existing_collected_dois: set[str] | None = None,
                              ) -> tuple[list[str], list[str]]:
    collected: list[str] = []
    not_collected: list[str] = []

    for doi, entry in entries.items():
        if zotero is not None:
            try:
                found = zotero.match_entry(entry).is_collected
            except Exception:
                logger.exception("Zotero lookup failed for %s", doi)
                found = False
            if found:
                collected.append(_render_journal_paper(entry))
            else:
                not_collected.append(_render_journal_paper(entry))
        elif (existing_collected_dois
              and normalize_doi(doi) in existing_collected_dois):
            collected.append(_render_journal_paper(entry))
        else:
            not_collected.append(_render_journal_paper(entry))

    return collected, not_collected


def parse_journal_report(file_path: str) -> list[str] | None:
    """Extract DOIs from a previously generated journal report.

    Returns None if the file doesn't exist (first run for that issue).
    """
    if not os.path.exists(file_path):
        return None

    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()

    if not _has_report_skeleton(content):
        logger.warning("Existing journal report looks corrupt, rebuilding: %s", file_path)
        return None

    old_ids: list[str] = []

    doi_pattern = r"^###\s*(10\.\S+)"
    for match in re.finditer(doi_pattern, content, re.MULTILINE):
        old_ids.append(match.group(1))

    completed_pattern = r"-\s*\[x\]\s*\[\[#(10\.\S+)\]\]"
    for match in re.finditer(completed_pattern, content):
        old_ids.append(match.group(1))

    return old_ids


def _journal_report_file(md_folder: str, journal_slug: str, volume: str, issue: str) -> str:
    return os.path.join(md_folder, journal_slug, str(volume), f"{issue}.md")


def _is_missing_or_corrupt_journal_report(md_folder: str,
                                          journal_slug: str,
                                          volume: str,
                                          issue: str) -> bool:
    report_file = _journal_report_file(md_folder, journal_slug, volume, issue)
    if not os.path.exists(report_file):
        return True
    old_content = _read_existing_report(report_file)
    if old_content is None:
        return True
    if parse_journal_report(report_file) is None:
        return True
    return has_journal_quality_failure(old_content)


def filter_journal_to_md(journal_name: str, journal_slug: str,
                         volume: str, issue: str, issn: str,
                         md_folder: str, zotero: ZoteroQuery | None = None,
                         year: int | None = None, acs_code: str = "",
                         provider: str = "", browser_snapshot: str = "",
                         ) -> bool:
    """Validate an agent capture and render locally; never fetch publisher data."""
    from papertrack.journals.capture import read_browser_issue, validate_reading_controls

    if not browser_snapshot:
        raise RuntimeError("Journal fetching is agent-owned. Generate a task with "
                           "journal issue, then import it with --browser-snapshot.")
    result = read_browser_issue(
        browser_snapshot, provider=provider or ("acs" if acs_code else ""),
        code=acs_code or journal_slug, volume=volume, issue=issue,
        journal_name=journal_name,
    )
    unavailable = set(result.metadata["abstract_unavailable_dois"])
    if any(not e.abstract and e.doi not in unavailable for e in result.entries.values()):
        raise ValueError("Capture has unresolved abstracts; resume the agent task")
    if result.metadata.get("browser_errors"):
        raise ValueError("Capture has unresolved browser errors; resume the agent task")
    # Explicit capture imports must pass the same source check as agent runs.
    validate_reading_controls(
        json.loads(Path(browser_snapshot).read_text(encoding='utf-8')),
        Path(browser_snapshot + '.html'),
        provider=provider or ('acs' if acs_code else ''),
    )
    return write_journal_entries_to_md(
        journal_name, journal_slug, volume, issue, result.entries, md_folder,
        zotero=zotero, year=year, provider=provider, acs_code=acs_code,
        verified_absent_dois=unavailable if provider == 'aps' else set(),
        repair_backup_dir=str(Path(browser_snapshot).parent / 'report-backups'),
    )


def write_journal_entries_to_md(
    journal_name: str,
    journal_slug: str,
    volume: str,
    issue: str,
    entries: dict[str, JournalEntry],
    md_folder: str,
    zotero: ZoteroQuery | None = None,
    year: int | None = None,
    provider: str = "",
    acs_code: str = "",
    existing_collected_dois: set[str] | None = None,
    verified_absent_dois: set[str] | None = None,
    repair_backup_dir: str = '',
) -> bool:
    """Render already-authoritative journal entries without network access."""
    if not entries:
        return False

    canonical = Path(_journal_report_file(md_folder, journal_slug, volume, issue))
    legacy = canonical.with_suffix('') / 'report.md'
    if legacy.is_file():
        raise ValueError('Legacy journal layout requires local migration before import: '
                         f'{legacy}; run migrate --journal-layout-only with --backup-dir')
    content, needs_backup = _prepare_journal_report(
        str(canonical), journal_name, journal_slug, volume, issue, entries,
        zotero, year, provider, acs_code, existing_collected_dois,
        verified_absent_dois, repair_backup_dir,
    )
    if needs_backup:
        backup_dir = Path(repair_backup_dir) if repair_backup_dir else Path(md_folder) / '.papertrack-backups'
        _backup_journal_report(canonical, backup_dir)
    write_report_file(str(canonical), content, source="journal")
    return True


def _check_journal_user_text(content: str | None, path: str, generated_tag: str) -> None:
    """Reject text outside the generated scaffold and supported task edits.

    Field contents are regenerated by request; arbitrary prose, images, custom
    metadata and quotes outside the reading callout must not disappear.
    """
    if not content:
        return
    body = content
    front = re.match(r'\A---\n(.*?)\n---\n', body, re.S)
    if front:
        for line in front[1].splitlines():
            if not (re.fullmatch(r'papertrack_(?:format|source|provider|journal|volume|issue):.*', line)
                    or line == 'tags:' or line == f'  - {generated_tag}'):
                raise ValueError(f'Unpreserved user metadata in journal report: {path}: {line}')
        body = body[front.end():]
    body = re.sub(r'(?m)^```dataview\nTASK\nfrom #' + re.escape(generated_tag)
                  + r'\n\s*WHERE completed\n\s*```', '', body)
    reading = False
    field_continuation = False
    doi = ''
    in_update = False
    seen_tasks = set()
    anchors = {normalize_doi(value) for value in re.findall(r'(?m)^###\s+(10\.\S+)', body)}
    for line in body.splitlines():
        if not line.strip():
            reading = False
            field_continuation = False
            continue
        if re.fullmatch(r'(?:Title|Authors):[ \t]*.*', line):
            field_continuation = True
            reading = False
            continue
        if field_continuation:
            # Generated fields may wrap without a blank line. Markdown blocks
            # are not field continuations; a blank line always ends the field.
            if not re.match(r'^(?:#|>|!\[|```|%%|[-*] |Links:|Abstract:|Extract:)', line):
                continue
            field_continuation = False
        if line.startswith('## '):
            in_update = line == '## update'
        heading = re.fullmatch(r'###\s+(10\.\S+)', line)
        if heading:
            doi = heading[1]
            reading = False
            continue
        if re.fullmatch(r'> \[!quote\]- (?:Abstract|Extract(?:（原文摘录）)?)', line):
            reading = True
            continue
        if reading and line.startswith('>'):
            continue
        reading = False
        task = re.fullmatch(r'- \[[ xX]\] \[(10\.[^\]]+)\]\(https://doi\.org/([^)]+)\).*', line)
        internal_task = re.fullmatch(r'- \[[ xX]\] \[\[#(10\.[^\]]+)\]\].*', line)
        valid_external = (task and not in_update
                          and normalize_doi(task[1]) == normalize_doi(task[2]) == normalize_doi(doi))
        valid_internal = (internal_task and ((in_update and normalize_doi(internal_task[1]) in anchors)
                                            or normalize_doi(internal_task[1]) == normalize_doi(doi)))
        if valid_external or valid_internal:
            identifier = normalize_doi((task if valid_external else internal_task)[1])
            key = ('update' if in_update else 'paper', identifier)
            if key in seen_tasks:
                raise ValueError(f'Unpreserved duplicate task in journal report: {path}: {line}')
            seen_tasks.add(key)
            continue
        if (re.fullmatch(r'# .+ Vol\. .+, Iss\. .+ — .+', line)
                or line in ('## collected', '## not collected', '## update', 'Links:', 'Abstract:', 'Extract:')
                or re.fullmatch(r'%% papertrack_provenance: \{.*\} %%', line)
                or (doi and re.fullmatch(r'!\[\|700\]\(https://pubs\.acs\.org/cms/' + re.escape(doi) + r'/asset/images/[^)]+\)', line))):
            continue
        raise ValueError(f'Unpreserved user text in journal report: {path}: {line[:160]}')


def _backup_journal_report(path: Path, backup_dir: Path) -> None:
    original = path.read_bytes()
    digest = hashlib.sha256(original).hexdigest()
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f'journal-{digest}.md'
    if backup.exists():
        if backup.read_bytes() != original:
            raise ValueError(f'Prior report backup does not match: {backup}')
    else:
        with backup.open('xb') as output:
            output.write(original)
            output.flush()
            os.fsync(output.fileno())


def _prepare_journal_report(
    report_file, journal_name, journal_slug, volume, issue, entries,
    zotero, year, provider, acs_code, existing_collected_dois,
    verified_absent_dois, repair_backup_dir,
):
    entries = deepcopy(entries)

    # Infer date from first entry
    first = next(iter(entries.values()))
    date_str = first.date or f"{year or ''}"

    logger.info("processing %s Vol.%s Iss.%s, papers: %d", journal_name, volume, issue, len(entries))

    parse_old = parse_journal_report(report_file)
    old_content = _read_existing_report(report_file)
    _check_journal_user_text(old_content, report_file, journal_report_tag(journal_slug, volume, issue))
    old_anchors = {normalize_doi(d): d for d in re.findall(
        r"^###\s*(10\.\S+)", old_content or "", re.MULTILINE)}
    current = {normalize_doi(e.doi): e for e in entries.values()}
    if not old_anchors.keys() <= current.keys():
        raise ValueError("Capture would remove prior issue members; prior report preserved")
    repair_erratum_teaser = False
    for block in re.split(r"(?m)^###\s+", old_content or "")[1:]:
        doi = normalize_doi(block.splitlines()[0].strip())
        has_reading_text = re.search(r"(?m)^(?:(?:Abstract|Extract(?:（原文摘录）)?):[ \t]*|> \[!quote\]- (?:Abstract|Extract(?:（原文摘录）)?))$", block)
        if doi in current and has_reading_text and not current[doi].abstract:
            quoted = ' '.join(line[1:].strip() for line in block.splitlines()
                              if line.startswith('>') and not line.startswith('> [!'))
            known_teaser = re.fullmatch(r'This corrects the article DOI: 10\.\d{4,9}/\S+\.', quoted)
            if (provider == 'aps' and doi in (verified_absent_dois or set())
                    and current[doi].title.startswith('Erratum:')
                    and known_teaser and repair_backup_dir):
                repair_erratum_teaser = True
            else:
                raise ValueError("Capture would remove a prior abstract; prior report preserved")
    # Keep existing anchor spelling so DOI casing changes cannot erase task state.
    for doi, anchor in old_anchors.items():
        current[doi].doi = anchor
    # A repair run commonly has no live Zotero connection.  Retain the
    # existing collection grouping in that case; completed checkbox state is
    # preserved separately below.
    if existing_collected_dois is None and old_content:
        existing_collected_dois = _existing_collected_journal_dois(old_content)
    collected, not_collected = _classify_journal_papers(
        entries,
        zotero,
        existing_collected_dois,
    )

    new_data: list[str] = []
    if parse_old is not None:
        for doi in entries:
            if normalize_doi(doi) not in {normalize_doi(d) for d in parse_old}:
                new_data.append(doi)

    # Existing update tasks also carry durable completion and inline notes.
    for anchor in re.findall(r"(?m)^- \[[ xX]\] \[\[#(10\.[^\]]+)\]\]", old_content or ""):
        normalized = normalize_doi(anchor)
        if normalized not in current:
            raise ValueError("Capture would remove a prior task; prior report preserved")
        if normalized not in {normalize_doi(value) for value in new_data}:
            new_data.append(current[normalized].doi)

    render_provider = provider or ("acs" if acs_code else "")
    markdown_str = _render_journal_report(
        journal_name, journal_slug, volume, issue,
        date_str, render_provider, collected, not_collected, new_data,
    )
    markdown_str = preserve_task_state(markdown_str, old_content)

    if not validate_report_content(markdown_str, source="journal"):
        raise ValueError(f"Generated journal report failed validation: {report_file}")
    return markdown_str, repair_erratum_teaser


def _existing_collected_journal_dois(content: str) -> set[str]:
    """Return generated DOI anchors in the durable ``collected`` section."""
    match = re.search(
        r"^## (?:collected|Collected Papers)[ \t]*\r?\n(.*?)(?=^## |\Z)",
        content, re.DOTALL | re.MULTILINE
    )
    if not match:
        return set()
    return {
        normalize_doi(doi)
        for doi in re.findall(r"^###\s*(10\.\S+)", match.group(1), re.MULTILINE)
    }

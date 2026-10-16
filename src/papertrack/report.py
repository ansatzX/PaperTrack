import os
import re
import calendar
import logging
from datetime import date, timedelta

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .adapter_result import RATE_LIMITED
from .arxiv_entry import ArxivEntry
from .arxiv_index_fetch import query_arxiv_result
from .codex import official_arxiv_category
from .journal_entry import JournalEntry
from .identity import normalize_doi
from .journal_fetch import query_journal_issue_full_result
from .migration import has_journal_quality_failure, preserve_task_state, write_report_file
from .report_tags import arxiv_report_tag, journal_report_tag
from .source_policy import SourcePolicy
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
            raise RuntimeError(
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
            r"Abstract:\s*> \[!quote\]- Abstract\s*>\s*(.+?)(?=###\s*arXiv:|## update|\Z)",
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
    return _get_jinja_env().get_template("journal_paper.md.j2").render(entry=entry)


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
                         provider: str = "",
                         source_policy: SourcePolicy | None = None,
                         existing_collected_dois: set[str] | None = None,
                         provider_runtime=None,
                         browser_snapshot: str = "",
                         ) -> bool:
    """Fetch journal articles for a volume/issue and write a Markdown report.

    The report goes to {md_folder}/{journal_slug}/{volume}/{issue}.md
    """
    browser_args = {"browser_snapshot": browser_snapshot} if browser_snapshot else {}
    fetch_result = query_journal_issue_full_result(
        issn=issn,
        volume=volume,
        issue=issue,
        journal_name=journal_name,
        year=year,
        acs_code=acs_code,
        provider=provider,
        journal_slug=journal_slug,
        source_policy=source_policy,
        provider_runtime=provider_runtime,
        **browser_args,
    )
    if provider_runtime is not None and hasattr(provider_runtime, "stats"):
        provider_runtime.last_source_outcome = fetch_result.outcome
        provider_runtime.last_source_metadata = dict(fetch_result.metadata)
        provider_runtime.last_source_message = fetch_result.message
    if not fetch_result.ok:
        logger.warning(
            "Skipping %s Vol.%s Iss.%s because fetch outcome was %s",
            journal_name,
            volume,
            issue,
            fetch_result.outcome,
        )
        return False
    entries = fetch_result.entries

    if not entries:
        logger.warning("No articles found for %s Vol.%s Iss.%s", journal_name, volume, issue)
        return False

    if fetch_result.source_role == "third_party_issue":
        report_file = _journal_report_file(md_folder, journal_slug, volume, issue)
        old_ids = parse_journal_report(report_file) if os.path.exists(report_file) else None
        current = {normalize_doi(e.doi): e for e in entries.values()}
        if old_ids and not {normalize_doi(d) for d in old_ids}.issubset(current):
            logger.warning("Keeping prior report: third-party sources lost existing issue members")
            return False
        old_content = _read_existing_report(report_file) or ""
        for block in re.split(r"(?m)^###\s+", old_content)[1:]:
            doi = normalize_doi(block.splitlines()[0].strip())
            if doi in current and "Abstract:" in block and not current[doi].abstract:
                logger.warning("Keeping prior report: third-party sources lost an existing abstract")
                return False

    return write_journal_entries_to_md(
        journal_name=journal_name,
        journal_slug=journal_slug,
        volume=volume,
        issue=issue,
        entries=entries,
        md_folder=md_folder,
        zotero=zotero,
        year=year,
        provider=provider,
        acs_code=acs_code,
        existing_collected_dois=existing_collected_dois,
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
) -> bool:
    """Render already-authoritative journal entries without network access."""
    if not entries:
        return False

    # Infer date from first entry
    first = next(iter(entries.values()))
    date_str = first.date or f"{year or ''}"

    logger.info("processing %s Vol.%s Iss.%s, papers: %d", journal_name, volume, issue, len(entries))

    report_file = _journal_report_file(md_folder, journal_slug, volume, issue)
    file_dir = os.path.dirname(report_file)
    os.makedirs(file_dir, exist_ok=True)

    parse_old = parse_journal_report(report_file)
    old_content = _read_existing_report(report_file)
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
            if doi not in parse_old:
                new_data.append(doi)

    render_provider = provider or ("acs" if acs_code else "")
    markdown_str = _render_journal_report(
        journal_name, journal_slug, volume, issue,
        date_str, render_provider, collected, not_collected, new_data,
    )
    markdown_str = preserve_task_state(markdown_str, old_content)

    write_report_file(report_file, markdown_str, source="journal")
    return True


def _existing_collected_journal_dois(content: str) -> set[str]:
    """Return generated DOI anchors in the durable ``collected`` section."""
    match = re.search(
        r"## Collected Papers\s*\n(.*?)(?=\n## |\Z)", content, re.DOTALL
    )
    if not match:
        return set()
    return {
        normalize_doi(doi)
        for doi in re.findall(r"^###\s*(10\.\S+)", match.group(1), re.MULTILINE)
    }


# ── State file helpers ──────────────────────────────────────────────────


def _state_path(md_folder: str) -> str:
    return os.path.join(os.getcwd(), ".papertrack_state.json")


def _load_state(md_folder: str) -> dict:
    import json
    sp = _state_path(md_folder)
    if os.path.exists(sp):
        with open(sp) as f:
            return json.load(f)
    return {}


def _save_state(md_folder: str, state: dict):
    import json
    with open(_state_path(md_folder), "w") as f:
        json.dump(state, f, indent=2)


# ── Auto-discover journal pipeline ──────────────────────────────────────


def _process_auto_triples(
    triples,
    *,
    backfill,
    rebuilt_keys,
    processed,
    journal_name,
    journal_slug,
    issn,
    acs_code,
    md_folder,
    zotero,
    provider,
    policy,
) -> int:
    """Process (year|None, volume, issue) discovery triples into reports.

    Newest-first input is re-sorted oldest-first for backfill, or truncated to
    the newest issue for latest mode.  Returns the number of newly written
    reports.
    """
    triples = list(triples)
    if backfill:
        triples.reverse()  # oldest-first for backfill
    else:
        triples = triples[:1]  # latest only
    new_count = 0
    for yr, vol, iss in triples:
        key = (int(vol), int(iss))
        if key in rebuilt_keys:
            continue
        if key in processed and not _is_missing_or_corrupt_journal_report(
            md_folder, journal_slug, str(vol), str(iss)
        ):
            continue
        logger.info("New issue: %s Vol %d Iss %d (year %s)", journal_name, vol, iss, yr)
        try:
            written = filter_journal_to_md(
                journal_name=journal_name,
                journal_slug=journal_slug,
                volume=str(vol),
                issue=str(iss),
                issn=issn,
                md_folder=md_folder,
                zotero=zotero,
                acs_code=acs_code,
                year=yr,
                provider=provider,
                source_policy=policy,
            )
        except Exception:
            logger.exception(
                "Failed to process %s Vol %d Iss %d; leaving it unprocessed for a later run",
                journal_name,
                vol,
                iss,
            )
            continue
        if written:
            processed.add(key)
            new_count += 1
        if not backfill:
            break
    return new_count


def _discover_with_crossref_fallback(
    primary,
    *,
    journal_name,
    issn,
    from_year,
    backfill,
):
    """Run the official discovery callable, falling back to CrossRef on failure.

    Returns a newest-first list of (year, volume, issue) triples, or None when
    no issues could be discovered at all.
    """
    try:
        triples = primary()
        if not triples:
            raise RuntimeError("official discovery returned no issues")
        return triples
    except Exception as exc:
        logger.warning(
            "%s official issue discovery failed (%s); falling back to CrossRef",
            journal_name,
            exc,
        )
        from .crossref_loi import discover_issues_crossref

        triples = discover_issues_crossref(
            issn, from_year=from_year if backfill else 0
        )
        if not triples:
            logger.warning("No issues discovered for %s via CrossRef", journal_name)
            return None
        return triples


def filter_journal_auto(journal_name: str, journal_slug: str,
                        issn: str, acs_code: str,
                        md_folder: str, zotero: ZoteroQuery | None = None,
                        backfill: bool = False, from_year: int = 0,
                        provider: str = "acs",
                        source_policy: SourcePolicy | None = None):
    """Auto-discover new issues for a journal and process them.

    Official discovery runs first (ACS LOI pages, APS issue pages, CrossRef
    for AIP); when official discovery fails, issue discovery falls back to
    CrossRef.  Article extraction itself follows the AGENTS.md source chain
    (official issue page -> PubMed -> CrossRef).

    State is persisted in .papertrack_state.json in the current working directory.
    Repeated runs only pick up newly published issues.
    """
    policy = source_policy or SourcePolicy.official_only()
    state = _load_state(md_folder)
    journal_state = state.get(journal_slug, {})
    processed = set(tuple(p) for p in journal_state.get("processed", []))
    new_count = 0
    rebuilt_keys: set[tuple[int, int]] = set()

    for vol, iss in sorted(processed):
        if not _is_missing_or_corrupt_journal_report(md_folder, journal_slug, str(vol), str(iss)):
            continue
        logger.info("Rebuilding missing/corrupt issue: %s Vol %d Iss %d", journal_name, vol, iss)
        written = filter_journal_to_md(
            journal_name=journal_name,
            journal_slug=journal_slug,
            volume=str(vol),
            issue=str(iss),
            issn=issn,
            md_folder=md_folder,
            zotero=zotero,
            acs_code=acs_code,
            provider=provider,
            source_policy=policy,
        )
        rebuilt_keys.add((vol, iss))
        if written:
            new_count += 1

    if acs_code:
        # ── ACS path: LOI page, falling back to CrossRef discovery ──
        from .acs_loi import discover_issues, get_latest_issue

        if backfill:
            triples = _discover_with_crossref_fallback(
                lambda: discover_issues(acs_code, from_year=from_year),
                journal_name=journal_name, issn=issn,
                from_year=from_year, backfill=backfill,
            )
        else:
            def _latest():
                latest = get_latest_issue(acs_code)
                return [(None, int(latest[0]), int(latest[1]))] if latest else []

            triples = _discover_with_crossref_fallback(
                _latest,
                journal_name=journal_name, issn=issn,
                from_year=from_year, backfill=backfill,
            )
        if triples is None:
            return
        new_count += _process_auto_triples(
            triples,
            backfill=backfill, rebuilt_keys=rebuilt_keys, processed=processed,
            journal_name=journal_name, journal_slug=journal_slug, issn=issn,
            acs_code=acs_code, md_folder=md_folder, zotero=zotero,
            provider=provider, policy=policy,
        )

    elif provider == "aps":
        # ── APS path: official issue pages, falling back to CrossRef ──
        from .aps_loi import discover_issues_aps

        triples = _discover_with_crossref_fallback(
            lambda: discover_issues_aps(
                journal_slug,
                from_year=from_year if backfill else 0,
                max_volumes=5,
            ),
            journal_name=journal_name, issn=issn,
            from_year=from_year, backfill=backfill,
        )
        if triples is None:
            return
        new_count += _process_auto_triples(
            triples,
            backfill=backfill, rebuilt_keys=rebuilt_keys, processed=processed,
            journal_name=journal_name, journal_slug=journal_slug, issn=issn,
            acs_code=acs_code, md_folder=md_folder, zotero=zotero,
            provider=provider, policy=policy,
        )

    else:
        # ── CrossRef/AIP path: discover via CrossRef API ──
        if not policy.crossref_discovery_enabled:
            raise RuntimeError(
                "AIP latest/backfill requires CrossRef issue discovery; "
                "in official-only mode use 'journal issue --volume V --issue I'"
            )
        from .crossref_loi import discover_issues_crossref

        triples = discover_issues_crossref(issn, from_year=from_year)
        if not triples:
            logger.warning("No issues discovered for %s via CrossRef", journal_name)
            return
        new_count += _process_auto_triples(
            triples,
            backfill=backfill, rebuilt_keys=rebuilt_keys, processed=processed,
            journal_name=journal_name, journal_slug=journal_slug, issn=issn,
            acs_code=acs_code, md_folder=md_folder, zotero=zotero,
            provider=provider, policy=policy,
        )

    journal_state["processed"] = [[v, i] for v, i in sorted(processed)]
    state[journal_slug] = journal_state
    _save_state(md_folder, state)

    if new_count == 0:
        logger.info("%s: no new issues", journal_name)
    else:
        logger.info("%s: %d new issues processed", journal_name, new_count)

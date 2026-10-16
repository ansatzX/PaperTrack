"""Bounded third-party issue discovery and exact-DOI abstract enrichment."""
from __future__ import annotations

import json
from copy import deepcopy
import logging
import os
import re
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .diagnostics import record_fetch_attempt
from .journal_entry import JournalEntry

logger = logging.getLogger(__name__)
FIELDS = ("title", "authors", "abstract", "journal_name", "volume", "issue", "pages", "date")


def normalize_doi(value: str) -> str:
    if not isinstance(value, str):
        return ""
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", (value or "").strip(), flags=re.I).lower()
    return value if re.fullmatch(r"10\.\d{4,9}/\S+", value) else ""


def request_json(provider: str, url: str, payload=None) -> dict | list:
    headers = {"User-Agent": "PaperTrack/0.1", "Accept": "application/json"}
    key = os.environ.get({"openalex": "OPENALEX_API_KEY", "semantic_scholar": "SEMANTIC_SCHOLAR_API_KEY"}.get(provider, ""))
    if key:
        headers["x-api-key" if provider == "semantic_scholar" else "Authorization"] = key if provider == "semantic_scholar" else f"Bearer {key}"
    body = None if payload is None else json.dumps(payload).encode()
    if body is not None:
        headers["Content-Type"] = "application/json"
    try:
        with urlopen(Request(url, data=body, headers=headers), timeout=20) as response:
            return json.load(response)
    except Exception as exc:
        # Do not log headers/credentials, and do not retry a throttled service.
        record_fetch_attempt({"source": "journal", "provider": provider,
                              "source_role": "third_party_metadata", "stage": "metadata_api",
                              "outcome": "fetch_failed", "exception": type(exc).__name__,
                              "status_code": getattr(exc, "code", None), "url": url})
        return {}


def europe_pmc_issue(issn: str, volume: str, issue: str, journal_name: str) -> dict[str, JournalEntry]:
    if not issn:
        return {}
    entries = {}
    cursor = "*"
    seen = set()
    # Bounded issue query; partial results remain useful but are never called complete.
    for _ in range(5):
        if cursor in seen:
            break
        seen.add(cursor)
        params = {"query": f'ISSN:"{issn}" AND VOLUME:"{volume}" AND ISSUE:"{issue}"',
                  "resultType": "core", "format": "json", "pageSize": 1000, "cursorMark": cursor}
        data = request_json("europe_pmc", "https://www.ebi.ac.uk/europepmc/webservices/rest/search?" + urlencode(params))
        if not isinstance(data, dict):
            break
        result_list = data.get("resultList") or {}
        if not isinstance(result_list, dict):
            break
        rows = result_list.get("result") or []
        if not isinstance(rows, list):
            break
        for row in rows:
            if not isinstance(row, dict):
                continue
            info = row.get("journalInfo") or {}
            journal = info.get("journal") or {}
            doi = normalize_doi(row.get("doi", ""))
            if (issn not in {journal.get("issn"), journal.get("essn")} or
                    str(info.get("volume", "")) != str(volume) or
                    str(info.get("issue", "")) != str(issue) or not doi):
                continue
            title = JournalEntry._clean_crossref_abstract(row.get("title", ""))
            if not title or JournalEntry.is_non_article(doi, title):
                continue
            authors = [a.get("fullName", "") for a in (row.get("authorList") or {}).get("author", [])]
            entries[doi] = JournalEntry(
                doi=doi, title=title, authors=[a for a in authors if a],
                abstract=JournalEntry._clean_crossref_abstract(row.get("abstractText", "")),
                journal_name=journal_name or journal.get("title", ""), volume=str(volume), issue=str(issue),
                pages=row.get("pageInfo", ""), date=row.get("firstPublicationDate", ""))
        cursor = data.get("nextCursorMark")
        if not rows or not cursor:
            break
    return entries


def stamp(entry: JournalEntry, source: str) -> None:
    for field in FIELDS:
        if getattr(entry, field):
            entry.field_sources.setdefault(field, source)
    if source not in entry.membership_sources:
        entry.membership_sources.append(source)


def merge_entries(base: dict[str, JournalEntry], incoming: dict[str, JournalEntry], source: str,
                  *, allow_new: bool, volume: str, issue: str) -> list[dict]:
    """Preserve the chosen source's values; fill blanks, recording disagreements."""
    conflicts = []
    index = {normalize_doi(e.doi): e for e in base.values()}
    for candidate in incoming.values():
        doi = normalize_doi(candidate.doi)
        if not doi or str(candidate.volume) != str(volume) or str(candidate.issue) != str(issue):
            continue
        if JournalEntry.is_non_article(doi, candidate.title):
            continue
        current = index.get(doi)
        if current is None:
            if allow_new and candidate.title:
                candidate = deepcopy(candidate)
                candidate.doi = doi
                stamp(candidate, source)
                base[doi] = candidate
                index[doi] = candidate
            continue
        if source not in current.membership_sources:
            current.membership_sources.append(source)
        for field in FIELDS:
            old, new = getattr(current, field), getattr(candidate, field)
            if not old and new:
                setattr(current, field, new)
                current.field_sources[field] = source
            elif old and new and old != new:
                conflicts.append({"doi": doi, "field": field, "kept": current.field_sources.get(field), "other": source})
    return conflicts


def enrich_abstracts(entries: dict[str, JournalEntry]) -> None:
    """Two batch API sources, only for missing abstracts; never add members."""
    for provider in ("openalex", "semantic_scholar"):
        missing = {normalize_doi(e.doi): e for e in entries.values() if not e.abstract and normalize_doi(e.doi)}
        dois = sorted(missing)
        for start in range(0, len(dois), 50):
            batch = dois[start:start + 50]
            if provider == "openalex":
                url = "https://api.openalex.org/works?" + urlencode({
                    "filter": "doi:" + "|".join(batch), "per_page": 50,
                    "select": "doi,abstract_inverted_index"})
                data = request_json(provider, url)
                if not isinstance(data, dict) or not data:
                    break
                rows = data.get("results", [])
                if not isinstance(rows, list):
                    break
            else:
                url = "https://api.semanticscholar.org/graph/v1/paper/batch?fields=externalIds,abstract"
                rows = request_json(provider, url, {"ids": ["DOI:" + d for d in batch]})
                if not isinstance(rows, list) or not rows:
                    break
            for row in rows:
                if not isinstance(row, dict):
                    continue
                identifiers = row.get("externalIds") or {}
                if not isinstance(identifiers, dict):
                    continue
                doi = normalize_doi(row.get("doi", "") if provider == "openalex" else identifiers.get("DOI", ""))
                if doi not in missing or missing[doi].abstract:
                    continue
                if provider == "openalex":
                    inverted = row.get("abstract_inverted_index") or {}
                    if not isinstance(inverted, dict) or any(
                            not isinstance(positions, list) or not all(type(p) is int and p >= 0 for p in positions)
                            for positions in inverted.values()):
                        continue
                    pairs = sorted((pos, word) for word, positions in inverted.items() for pos in positions)
                    if [pos for pos, _ in pairs] != list(range(len(pairs))):
                        continue
                    abstract = " ".join(word for _, word in pairs)
                else:
                    abstract = row.get("abstract") or ""
                if not isinstance(abstract, str):
                    continue
                abstract = JournalEntry._clean_crossref_abstract(abstract)
                # Reject obvious fragments such as the observed 27-word OpenAlex tail.
                # This is a quality floor, not proof that a long abstract is complete.
                if len(abstract.split()) < 40 or abstract.startswith(("=", ")", ",", ";")):
                    continue
                missing[doi].abstract = abstract
                missing[doi].field_sources["abstract"] = provider

"""Local presentation repair using retained source evidence, without fetching."""
import json
import re
from functools import lru_cache
from html import unescape
from pathlib import Path

from papertrack.reports.markdown import repair_presentation
from papertrack.journals.metadata import normalize_doi
from papertrack.journals.storage import cache_roots, relocated_record


@lru_cache(maxsize=4)
def _captures(data_dir: str) -> dict:
    found = {}
    for path in sorted(path for cache in cache_roots(data_dir)
                       for path in cache.glob('*/result.json')):
        try:
            result = relocated_record(json.loads(path.read_text()), data_dir, path.parent)
        except (OSError, ValueError):
            continue
        if result.get('status') != 'complete' or not result.get('report_path'):
            continue
        key = str(Path(result['report_path']).resolve())
        if result.get('updated_at', '') > found.get(key, ({}, None))[0].get('updated_at', ''):
            capture = Path(result.get('capture_path') or path.parent / 'capture.json')
            if not capture.is_absolute():
                capture = path.parent / capture
            found[key] = (result, capture)
    return found


def repair_journal_presentation(content: str, report_path: Path, data_dir: str) -> str:
    match = _captures(str(Path(data_dir).resolve())).get(str(report_path.resolve())) if data_dir else None
    if match:
        from papertrack.journals.capture import read_browser_issue, validate_reading_controls
        from papertrack.config import journal_configs
        from papertrack.reports.markdown import abstract_markdown

        result, capture = match
        cfg = journal_configs[result['journal']]
        data = json.loads(capture.read_text())
        validate_reading_controls(data, Path(str(capture) + '.html'), provider=result['provider'])
        args = dict(provider=result['provider'], code=cfg.get('acs_code') or cfg['slug'],
                    volume=result['volume'], issue=result['issue'], journal_name=cfg['name'])
        old = read_browser_issue(str(capture), render_math=False, **args).entries
        new = read_browser_issue(str(capture), **args).entries
        raw_titles = {normalize_doi(row.get('doi', '')): row.get('title', '')
                      for row in data.get('entries', [])}
        blocks = re.split(r'(?m)(?=^### 10\.)', content)
        for index, block in enumerate(blocks):
            heading = re.match(r'### (10\.\S+)', block)
            if not heading:
                continue
            doi = heading[1].lower()
            if doi not in old:
                continue
            title = re.search(
                r'(?m)^(Title:[ \t]*)([^\n]*(?:\n(?![ \t]*$|(?:Authors|Links|Abstract|Extract):|>|#|%%)[^\n]+)*)',
                block,
            )
            if title and title[2].strip() in {raw_titles.get(doi, '').strip(), old[doi].title.strip()}:
                # Compare exact field contents, including internal line breaks.
                # Only the title span changes; task edits and surrounding notes
                # are not reconstructed from a generated report template.
                block = block[:title.start(2)] + new[doi].title.strip() + block[title.end(2):]
                blocks[index] = block
            quoted = re.search(r'(?m)^> \[!quote\]- [^\n]+\n(?:\n)?((?:>[^\n]*(?:\n|$))+)', block)
            if not quoted:
                continue
            actual = '\n'.join(line[1:].lstrip(' ') for line in quoted[1].splitlines()).strip()
            # Preserve user-edited abstracts. Only upgrade an exact old rendering.
            if unescape(actual) != old[doi].abstract.strip():
                continue
            replacement = '\n'.join('> ' + line for line in abstract_markdown(new[doi].abstract).split('\n')) + '\n'
            blocks[index] = block[:quoted.start(1)] + replacement + block[quoted.end(1):]
        content = ''.join(blocks)
    return repair_presentation(content)

"""Isolated publisher adapter: validate first, render in staging, atomically publish.
No arXiv CLI changes, model imports, or network calls.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
from papertrack.journals.capture import read_browser_issue, validate_reading_controls
from papertrack.config import journal_configs
from papertrack.reports.render import write_journal_entries_to_md


def publish(job, capture, destination):
    cfg = journal_configs[job['journal']]
    provider = cfg.get('provider', 'acs')
    code = cfg.get('acs_code', cfg['slug'])
    result = read_browser_issue(capture, provider=provider, code=code,
                                volume=job['volume'], issue=job['issue'], journal_name=cfg['name'])
    unavailable = set(result.metadata['abstract_unavailable_dois'])
    gaps = [e.doi for e in result.entries.values() if not e.abstract and e.doi not in unavailable]
    if gaps:
        raise ValueError('unresolved_abstracts')
    target = Path(destination).resolve() / provider / cfg['slug'] / job['volume'] / (job['issue'] + '.md')
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(str(target) + '.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        old = target.read_bytes() if target.exists() else b''
        old_members = set(re.findall(r'^###\s*(10\.\S+)', old.decode(), re.M))
        if not {d.lower() for d in old_members}.issubset(result.entries):
            raise ValueError('prior_members_would_be_removed')
        for block in re.split(r'(?=^###\s*10\.)', old.decode(), flags=re.M):
            match = re.match(r'###\s*(10\.\S+)', block)
            if match and re.search(r'(?m)^> \[!quote\]- (?:Abstract|Extract(?:（原文摘录）)?)[ \t]*$', block):
                if not result.entries[match.group(1).lower()].abstract:
                    raise ValueError('prior_abstract_would_be_removed')
        validate_reading_controls(json.loads(Path(capture).read_text(encoding='utf-8')),
                                  Path(capture + '.html'), provider=provider)
        with tempfile.TemporaryDirectory(dir=target.parent) as stage:
            staged = Path(stage) / cfg['slug'] / job['volume'] / (job['issue'] + '.md')
            staged.parent.mkdir(parents=True)
            if old:
                staged.write_bytes(old)
            assert write_journal_entries_to_md(cfg['name'], cfg['slug'], job['volume'], job['issue'],
                                               result.entries, stage, year=job['year'], provider=provider,
                                               acs_code=cfg.get('acs_code', ''))
            new = staged.read_bytes()
            old_checked = set(re.findall(r'^- \[[xX]\].*?\[(10\.[^\]]+)\]', old.decode(), re.M))
            new_checked = set(re.findall(r'^- \[[xX]\].*?\[(10\.[^\]]+)\]', new.decode(), re.M))
            if not old_checked.issubset(new_checked):
                raise ValueError('reading_state_lost')
            if (target.read_bytes() if target.exists() else b'') != old:
                raise ValueError('report_changed_during_render')
            if old:
                backup = target.with_name(target.name + '.' + hashlib.sha256(old).hexdigest()[:12] + '.bak')
                if not backup.exists():
                    backup.write_bytes(old)
            with staged.open('rb') as handle:
                os.fsync(handle.fileno())
            os.replace(staged, target)
    return {'path': str(target), 'entries': len(result.entries),
            'abstracts': sum(bool(e.abstract) for e in result.entries.values()),
            'unavailable': sorted(unavailable), 'sha256': hashlib.sha256(new).hexdigest()}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--job', required=True)
    parser.add_argument('--capture', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(publish(json.loads(Path(args.job).read_text()), args.capture, args.output)))
    except Exception as error:
        print(json.dumps({'error': type(error).__name__}), file=sys.stderr)
        sys.exit(1)

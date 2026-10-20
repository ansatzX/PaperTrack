"""One bounded Codex capture, followed by independent local acceptance.

The CLI owns logs and process lifetime. The agent owns browser access only;
it never publishes reports or changes processed-issue state.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import math
import os
from pathlib import Path
from urllib.parse import urlsplit
import shutil
import signal
import subprocess
import tempfile

from papertrack.journals.capture import read_browser_issue, validate_reading_controls
from papertrack.journals.task import issue_task, issue_url, archive_url, archive_page_task
from papertrack.journals.discovery import validate_latest_capture

logger = logging.getLogger(__name__)
DEFAULT_MODEL = "gpt-6-luna"
DEFAULT_TIMEOUT = 660
RESOURCE_LIMITS = ("MemoryHigh=6G", "MemoryMax=8G", "MemorySwapMax=0",
                   "CPUQuota=200%", "TasksMax=256")


@dataclass
class AgentCapture:
    run_dir: Path
    capture_path: Path
    record: dict

    def save(self, status: str, **details) -> None:
        self.record.update(status=status, updated_at=datetime.now(timezone.utc).isoformat(), **details)
        # Retain both successful and failed captures as local parser evidence.
        candidates = {
            'capture': self.capture_path,
            'issue_html': Path(str(self.capture_path) + '.html'),
            'archive_html': Path(str(self.capture_path) + '.archive.html'),
            'access_html': Path(str(self.capture_path) + '.access.html'),
            'incomplete_html': Path(str(self.capture_path) + '.incomplete.html'),
            'incomplete_issue': Path(str(self.capture_path) + '.incomplete.json'),
            **{name: self.run_dir / filename for name, filename in (
                ('task', 'task.txt'), ('events', 'events.jsonl'),
                ('stderr', 'stderr.log'), ('final', 'final.txt'))},
        }
        self.record['artifacts'] = {
            name: {'path': str(path), 'bytes': path.stat().st_size}
            for name, path in candidates.items() if path.is_file()
        }
        target = self.run_dir / "result.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(target)


def _stop_process(process: subprocess.Popen) -> None:
    """Stop the whole agent process group, including tool subprocesses."""
    def send(sig):
        try:
            if os.name == "posix":
                os.killpg(process.pid, sig)
            else:
                process.terminate() if sig == signal.SIGTERM else process.kill()
        except ProcessLookupError:
            pass

    send(signal.SIGTERM)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    # The leader can exit while descendants are still running.
    send(signal.SIGKILL)
    process.wait()


def _seed_capture(source: Path, target: Path, cfg: dict, volume: str, issue: str):
    """Clone a checkpoint into a new run; never mutate original capture evidence."""
    from papertrack.journals.metadata import normalize_doi

    data = json.loads(source.read_text())
    expected = urlsplit(issue_url(cfg, volume, issue))
    actual = urlsplit(data.get('url', ''))
    paths = {expected.path.rstrip('/')}
    if cfg.get('provider') == 'aip':
        paths.update((f'/aip/{cfg["slug"]}/issue/{volume}/{issue}',
                      f'/{cfg["slug"]}/issue/{volume}/{issue}'))
    rows = data.get('entries', [])
    dois = [normalize_doi(row.get('doi', '')) for row in rows]
    if (data.get('schema') != 'papertrack-browser-issue-v1'
            or data.get('collector') != 'papertrack-browser-v2'
            or data.get('capture_method') != 'fixed_browser'
            or str(data.get('volume')) != str(volume) or str(data.get('issue')) != str(issue)
            or actual.scheme != 'https' or actual.netloc != expected.netloc
            or actual.path.rstrip('/') not in paths
            or not rows or data.get('card_count') != len(rows)
            or '' in dois or len(dois) != len(set(dois))):
        raise ValueError('Resume capture does not match requested formal issue')
    for suffix in ('.html', '.archive.html', '.access.html', '.recent.json',
                   '.incomplete.html', '.incomplete.json'):
        old = Path(str(source) + suffix)
        if old.is_file():
            shutil.copy2(old, Path(str(target) + suffix))
    for suffix in ('.articles', '.readings', '.recent-pages'):
        old = Path(str(source) + suffix)
        if old.is_dir():
            shutil.copytree(old, Path(str(target) + suffix))
    if cfg.get('provider') == 'aps':
        from papertrack.journals.capture import _validate_aps_abstracts
        for row in rows:
            if not row.get('abstract') and row.get('abstract_type') != 'unavailable':
                continue
            try:
                _validate_aps_abstracts({'entries': [row]}, Path(str(target) + '.articles'))
            except (OSError, ValueError):
                # Revisit text lacking matching raw HTML; keep the old source
                # in its original run and preserve the diagnostic history.
                row['abstract'] = ''
                row['abstract_type'] = 'abstract'
                row.pop('unavailable_reason', None)
                data.setdefault('error_history', []).append({
                    'doi': row['doi'], 'reason': 'resume_source_evidence_missing_or_invalid'})
    else:
        from bs4 import BeautifulSoup
        from papertrack.journals.capture import _validate_silverchair_rows, _render_silverchair_reading
        html_path = Path(str(target) + '.html')
        soup = BeautifulSoup(html_path.read_text(), 'lxml') if html_path.is_file() else None
        for row in rows:
            if not row.get('abstract') and row.get('abstract_type') != 'unavailable':
                continue
            try:
                if soup is None:
                    raise ValueError('Issue HTML evidence is missing')
                _validate_silverchair_rows(data, soup, html_path, [row])
                if row.get('abstract_type') in ('abstract', 'extract'):
                    _render_silverchair_reading(row, data, html_path, soup)
            except (OSError, ValueError) as error:
                row['abstract'] = ''
                # Preserve source files in the old run. The new collector must
                # save fresh panel evidence, not reuse a conflicting sidecar.
                from papertrack.journals.entry import JournalEntry
                if JournalEntry.is_non_article(row['doi'], row.get('title', '')):
                    continue
                import hashlib
                sidecar = Path(str(target) + '.readings') / (
                    hashlib.sha256(normalize_doi(row['doi']).encode()).hexdigest() + '.json')
                if sidecar.exists():
                    from uuid import uuid4
                    sidecar.rename(sidecar.with_name(sidecar.stem + f'.previous-{uuid4().hex}.json'))
                row.pop('unavailable_reason', None)
                data.setdefault('error_history', []).append({
                    'doi': row['doi'], 'reason': 'resume_source_evidence_missing_or_invalid',
                    'message': str(error)})
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')


def run_issue_agent(*, journal: str, cfg: dict, volume: str, issue: str,
                    year: int, data_dir: str, capture_path: str = "",
                    model: str = DEFAULT_MODEL, timeout: float = DEFAULT_TIMEOUT,
                    discover_latest: bool = False, archive_page: str = "",
                    resume_capture: str = "") -> AgentCapture:
    """Run once, with no automatic retry, permission escalation or model fallback."""
    url = archive_page or (archive_url(cfg) if discover_latest else issue_url(cfg, volume, issue))
    if archive_page:
        from papertrack.journals.inventory import archive_page_allowed
        if not archive_page_allowed(cfg, archive_page):
            raise ValueError('Invalid archive page URL')
    if not model.strip() or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Agent model and positive finite timeout are required")
    executable = shutil.which("codex")
    if not executable:
        raise RuntimeError("Codex CLI not found on PATH; install/authenticate it and enable its browser tools")
    systemd_run = shutil.which("systemd-run")
    systemctl = shutil.which("systemctl")
    if not systemd_run or not systemctl:
        raise RuntimeError("systemd-run and systemctl are required; refusing an unbounded agent run")
    module = Path(__file__).parents[1] / "browser" / "cua.mjs"
    if not module.is_file():
        raise RuntimeError("Installed package is missing the browser collector")
    explicit_capture = Path(capture_path).resolve() if capture_path else None
    if explicit_capture and explicit_capture.exists():
        raise ValueError("Capture path already exists; import it with --browser-snapshot or choose a new path")
    runs = Path(data_dir).resolve() / ".papertrack-runs"
    runs.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix=f"{journal}-{volume}-{issue}-", dir=runs))
    capture = explicit_capture or run_dir / "capture.json"
    capture.parent.mkdir(parents=True, exist_ok=True)
    run = AgentCapture(run_dir, capture, {
        "journal": journal, "volume": str(volume), "issue": str(issue), "url": url,
        "source": "official", "provider": cfg.get("provider") or "acs", "model": model,
        "capture_path": str(capture), "timeout_seconds": timeout,
    })
    if resume_capture:
        if discover_latest or archive_page:
            raise ValueError('Resume requires a specific formal issue, not discovery')
        try:
            _seed_capture(Path(resume_capture).resolve(), capture, cfg, volume, issue)
        except Exception as error:
            run.save('failed', error=str(error))
            raise
        run.record['resumed_from'] = str(Path(resume_capture).resolve())
    prompt = (archive_page_task(cfg, url, str(capture), max(1, timeout - 60))
              if archive_page else issue_task(
                  journal, cfg, volume, issue, year, str(capture), data_dir,
                  capture_only=True, time_budget_seconds=max(1, timeout - 60),
                  discover_latest=discover_latest))
    prompt += (f"\nExecution boundary: only write {capture}, {capture}.html, "
               f"{capture}.archive.html, {capture}.access.html, {capture}.recent.json, "
               f"{capture}.incomplete.html, {capture}.incomplete.json and collector-owned evidence inside {capture}.articles/, {capture}.readings/ and {capture}.recent-pages/.\n"
               "Do not edit package/repository code, reports, configuration or Git state.\n"
               "Do not install dependencies, create agents, or change the model or permissions.\n"
               "Report exact capability/access failures and stop; retain partial artifacts.\n")
    if resume_capture:
        prompt += '\nThe parent copied a prior fixed-collector checkpoint and source evidence here. Resume that checkpoint with the same fixed collector; do not discard its captured text or edit its JSON manually.\n'
    (run_dir / "task.txt").write_text(prompt, encoding="utf-8")
    command = [executable, "exec", "--model", model, "--sandbox", "workspace-write",
               "--cd", str(run_dir), "--skip-git-repo-check", "--json", "--color", "never",
               "--output-last-message", str(run_dir / "final.txt")]
    if capture.parent != run_dir:
        command += ["--add-dir", str(capture.parent)]
    command += ["-"]
    unit = f"papertrack-agent-{run_dir.name}.scope"
    limits = (*RESOURCE_LIMITS, f"RuntimeMaxSec={math.ceil(timeout)}s")
    command = [systemd_run, "--user", "--scope", "--quiet", "--collect",
               f"--unit={unit}", *(f"--property={limit}" for limit in limits), "--", *command]
    run.save("running", command=command, resource_unit=unit, resource_limits=list(limits),
             browser_resource_boundary="Existing user Chrome is outside this agent cgroup")
    logger.info("Starting Codex %s; run evidence: %s", model, run_dir)
    try:
        with (run_dir / "events.jsonl").open("w", encoding="utf-8") as stdout, \
                (run_dir / "stderr.log").open("w", encoding="utf-8") as stderr:
            process = subprocess.Popen(command, cwd=run_dir, stdin=subprocess.PIPE,
                                       stdout=stdout, stderr=stderr, text=True,
                                       start_new_session=os.name == "posix")
            try:
                process.communicate(input=prompt, timeout=timeout)
            except subprocess.TimeoutExpired:
                _stop_process(process)
                run.save("timeout", exit_code=process.returncode)
                raise RuntimeError(f"Agent timed out; partial capture and logs retained at {run_dir}") from None
            except BaseException:
                _stop_process(process)
                run.save("interrupted", exit_code=process.returncode)
                raise
        run.record["exit_code"] = process.returncode
        with (run_dir / "events.jsonl").open(encoding="utf-8") as events:
            for line in events:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("type") == "thread.started":
                    run.record["session_id"] = event.get("thread_id")
                    break
        if process.returncode != 0:
            raise RuntimeError(f"Codex exited with status {process.returncode}")
        # A successful process or a reassuring final message proves no data quality.
        if not capture.is_file():
            final = run_dir / 'final.txt'
            reason = final.read_text(encoding='utf-8')[:1200] if final.is_file() else 'No agent explanation'
            raise ValueError('No capture produced. Agent reported: ' + reason)
        data = json.loads(capture.read_text(encoding="utf-8"))
        if archive_page:
            from papertrack.journals.inventory import archive_page_allowed, canonical_url
            if (data.get('schema') != 'papertrack-browser-archive-v1' or
                    canonical_url(data.get('requested_url', '')) != canonical_url(url) or
                    not archive_page_allowed(cfg, data.get('url', '')) or
                    not Path(str(capture) + '.html').is_file()):
                raise ValueError('Invalid archive page capture')
            run.save('archive_captured', kind='archive', observed_url=data['url'])
            return run
        if data.get("capture_method") != "fixed_browser" or data.get("collector") != "papertrack-browser-v2":
            raise ValueError("Expected the packaged fixed-browser collector output")
        if discover_latest:
            volume, issue = validate_latest_capture(cfg, data, Path(str(capture) + '.archive.html'))
            run.record.update(volume=volume, issue=issue, url=issue_url(cfg, volume, issue),
                              archive_url=url, discovery='latest_observed_formal_issue')
        result = read_browser_issue(str(capture), provider=cfg.get("provider") or "acs",
                                    code=cfg.get("acs_code") or cfg["slug"], volume=volume,
                                    issue=issue, journal_name=cfg["name"])
        unavailable = set(result.metadata['abstract_unavailable_dois'])
        unresolved = any(not entry.abstract and entry.doi not in unavailable
                         for entry in result.entries.values())
        if unresolved or data.get("remaining_abstracts") != 0:
            raise ValueError("Capture has unresolved abstract gaps")
        html = Path(str(capture) + ".html")
        if not html.is_file() or html.stat().st_size == 0:
            raise ValueError("Rendered issue HTML evidence is missing")
        validate_reading_controls(data, html, provider=cfg.get('provider') or 'acs')
        run.save("validated", article_count=len(result.entries),
                 reading_coverage=result.metadata['reading_coverage'])
        return run
    except Exception as error:
        if run.record.get("status") not in {"timeout", "interrupted"}:
            run.save("failed", error=str(error), exception=type(error).__name__)
        raise RuntimeError(f"Journal agent failed: {error}; evidence: {run_dir}") from error
    finally:
        # Stop detached tool descendants as well; never stop the user's Chrome.
        try:
            subprocess.run([systemctl, "--user", "stop", unit], stdin=subprocess.DEVNULL,
                           capture_output=True, timeout=5, check=False)
        except (OSError, subprocess.TimeoutExpired):
            logger.warning("Could not confirm resource unit cleanup: %s", unit)

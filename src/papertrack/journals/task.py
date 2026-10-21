"""Describe browser tasks for an external agent; no model or browser runtime."""
from __future__ import annotations

import json
from pathlib import Path
import shlex
import sys

from papertrack.journals.entry import validate_issue_placement


_TAB_GROUP_CLEANUP = """Before creating task tabs, record the existing tab group IDs.
In finally, after closing task-created tabs, delete any tab groups created by
this task, including groups automatically created by the browser tool. Use only
the documented browser management API and verify that those groups are gone.
Never delete pre-existing groups or groups containing unrelated user tabs; do not
infer ownership from a group name alone. If cleanup is unavailable or cannot be
verified, report the exact remaining group IDs instead of claiming cleanup.
"""


def issue_url(cfg: dict, volume: str, issue: str) -> str:
    validate_issue_placement(volume, issue)
    if cfg.get("acs_code"):
        return f"https://pubs.acs.org/{cfg['acs_code']}/issue/{volume}/{issue}"
    if cfg.get("provider") == "aip":
        return f"https://pubs.aip.org/aip/{cfg['slug']}/issue/{volume}/{issue}"
    if cfg.get("provider") == "aps":
        return f"https://journals.aps.org/{cfg['slug']}/issues/{volume}/{issue}"
    raise ValueError("No official issue URL configured")


def issue_task(journal: str, cfg: dict, volume: str, issue: str, year: int,
               capture_path: str, data_dir: str, *, capture_only: bool = False,
               time_budget_seconds: float = 600, discover_latest: bool = False) -> str:
    url = archive_url(cfg) if discover_latest else issue_url(cfg, volume, issue)
    capture = str(Path(capture_path).resolve())
    command = shlex.join([
        sys.executable, "-m", "papertrack",
        "journal", "issue", "--journal", journal, "--volume", str(volume),
        "--issue", str(issue), "--year", str(year),
        "--browser-snapshot", capture, "--data-dir", str(Path(data_dir).resolve()),
    ])
    module_path = str(Path(__file__).resolve().parents[1] / "browser" / "cua.mjs")
    completion = (
        "The parent PaperTrack process validates the cache and writes reports. "
        "Do not run the import command or write reports."
        if capture_only else
        f"Only after complete, import with the repository Python environment:\n{command}"
    )
    request = json.dumps({"url": url, "volume": str(volume), "issue": str(issue)})
    discovery = ''
    if discover_latest:
        discovery = f'''First establish archive access, then run one discovery call:
var discovery = await collector.discoverCuaLatestIssue(cua, browserId, tab.id,
  {json.dumps({'archiveUrl': url, 'code': cfg.get('acs_code') or cfg['slug']})},
  {{tab, evidencePath: {json.dumps(capture + '.archive.html')}}});
nodeRepl.write(discovery);
For needs_access, restore access; for archive_not_loaded, wait for loading.
Otherwise stop unless ready. On ready, navigate to discovery.request.url and
establish issue access. Use discovery.request below. Do not invent URLs or issue numbers.
'''
        request = 'discovery.request'
    aps = cfg.get('provider') == 'aps'
    detail = (
        'Create a second task-owned Abstract-page tab as abstractTab; restore access there if challenged.'
        if aps else 'Use issue-page controls; no Abstract-page tab is needed.'
    )
    detail_option = ', abstractTab' if aps else ''
    recent = '' if aps else f'''- needs_recent: retain result.recent_url as recentUrl, navigate the SAME tab
  there and establish access. Use this call instead of the issue call:
  var result = await collector.collectCuaRecentBatch(cua, browserId, tab.id,
    request, {{tab, checkpointPath: {json.dumps(capture)}, recentUrl,
    maxInteractions: 4}}); nodeRepl.write(result);
  For partial, reuse recentUrl. For needs_recent, follow the next observed URL.
  Only exact issue DOIs may be supplemented; never add recent-only papers.
'''
    target = 'the latest formal issue' if discover_latest else f'volume {volume}, issue {issue}'
    return f'''Collect {cfg['name']}: {target}.
Official page: {url}
Capture checkpoint: {capture}
Finish within a {time_budget_seconds:g}-second budget; stop as soon as complete.

RESPONSIBILITY
You establish browser access; packaged code collects data; Python validates it.
Execute directly without delegation. Use only the fixed calls below: no custom
selectors, parsers, abstract-copying loops or manually written capture JSON.
Capture verbatim Abstract/Extract with its original type and unavailable_reason;
never substitute teasers, summaries, TOC image recognition or third-party text.
Formal issue membership is authoritative. inventory_complete means checked
observed membership, not independent proof of publisher completeness.

BROWSER AND ACCESS
Use mcp__cua_repl.js for access and collection in the SAME runtime; read its
current documentation. Keep the real cua, selected browserId and task-owned tab.
Never construct, mock, or overwrite cua or browser/tab handles. Do not switch to
mcp__node_repl.js. Create a fresh issue tab per invocation; rediscover your own
handles on resume. Pass bound tab below; omit visible when creating Chrome tabs.
{detail}
Use ordinary authorized browser verification. Allow automatic verification up to
30 seconds before reassessing; use visible verification controls where supported.
Do not bypass security controls, read profiles/cookies or install transports.
If human action or a runtime capability is required, report the exact blocker.
Before returning an access failure, call the fixed adapter once to retain blocked
page evidence, if the runtime is available. Do not treat it as article data.

FIXED CALLS
Import once in cua_repl:
var collector = await import({json.dumps(module_path)});
{discovery}
After access, use timeout_ms: 120000 and exactly ONE collector call per cua_repl invocation.
Do not loop batches inside a tool call. Keep the same checkpoint throughout.
var request = {request};
var result = await collector.collectCuaIssueBatch(cua, browserId, tab.id, request, {{
  tab, checkpointPath: {json.dumps(capture)}, maxInteractions: 4, saveHtml: true{detail_option}
}});
nodeRepl.write(result);

NEXT STEP
- partial: repeat the same call. Compare consecutive results in the current phase;
  progress is decreased remaining, increased captured, or an advanced recent URL.
  remaining also accounts for missing reading evidence: captured alone is not a
  stall indicator. Start a new comparison baseline when changing phase.
{recent}- needs_access: restore access in the affected tab, then resume the same call.
- needs_attention with abstract_not_loaded, abstract_interaction_failed or
  abstract_visit_failed: inspect access, then retry the SAME call once for that DOI.
  If the same DOI fails again, stop; do not declare its reading unavailable.
- complete: stop collection. {completion}
- needs_extractor_update, wrong_issue or any other error: preserve evidence and
  report it; readable-page parser failures require a package fix, not improvisation.
Stop on two consecutive batches without progress, two identical failures, or the
budget limit. Partial work is not success. Retain checkpoint and all collector
HTML/evidence, including .articles/, .readings/ and recent-page files.

CLEANUP AND RETURN
Do not call browser.nameSession, create tab groups, or name tab groups.
Close EVERY task-created tab on success/failure in finally; keep partial-batch tabs
open. The adapter closes supplied tabs on complete (task_tabs_closed=true): skip
those in finally. After a tool/close error, inspect remaining owned tabs first.
{_TAB_GROUP_CLEANUP}
Preserve unrelated tabs and sessions; report exact tab IDs if cleanup is unverified.
Never mark task tabs as deliverables.
Report status, collector version, counts, errors, checkpoint path and cleanup.
Distinguish Abstract, Extract and missing readings; never invent a report path.
'''


def discovery_task(journal: str, cfg: dict, data_dir: str, from_year: int = 0) -> str:
    url = archive_url(cfg)
    scope = f"formal issues published since {from_year}" if from_year else "the latest complete formal issue"
    return f'''Use your browser tools to discover {scope} of {cfg['name']}.
Start at the official issue archive: {url}
Do not use ASAP/recent articles to infer issue membership. Verify each issue's
actual year, volume, issue and completeness on the publisher site.
For each resolved issue, generate its collection task from this repository:
python main.py journal issue --journal {shlex.quote(journal)} --volume V --issue I --year YYYY --capture-path /absolute/path/to/unique-capture.json --data-dir {shlex.quote(str(Path(data_dir).resolve()))}
Follow that task, save captures incrementally and import only validated results.
Inspect existing reports and reading state before choosing work. Historical
.papertrack_state.json remains a legacy record; do not change it or treat a failed
attempt as processed. Keep discovery bounded; report gaps and required handoffs.
'''


def archive_url(cfg: dict) -> str:
    if cfg.get("acs_code"):
        url = f"https://pubs.acs.org/{cfg['acs_code']}/issue-covers"
    elif cfg.get("provider") == "aps":
        url = f"https://journals.aps.org/{cfg['slug']}/issues"
    else:
        url = f"https://pubs.aip.org/aip/{cfg['slug']}/issue"
    return url


def archive_page_task(cfg: dict, url: str, capture: str, budget: float) -> str:
    module = str(Path(__file__).resolve().parents[1] / 'browser' / 'cua.mjs')
    return f"""Save the official journal archive page for {cfg['name']}.
URL: {url}
Capture checkpoint: {capture}
Use the connected authorized browser in mcp__cua_repl.js. Read its documentation.
Create one task-owned tab, navigate to the exact URL, and establish ordinary access.
Allow automatic verification up to 30 seconds within the {budget:g}-second budget.
Do not bypass browser security controls. If human action is needed, report it.
Do not create/name tab groups, nameSession, or close unrelated tabs.
Use the fixed module only, not custom selectors, extraction code or JSON writing:
var collector = await import({json.dumps(module)});
Then in a separate browser-tool call run exactly one bounded collector call:
nodeRepl.write(await collector.collectCuaArchivePage(cua, browserId, tab.id,
  {{url: {json.dumps(url)}}}, {{tab, capturePath: {json.dumps(capture)}}}));
Use the real cua object in the SAME cua runtime. Retain the selected browser ID
as browserId and the real task-owned tab as tab; pass tab to reuse its existing
binding. The adapter supports cua.getTab(tabId, {{browser: browserId}}).
Never construct/mock/overwrite cua, agent, browsers, browser.tabs, or tab objects.
Use documented cua.getBrowser for browser selection if needed; it creates no tab.
For Chrome createBrowserTab calls, omit the in-app-only visible option.
If needs_access, establish access then resume the same fixed call. Preserve failure HTML.
On captured, stop. Python parses dates, discovers archive links and checks the cache.
Do not collect any issue, guess issue numbers, follow other links or write reports.
Close the task-owned tab in finally on success/failure; report unverified cleanup.
{_TAB_GROUP_CLEANUP}
"""

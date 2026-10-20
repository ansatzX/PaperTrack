"""Describe browser tasks for an external agent; no model or browser runtime."""
from __future__ import annotations

import json
from pathlib import Path
import shlex
import sys


_TAB_GROUP_CLEANUP = """Before creating task tabs, record the existing tab group IDs.
In finally, after closing task-created tabs, delete any tab groups created by
this task, including groups automatically created by the browser tool. Use only
the documented browser management API and verify that those groups are gone.
Never delete pre-existing groups or groups containing unrelated user tabs; do not
infer ownership from a group name alone. If cleanup is unavailable or cannot be
verified, report the exact remaining group IDs instead of claiming cleanup.
"""


def issue_url(cfg: dict, volume: str, issue: str) -> str:
    if not str(volume).isdigit() or not str(issue).isdigit():
        raise ValueError("Formal volume and issue must be numeric")
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
    completion = ("Once complete, return the checkpoint path and counts. The parent PaperTrack process\n"
                  "will independently validate and import it. Do not run the import command or write reports."
                  if capture_only else
                  f"Once complete, import with the repository Python environment:\n{command}")
    request = json.dumps({"url": url, "volume": str(volume), "issue": str(issue)})
    discovery = ''
    if discover_latest:
        discovery = f'''
First establish access to the official archive above. Import the fixed module:
var collector = await import({json.dumps(module_path)});
Then, in a separate browser-tool invocation, run exactly one discovery call:
var discovery = await collector.discoverCuaLatestIssue(cua, browserId, tab.id,
  {json.dumps({'archiveUrl': url, 'code': cfg.get('acs_code') or cfg['slug']})},
  {{tab, evidencePath: {json.dumps(capture + '.archive.html')}}});
nodeRepl.write(discovery);
If needs_access, establish access and repeat. Any other non-ready status is a
visible failure: stop, preserve evidence, and close task tabs. Do not invent URLs,
issue numbers or archive parsers. Once ready, navigate your task-owned tab to
discovery.request.url and establish issue access. Use discovery.request below.
The archive evidence and capture must come from this same invocation.
If reason is archive_not_loaded, allow the existing page to finish loading before
repeating the fixed discovery call. An empty loading page is not evidence of an
empty journal archive. Keep the same bounded task budget and no-progress limit.
'''
        request = 'discovery.request'
    detail = ("For APS, create a second task-owned Abstract-page tab and pass the real handle as abstractTab. "
              "If an Abstract page is challenged, handle access in that tab and resume the collector."
              if cfg.get("provider") == "aps" else "ACS/AIP use controls on the issue page; no detail tab is needed.")
    detail_option = ", abstractTab" if cfg.get("provider") == "aps" else ""
    target = ('the latest formal issue' if discover_latest else f'volume {volume}, issue {issue}')
    return f'''Obtain browser access to {cfg['name']}: {target}.
Official issue page: {url}
Capture checkpoint: {capture}

Your responsibility is access: connect the user's authorized browser, navigate to
this issue, handle access challenges and recover the connection. Execute directly;
do not delegate or wait for another agent. Create a new task-owned issue tab for
each new invocation; do not claim tabs owned by another agent session. Rediscover
your own tabs on resume instead of
assuming old IDs/variables remain valid. Preserve unrelated tabs and user sessions.
Do not call browser.nameSession, create tab groups, or name tab groups. Do not
mark task tabs as deliverables or handoffs. Close EVERY task-created issue and
Abstract tab before returning, on success or failure; use try/finally for cleanup.
{_TAB_GROUP_CLEANUP}
The adapter closes both supplied tabs on complete and returns task_tabs_closed=true.
Do not close those tabs again. Cleanup belongs at the end of the task, not after
every partial batch. If a tool reports an error after closing a tab, inspect your
remaining task-owned tabs before attempting another close; preserve the capture.
If the browser disconnects and cleanup cannot be confirmed, report exact tab IDs.
If a challenge needs human action you cannot complete, retain progress and report
that exact blocker. Do not install transports or read browser profiles/cookies.
The user has authorized normal browser verification for these journal pages.
An automatic "Performing security verification" screen is not by itself a
request for human input: allow it up to 30 seconds to settle, then inspect again
within this invocation's total budget. If a visible verification control appears,
use the normal browser UI where supported. Do not bypass browser security controls.
Before returning an access failure, call the fixed discovery/collection adapter
once on the blocked page with evidence saving enabled; its access gate retains
the HTML without treating it as article data. Two immediate unchanged snapshots
are not evidence that an automatic verification has exhausted that wait.
{discovery}

AFTER access, use the fixed collector below. Do not write selectors, HTML parsers,
DOI extraction code or abstract-copying loops. Do not manually produce capture JSON.
Use mcp__cua_repl.js for BOTH access and the module call below. Read its current
documentation first. Retain the actual selected browser identifier as browserId
and the real task-owned tab returned by cua.createBrowserTab or cua.getTab as tab.
Use the existing cua object directly. Never construct, mock, or overwrite cua,
agent, browsers, browser.tabs, or tab objects to imitate a missing API.
The adapter supports cua.getTab(tabId, {{browser: browserId}}); pass the already
bound tab below to avoid repeated getTab calls emitting large UI snapshots.
If selecting a browser is needed, use the documented cua.getBrowser entry point;
getBrowser alone does not create a tab. When opening Chrome tabs, omit the
visible option (it is only supported for the in-app browser). {detail}
mcp__node_repl.js is a DIFFERENT REPL and does not inherit browser handles.
If the real cua runtime or module import is unavailable, report that exact
capability failure; do not fabricate a runtime or replace the fixed collector.

Run in the supported browser tool runtime:
Use timeout_ms: 120000 and exactly ONE collector call per cua_repl invocation.
Do not wrap multiple batches in a loop: the outer tool deadline can expire during
a later click even though each individual collection batch is bounded.
var collector = await import({json.dumps(module_path)});
var request = {request};
var result = await collector.collectCuaIssueBatch(cua, browserId, tab.id, request, {{
  tab, checkpointPath: {json.dumps(capture)}, maxInteractions: 4, saveHtml: true{detail_option}
}});
nodeRepl.write(result);

This fixed code reads the real DOM, follows aria-controls (control IDs are opaque),
expands Abstract/Extract panels, saves a bounded batch and retains prior text. It
never launches browsers, solves challenges, makes publisher HTTP calls or invokes a model.
HTML is saved at checkpoint + '.html' for parser diagnosis without another fetch.
APS abstract detail HTML is saved by the collector inside checkpoint + '.articles/'.
ACS/AIP reading panels are saved verbatim by the collector inside checkpoint + '.readings/'.
Retain these files: closing a panel may remove its contents from the final issue HTML.
Keep these files together with the checkpoint; Python validates their DOI and abstract text.
The stable schema is papertrack-browser-issue-v1; title, authors, DOI, citation,
article URL, verbatim Abstract/Extract, original type and unavailable_reason come
from fixed code. Only official formal-issue members belong in the reading ledger.
Do not substitute teasers, summaries, ASAP entries or third-party metadata.
Report Abstract, Extract and missing reading text separately. An Extract is not
a complete Abstract; captured counts include both kinds of reading text.

Handle result.status:
- partial: repeat the SAME module call and checkpoint while remaining decreases
  OR captured increases. Retained text can already be counted as captured while
  its missing raw panel evidence is being saved; an unchanged captured count alone
  is NOT a stall. Compare consecutive batch results, not the original checkpoint's
  remaining count, which may predate required evidence checks. For recent batches,
  use their remaining count and next observed page URL to track progress.
- needs_recent: the fixed collector found an official latest-articles link and
  issue members with no reading control. Navigate the SAME task-owned tab to
  result.recent_url and establish access. Then use exactly one call per tool turn:
  result = await collector.collectCuaRecentBatch(cua, browserId, tab.id,
    request, {{tab, checkpointPath: {json.dumps(capture)}, recentUrl: result.recent_url,
    maxInteractions: 4}}); nodeRepl.write(result);
  Retain the recent URL in a variable before calling. For partial, repeat this
  recent call on that same URL. For needs_recent, follow its next observed URL.
  Fixed code targets only exact issue DOIs, saves raw HTML and follows observed
  rel=next links (at most 20 pages). Never match titles or add recent-only papers.
  Missing Abstracts after the observed pages are exhausted remain needs_attention.
- needs_access: handle access in the relevant tab, then resume the SAME checkpoint.
- needs_attention with abstract_not_loaded, abstract_interaction_failed or
  abstract_visit_failed: inspect actual access if needed, then retry the SAME
  fixed module call once for that failed DOI. Keep the checkpoint and error history.
  If the same DOI fails again, stop and report it; never declare it unavailable.
- needs_extractor_update, wrong_issue, or other needs_attention on a readable page:
  preserve evidence and return the exact status/errors; do not improvise a parser.
- complete: local validation/import may proceed.
Stop after two repeated identical failures, or two consecutive batches with neither
decreased remaining nor increased captured (nor an advanced recent page URL), or
a {time_budget_seconds:g}-second budget.
A partial checkpoint is not a completed issue. Fixed inventory_complete records a
checked observed inventory, not independent proof that publisher coverage is complete.

{completion}
Do not edit production Markdown directly or mark failed work processed. Report
collector version, counts, errors, checkpoint path and actual report path. The
program validates records and preserves reading/collection state before publication.
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

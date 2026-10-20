# Agent access → fixed browser collector

The agent establishes access to the official issue in a supported browser session.
Fixed repository code then expands abstracts, parses the rendered DOM and saves
structured data. It never launches a browser, reads profiles/cookies, calls an LLM,
solves challenges or refetches the publisher through HTTP.

## Keep the integration small

Use the existing Codex browser tool and one public handoff:
`collectCuaIssueBatch(...)`. Keep browser access with the agent, deterministic
DOM collection and checkpoints in these modules, and validation/report state in
Python. Do not add a custom browser MCP server, Python browser transport, model
SDK or autonomous retry service to this path. The older `experiments/journal-workflow/`
prototype is optional and is not required by this handoff.

Reliability means bounded batches, resumable atomic checkpoints, explicit
failures and preservation of prior reports. A readable page with a changed layout
requires a collector fix and regression fixture; repeating or expanding the
agent prompt must not replace the fixed parser.

The implementation ships in `src/papertrack/browser/` as Python package data.
The modules in this repository directory forward to that implementation for
existing callers and tests. `papertrack journal issue --run-agent` invokes Codex
and supplies the installed absolute module path; it adds no browser transport.
Task tabs must not be grouped/named or marked as deliverables. The CUA adapter
closes the supplied task-owned issue/Abstract tabs when collection completes;
the agent must also clean up on failure and report tab IDs if that is impossible.

## Handoff

In Codex, use **the same `cua_repl` runtime** for access, module imports and
collection. `node_repl` is a different process and does not inherit browser handles.
After selecting a browser through the documented CUA API, retain its `browserId`
and the real task-owned `tab`. Pass the actual `cua` runtime and reuse that tab;
the adapter can also resolve it through `cua.getTab(tabId, {browser: browserId})`.
Never construct mock runtime or tab objects. The legacy `agent.browsers` interface
remains supported; neither interface opens another browser transport.

After the agent has opened the official issue and resolved access, run in cua_repl:

```js
var collector = await import('/absolute/repo/browser/cua.mjs');
var request = {url:'https://pubs.acs.org/jcisd8/issue/66/12',volume:'66',issue:'12'};
var result = await collector.collectCuaIssueBatch(cua, browser.browserId, tab.id, request, {
  tab, checkpointPath:'/absolute/repo/.work/cache/jcim-66-12.json', maxInteractions:4, saveHtml:true,
});
nodeRepl.write(result);
```

Local ES-module imports and Node filesystem APIs must be available in cua_repl.
The API is not a plain Node browser launcher. A missing runtime capability is a
visible failure, not permission to replace the parser with model-generated code.
For another compatible browser host, `collect.mjs` also exposes
`collectIssueBatch(domTab, request, options)` for an existing tab with
`goto`, `playwright.evaluate` and locator `click/getAttribute` methods.

Only repeat while progress increases, with the same checkpoint path. Each call
checks access and current membership, loads saved text, attempts at most four
controls by default (allowed budget 1–8), closes floating panels, and atomically
replaces the checkpoint. The collector has a 30-second batch deadline, with an
in-flight wait allowed to extend it. The caller owns one tab/checkpoint at a time;
do not run concurrent collectors against the same checkpoint.

For APS provide `abstractTabId` to the CUA adapter (or `abstractTab` to the lower-level
collector), a separate task-owned tab for linked Abstract pages. The official issue controls membership; summaries are not abstracts. No
PDF/full-text links are followed. If an Abstract page is challenged, the failed
visit remains in errors for the agent's access recovery. Other articles retain
progress. ACS/AIP use issue-page controls and require no second tab.

## Status and ownership

| Status | Next owner/action |
|---|---|
| `needs_access` | Agent handles navigation/challenge or requests human handoff. No checkpoint overwritten. |
| `partial` | Agent repeats the same fixed module call while captured increases. |
| `needs_attention` | Inspect the stage. Access/interaction failures allow one repeat of the same fixed call per failed DOI; repeated failure stops. Readable-page extraction defects require a code/test fix. |
| `needs_extractor_update` | Preserve HTML and fix repository extraction code; no runtime parser invention. |
| `wrong_issue` | Agent opens the requested journal/issue. |
| `complete` | Python validates and renders. This is observed-inventory consistency, not an independent completeness oracle. |

`access.mjs` performs a read-only readiness probe. `dom.mjs` contains the fixed
Silverchair ACS/AIP parser. `issue.mjs` and `aps_issue.mjs` own deterministic
interaction. Silverchair panels are located by the actual `aria-controls`
attribute: opaque control IDs must never be transformed into guessed panel IDs.

Errors carry the DOI and interaction stage; historical errors are retained.
Malformed or changed checkpoints are rejected. New tabs and reordered inventories
can resume captured DOI text. Already captured abstracts are not revisited.

`saveHtml:true` writes `<checkpoint>.html` on the first batch, errors and completion from the rendered issue DOM for local
parser diagnosis. `html.mjs` reads 32,000-character chunks and checks their lengths
and the total size, avoiding the browser API's observed large-string truncation.
Failed exports preserve previous HTML. It may contain institution-specific page text; keep it in the
private task/cache directory. It does not export cookies or a browser profile.
If access is blocked later, the previous valid checkpoint and HTML survive.

## Import

```bash
python main.py journal issue --journal jcim --volume 66 --issue 12 --year 2026 \
  --browser-snapshot /tmp/jcim-66-12.json --data-dir /tmp/papertrack-review
```

The fixed collector produces the existing capture schema with
`capture_method:"fixed_browser"` and `collector:"papertrack-browser-v2"`.
Python validates requested issue identity, DOI inventory and all available
Abstract/Extract fields before writing. Publisher Extracts keep their source type;
missing controls have an observed unavailable reason. Prior report members,
abstracts, anchors and reading state are preserved.

`issue.mjs` also retains the lower-level `captureIssue(tab,request,options)` API
for the older worker. New tasks should use `collectIssueBatch`, which adds access
checking and disk checkpoints and always sets `navigate:false`. Browser access
belongs to the agent, not the deterministic collector.

## Verification

```bash
node --test browser/*.test.mjs
pytest -q
```

See VALIDATION.md for dated live evidence. Tests do not establish current
publisher access or universal layout compatibility.

Python derives report reading text from the retained HTML after source validation.
`reports/reading.py` preserves native superscripts/subscripts, supported MathML and
rendered MathJax structures using explicit text notation (`10^{13.9}`, `M_{500c}`,
`(a)/(b)`, `√(x)`). Raw captures remain unchanged. Unsupported math structures
raise an error rather than silently flattening them; cached HTML supports local
parser regression checks without additional publisher traffic.

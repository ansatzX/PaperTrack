# Journal workflow (optional, isolated)

This package adds a conventional journal executor and an optional recovery agent.
It does not modify or import itself into the existing arXiv command path. Existing
`main.py arxiv ...`, cron scripts, Python dependencies and date-bucket logic stay
as they were. Install the separate Node package only when using this workflow.

## Boundaries

```
cli.mjs (composition)
  -> runner.mjs (Crawlee queue, bounded retries, one writer)
       -> executor.mjs (capture/checkpoint/validate; no models)
            -> BrowserBridge -> browser-driver.mjs -> supplied browser-client tabs
       -> render.py -> existing PaperTrack validator/renderer
  -> agent.mjs (optional recovery decisions)
       -> model-provider.mjs -> official OpenAI SDK -> OpenAI or DeepSeek
       -> inspect tool port / retry decision / require_human decision
```

Execution and agent policy share only explicit data contracts. The execution
module never imports SDKs, prompts or agents. Model code cannot edit the queue,
report, extraction rules, or files. It chooses `inspect`, `retry`, or
`require_human`; only the outer runner applies that decision. Unknown actions,
invalid model output, request failures and exhausted budgets pause visibly.
Model-driven source-code repair is deliberately outside the runtime interface.

Crawlee supplies queue storage and bounded request retries. `proper-lockfile`
supplies an exclusive run-directory lease. Captures are atomically replaced
(one article per browser call), with DOI membership and existing content checked
on resume. Queue state is under the run directory, not shared through the cwd.
Each invocation records an attempt; a new attempt resumes article checkpoints.
A process crash can leave a lease until its 10-second stale threshold expires.
Do not run multiple browser drivers for the same run directory.

`render.py` uses the existing publisher validator and report renderer in a
staging directory. It protects prior members, existing abstracts and checked
reading state, makes a content-addressed backup, and atomically replaces the
report. A per-report OS lock coordinates workflow publishers. Existing legacy
writers do not use this lock: do not run old and new writers concurrently against
the same output. The workflow does not update `.papertrack_state.json`.

## Install

Node **22+** and the existing PaperTrack Python environment are required.

```bash
cd experiments/journal-workflow
npm ci --ignore-scripts
npm test
```

The lockfile pins dependencies. `node_modules` and local runs are ignored. No
OpenAI SDK or Crawlee dependency is added to PaperTrack's Python installation.

## Start a job

Use `example-job.json` or create a job with explicit journal, URL, volume, issue
and year. Supported job keys: jcim, jctc, jcp, prl. URLs must identify a formal
issue on the configured publisher. Natural-language/latest-issue discovery stays
with the caller; this interface intentionally accepts resolved tasks only.

```bash
node cli.mjs --job example-job.json --dir /tmp/jcim-job \
  --output /tmp/papertrack-output --python /path/to/arxiv/env/bin/python \
  --agent off
```

The worker waits for its browser driver. In the supported Chrome tool runtime,
first connect using the installed browser skill and supply an owned tab:

```js
var workflow = await import('/absolute/repo/experiments/journal-workflow/browser-driver.mjs');
var tab = await browser.tabs.new();
var driver = workflow.createBrowserDriver({tab});
// For APS also supply abstractTab: await browser.tabs.new().
await workflow.serveBrowser('/tmp/jcim-job', driver, {durationMs:25000});
```

The browser host continues calling `serveBrowser` until `status.json` is
`complete` or `needs_attention`. It only transports commands; it does not decide
which article or batch to run. Calls yield for tool-runtime time limits. The
worker owns progression. A request expires after 90 seconds without a browser
response. Matching IDs prevent stale responses satisfying new requests.

**This is not an unattended Chrome launcher.** The supplied, live browser-client
connection is still required and is not serializable. The worker can run without
a model, but cannot capture new pages without its browser host. Browser/profile
setup, reconnection and human login remain host responsibilities. No cookies or
profile credentials are copied, and no separate CDP connection is opened.

After a lost tab/connection, reconnect through the supported host, create fresh
tabs/driver, and rerun the same CLI command and directory. Article content already
saved is retained. Repeating a completed job with an unchanged output returns its
result without browser or model work; a changed reading checkbox triggers a
render from the saved capture. To refresh publisher contents, start a new run
directory. Prior-report checks still apply at publication.

## Optional agent

```bash
# Uses DEEPSEEK_API_KEY from the runtime; do not put keys in job files.
node cli.mjs --job example-job.json --dir /tmp/jcim-job \
  --output /tmp/papertrack-output --python /path/to/python \
  --agent deepseek --model deepseek-flash

# Uses OPENAI_API_KEY. Official public model ID verified in documentation.
# --agent openai --model gpt-5.6-luna
```

Normal collection performs **zero model requests**. After two conventional
capture attempts fail, the optional agent receives allowlisted issue/error/count
information. It can request the task tab's bounded DOM, suggest one additional
retry, or pause. Maximum 3 model decisions per invocation; SDK timeout 30 seconds,
SDK retries disabled. Missing credentials do not break normal capture and return
`model_unavailable` if recovery is needed. Raw API exception bodies are not logged.

The OpenAI provider has opt-in image input (`vision:true` plus an explicitly
supplied image), contract-tested. The default runner sends text/DOM only; automatic
screenshot escalation is not implemented in this version. DeepSeek's adapter is
text-only here even if a selected model has additional capabilities. No abstract
rewriting, arbitrary shell/browser actions, or hidden self-modifying loop.

The normal job requires every available Abstract/Extract to be captured. A
missing control is retained as explicitly unavailable in report metadata. This
workflow's renderer uses the captured official data without network enrichment;
the existing `main.py journal issue --browser-snapshot ...` is now also local-only.
Without a snapshot, that command emits an external-agent task; it does not launch this worker.

## Validation

```bash
npm test
/path/to/arxiv/env/bin/python -m pytest test/test_render.py -q
# Explicit live-model smoke (real API requests; synthetic failure evidence):
node test/live-model.mjs deepseek /tmp/model-evidence.json
```

See [VALIDATION.md](VALIDATION.md) for actual acceptance and limitations.

References: [Crawlee BasicCrawler](https://crawlee.dev/js/api/basic-crawler/class/BasicCrawler),
[OpenAI SDK](https://developers.openai.com/api/docs/quickstart),
[GPT-5.6 Luna](https://developers.openai.com/api/docs/models/gpt-5.6-luna),
[DeepSeek API](https://api-docs.deepseek.com/guides/function_calling).

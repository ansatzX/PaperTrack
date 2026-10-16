# Connected-browser issue capture

`issue.mjs` uses a supplied browser-client tab to open a formal issue page,
click its Abstract buttons, and extract the displayed title, DOI, authors,
citation and abstract. It does not read browser profiles/cookies or visit
PDF/full-text links. Browser connection and any interactive login stay with
the caller. A new empty automated browser is not equivalent to the browser
session where the user can access the publisher.

The collector supports the observed Silverchair ACS/AIP layouts and APS issue
pages. ACS/AIP expand abstracts in the issue page; APS keeps the issue tab open
and visits only its linked Abstract pages in a second tab. APS issue summaries
are never treated as abstracts. Live samples and counts are in
[VALIDATION.md](VALIDATION.md); this is not a promise of universal access.

## Run from an agent's supported browser session

First establish the browser and read its documentation through the installed
Chrome/browser skill. Reuse the chosen tab and supported browser API. Then:

```js
var { captureIssue } = await import('/absolute/path/arxiv_reading/browser/issue.mjs');
var fs = await import('node:fs/promises');
var request = {url:'https://pubs.acs.org/toc/jcisd8/66/12', volume:'66', issue:'12'};
var capture = await captureIssue(tab, request);
await fs.writeFile('/tmp/jcim-66-12.json', JSON.stringify(capture, null, 2));
```

Each call attempts at most four still-missing Abstract controls and has a
30-second batch budget (an in-flight click/abstract wait can extend this).
While `remaining_abstracts > 0` and the prior call made progress, call again
with `{navigate:false, previous:capture}` and save the new capture. Each expanded panel is read then closed so it cannot cover the next control.
Already captured abstracts are retained in the passed capture; do not reload between batches. Inspect interaction errors instead
of endlessly retrying. The caller finalizes/releases its tabs when finished.
An interrupted connection produces an error, never an invented empty issue.

This is an actual browser interaction module for an agent-controlled session,
not a standalone Node CLI or a cron browser process. `--browser-snapshot` below
imports its output; that flag alone does not start/control Chrome.

For APS supply a second browser-owned tab:

```js
var abstractTab = await browser.tabs.new();
var capture = await captureIssue(issueTab,
  {url:'https://journals.aps.org/prl/issues/136/1', volume:'136', issue:'1'},
  {abstractTab});
// Resume with {navigate:false, abstractTab, previous:capture} and save each batch.
```

APS deduplicates highlighted papers by DOI. It validates the visited page's DOI
before accepting its abstract and preserves rendered MathJax text. It does not
click PDF or full-text links; the publisher may itself render article text below
the Abstract section, which this collector does not extract.

## Generate the existing PaperTrack report

```bash
python main.py journal issue --journal jcim --volume 66 --issue 12 --year 2026 \
  --browser-snapshot /tmp/jcim-66-12.json --data-dir /tmp/papertrack-browser-review
```

Python rejects wrong publisher URLs, wrong issue headings/citations, duplicate
DOIs, missing card identities and inconsistent card counts. It then preserves
this official member set and fills missing fields through the third-party
pipeline. Missing abstracts remain explicit; publisher Extracts fill the same reading abstract field, with their original type retained in provenance.
The Markdown retains checkbox and collection state through the existing writer.
Do not promote a fixture-based test or a single successful abstract into a
claim that the full live issue was captured.

## Complementary sources

Europe PMC → Crossref → PubMed fill empty fields, preserving chosen values and
recording disagreements. When official access fails they contribute a normalized
DOI union, restricted to exact volume/issue. OpenAlex and Semantic Scholar only
enrich abstracts for existing DOIs in batches of 50. Empty/error responses are
not successful empty issues. OpenAlex snippets below 40 words are rejected as a
conservative fragment filter; this may also reject unusually short real abstracts.
Optional `OPENALEX_API_KEY` and `SEMANTIC_SCHOLAR_API_KEY` are read at runtime and
never written into snapshots or logs. No key is required to attempt either API.

API references: [Europe PMC](https://europepmc.org/RestfulWebService),
[OpenAlex authentication and batch limits](https://help.openalex.org/api/authentication/),
[Semantic Scholar Graph API](https://api.semanticscholar.org/api-docs/graph),
[Crossref REST API](https://www.crossref.org/documentation/retrieve-metadata/rest-api/).

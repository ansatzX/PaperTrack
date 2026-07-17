# Official Journal Source Modes Design

## Goal

PaperTrack must obtain journal issue membership and primary metadata from the
official ACS, AIP, and APS issue surfaces during development. Stable scheduled
runs may opt into CrossRef metadata completion through an environment variable,
but CrossRef must never define issue membership or replace official title and
author metadata.

The implementation must continue to minimize publisher traffic: one official
issue page should provide the issue membership and most metadata. PaperTrack
must not visit individual article pages. AIP's official issue-page abstract
AJAX endpoint is allowed when the issue page exposes an article ID.

## Source Modes

PaperTrack exposes one explicit policy setting:

```text
PAPERTRACK_CROSSREF_MODE=disabled|metadata
```

- The unset/default value is `disabled`. This is the development mode. No
  CrossRef request is allowed in this mode.
- `metadata` permits CrossRef to fill missing abstracts by exact DOI after an
  official adapter has established the issue membership and official metadata.
- Any other value is a configuration error. PaperTrack must stop rather than
  silently selecting a weaker source policy.
- `run.sh` is the only repository-provided workflow that enables `metadata`.
- CrossRef cannot add an article, remove an article, or replace official DOI,
  title, author, volume, issue, page, or date metadata in either mode.

The CLI parses the environment setting once and passes a structured policy to
the report and journal-fetch layers. Provider adapters do not read environment
variables directly.

## Official Provider Boundaries

### ACS

- The formal ACS TOC URL defines issue membership.
- The adapter extracts DOI, title, authors, abstract, volume, issue, pages, and
  date from each official issue item.
- Issue publication information, editorial mastheads, and similar issue-level
  metadata records are not reading units.
- A valid issue response that contains zero recognized articles is a suspicious
  parse failure, not an empty successful issue.

### AIP

- The formal AIP issue URL defines issue membership and official DOI, title,
  author, volume, issue, page, and date metadata.
- When an issue item exposes `data-articleid`, PaperTrack may request its
  abstract from AIP's official journal-scoped endpoint, for example
  `/aip/jcp/PlatformArticle/ArticleAbstractAjax`. The current unscoped
  `/PlatformArticle/ArticleAbstractAjax` URL is not equivalent.
- PaperTrack does not visit article detail pages.
- Absence of an abstract control is distinct from an AJAX request failure and
  from an AJAX success response with no abstract.
- Direct HTTP is attempted first. If the official issue page returns a
  Cloudflare challenge, PaperTrack may make one headful Playwright fallback in
  an Xvfb display. That browser load blocks images, fonts, and media, retains
  scripts required by the publisher challenge, and is limited to the issue URL
  plus its official abstract AJAX requests.
- The browser context is reused for every abstract control in that issue and is
  then closed. A successful issue result is cached, so the same run does not
  repeat the browser challenge.

### APS

- The formal APS issue URL defines membership and metadata.
- The parser accepts current and historical title heading levels through a
  semantic `.title a[href*=...]` selector rather than assuming `h3`.
- Summary text embedded in the issue card is the official abstract source.
- Historical issue-page fixtures for PRL 127/27 and 129/27 protect the heading
  compatibility regression.

## Respectful Network Policy

Publisher requests are bounded and observable.

- Identify the client as PaperTrack where the HTTP library permits it.
- Fetch one issue page per issue and cache a successful parsed result for the
  process lifetime.
- Reuse a provider session and cookies within an issue fetch.
- Do not rotate through a Cartesian product of HTTP clients, fingerprints, and
  retry rounds.
- Use at most three issue-page network attempts for transient failures.
- Stop immediately on an identified challenge page unless the response supplies
  a retry interval. AIP may make its single bounded browser fallback; other
  providers do not intensify requests to defeat a challenge.
- Honor `Retry-After`; otherwise use bounded exponential backoff.
- Enforce a provider request interval. The initial floors are 1 second for ACS,
  10 seconds for APS, and 1 second for AIP. A larger configured interval wins.
- AIP abstract requests are sequential, cached by article ID, and limited to
  entries that advertise an official abstract control.
- Robots and publisher behavior are production evidence, not static truths.
  Diagnostics record the URL, status, challenge marker, attempt count, and
  effective delay so policy can be revised without guessing.

If a publisher explicitly blocks the request, PaperTrack records `blocked` and
leaves existing output and processed state unchanged.

## Result And Failure Model

An adapter result distinguishes:

- complete official success;
- official success with legitimate or unresolved missing optional fields;
- blocked or rate-limited fetch;
- transport failure;
- suspicious official-page parse failure.

Field-level diagnostics include source, provider, journal, volume, issue, DOI,
field name, stage, outcome, URL, and status/exception when applicable. In
particular, AIP records separate outcomes for:

- no official abstract control;
- official abstract request blocked or failed;
- official abstract response empty;
- official abstract parsed successfully;
- CrossRef metadata fill, when production mode permits it.

An absent abstract does not automatically invalidate a report because
editorials, corrections, comments, replies, and publisher notes may not have
abstracts. Missing DOI, title, or issue membership evidence is not optional.

## Report And State Safety

- Official failure never writes a new empty report.
- Official failure never marks an issue processed.
- A prior valid report survives later fetch failures.
- Existing task completion and supported Obsidian task suffixes survive a
  successful rebuild.
- Reports generated under the old CrossRef-membership behavior are repair
  candidates when their provenance or known metadata shape exposes that origin.
- Diagnostics and `.papertrack_state.json` remain local runtime files and are
  ignored by Git.

## Test Strategy

Tests mock all network and Zotero access unless they parse a saved publisher
fixture.

1. Source-policy tests prove the default makes zero CrossRef calls, `metadata`
   permits abstract-only completion, invalid values fail, and membership never
   changes.
2. ACS tests cover official issue extraction, challenge detection, bounded
   retries, and JCIM 66/12's two official entries without abstracts.
3. AIP tests cover issue extraction, article-ID discovery, each abstract outcome,
   sequential throttling, cache behavior, direct-HTTP-to-browser fallback,
   journal-scoped AJAX URLs, blocked heavy browser assets, and no article-page
   requests.
4. APS tests parse both current `h3` and historical `h4` issue cards and verify
   the PRL 127/27 and 129/27 snapshots.
5. Report tests prove failed official fetches preserve files and processed state.
6. A limited live verification command runs in the `arxiv` conda environment
   against one issue per provider. It reports observations and request counts;
   publisher blocking is reported as external evidence, not converted into a
   passing fetch.

## Acceptance Criteria

- Development/default execution does not call CrossRef.
- `run.sh` explicitly enables CrossRef metadata-only completion.
- No code path uses CrossRef issue membership for configured ACS, AIP, or APS
  journals.
- All three adapters obtain membership and primary metadata from official issue
  surfaces and never visit article pages.
- APS historical heading variants parse correctly.
- AIP abstract failures are visible per DOI and do not trigger article-page
  crawling.
- Publisher blocking preserves prior reports and state.
- Full tests and syntax checks pass in the `arxiv` conda environment.

# Unattended Official-Issue Fetch Design

## Goal and source boundary

PaperTrack must rebuild ACS, AIP, and APS journal reports without human
participation, using only publisher-owned issue pages and the official resources
loaded by those pages. CrossRef is not part of this journal pipeline, including
metadata fill. Success rate is the evaluation metric; it does not relax the
source boundary.

An issue is successful only when PaperTrack obtains an official issue
representation, establishes the issue membership from it, parses the required
metadata, and passes the report completeness checks. HTTP 200, a rendered
challenge page, or a partial article list is not success.

## Architecture

`run_j.sh` continues to start one worker per publisher in parallel. Each worker
starts one Chrome Stable process through Playwright using a publisher-specific
persistent profile. The Chrome process lives only for that run; the profile
survives across runs.

```text
run_j.sh
  +-- ACS worker -> ACS Chrome -> official ACS issue representations
  +-- AIP worker -> AIP Chrome -> official AIP issue representations
  +-- APS worker -> APS Chrome -> official APS issue representations
```

Profiles are stored below the runtime-only directory
`.papertrack_browser_profiles/{provider}`. They are sensitive cache state,
created with restrictive permissions and excluded from Git. A provider profile
has a single-owner lock so overlapping runs cannot open it concurrently.

The supervisor owns the Chrome process tree. Normal completion, worker failure,
SIGTERM, and SIGINT all close the browser before the worker exits. No Chrome
daemon remains resident after `run_j.sh` finishes.

## Official representation selection

Each provider adapter uses the following ordered strategy:

1. Reuse an already validated local snapshot for rebuild or parser migration.
2. Request the canonical official issue page in the persistent Chrome session.
3. Inspect same-origin responses loaded by that issue page for a denser official
   issue representation, such as JSON, an HTML fragment, or an abstract AJAX
   response.
4. Parse the best validated representation and save the raw official response.

Only HTTPS responses on the publisher's allowlisted official hosts are eligible.
The browser may load normal page assets, but PaperTrack must not navigate to
individual article pages. Provider adapters define explicit selectors, endpoint
patterns, and response validators; an arbitrary same-origin response is never
accepted merely because it is JSON.

ACS and APS initially use their official issue DOM while response capture shows
whether a stable issue-level representation exists. AIP retains its official
issue-page abstract endpoint, but sends those requests inside the same persistent
browser context instead of creating a fresh context per issue.

## Snapshot cache

Successful official responses are saved under the runtime-only cache
`.papertrack_official_cache/{provider}/{journal}/{volume}/{issue}`. Each snapshot
has:

- canonical official URL and final URL;
- provider, journal, volume, and issue;
- response media type and raw response bytes;
- capture time and content hash;
- selector or endpoint validator results;
- parser version and extracted DOI membership hash.

The cache is not committed. Raw bodies are immutable, content-addressed blobs.
A commit fsyncs all new blobs first and then atomically replaces the small issue
manifest that points to them. The previous manifest and its blobs remain valid
until that final replacement. A failed fetch or failed validation therefore
cannot damage the last valid snapshot.

Re-rendering Markdown from a valid snapshot is offline and may be repeated as
often as needed. A parser upgrade does not require another publisher request.

## Request pacing and challenge recovery

Each provider retains a minimum 30-second interval between issue navigations.
There is no hours-long delayed retry.

When a challenge is detected:

1. keep the same Chrome process, context, profile, and page;
2. wait 60 seconds;
3. reload that issue once;
4. if the challenge remains, record the issue as blocked and continue according
   to the provider circuit breaker.

Three consecutive blocked issues open the provider circuit for the remainder of
that `run_j.sh` execution. The other publisher workers continue. A later normal
invocation starts a new bounded attempt using the preserved profile.

Rate limiting, timeout, malformed content, missing selectors, challenge, and
partial metadata remain distinct outcomes. None may be converted into an empty
successful issue.

## Report preservation and state

The durable Markdown report remains the reading ledger. A newly validated
snapshot may repair its generated sections while preserving completed tasks,
anchors, and user edits under the existing migration rules.

If no valid snapshot or live official response is available, the current valid
Markdown survives unchanged. Progress state records the precise outcome rather
than marking the issue processed. `fetch_failed`, `blocked`, and incomplete
official results remain retryable.

`run_j.sh` sets the journal source policy to official-only. CrossRef discovery,
membership, and metadata-fill paths are not called by these workers.

## Metrics and diagnostics

The run summary reports two rates per provider and overall:

```text
official_fetch_rate = validated official issues / attempted issues
complete_report_rate = complete rebuilt reports / attempted issues
```

It also reports counts for snapshot hits, live DOM successes, official endpoint
successes, blocked pages, circuit-breaker skips, fetch failures, and unresolved
metadata. A cache hit is identified separately from a live network success.

Every live failure records provider, journal, volume, issue, official URL,
browser/profile mode, response status when available, challenge indicators,
failed selector or endpoint validator, attempt number, and circuit state.
Cookies and sensitive profile data never enter diagnostics.

## Verification

Tests use mocked browser pages and responses; the normal suite requires neither
network access nor an installed GUI session. Coverage includes:

- official-host and issue-URL allowlists;
- rejection of article-page navigation and unrecognized responses;
- one Chrome session reused across multiple issues;
- profile ownership and cleanup on every exit path;
- 30-second pacing, one 60-second recovery, and three-block circuit opening;
- atomic snapshot replacement and corrupt-cache rejection;
- offline rebuild from a validated snapshot;
- preservation of valid Markdown on every network failure;
- CrossRef never called by `run_j.sh` journal workers;
- accurate fetch-rate and complete-report-rate summaries.

A separate opt-in live verifier may test one issue per publisher and write only
diagnostics and cache candidates. It never forms part of the deterministic test
suite.

## Non-goals

- CAPTCHA solving, stealth plugins, fingerprint spoofing, or rotating proxies;
- individual article-page crawling;
- CrossRef or another third party as journal metadata fallback;
- a permanently resident browser daemon;
- treating a challenge or partial page as successful data.

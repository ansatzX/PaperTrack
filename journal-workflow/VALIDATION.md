# Acceptance — 2026-09-11

## Verified

- Real Chrome JCIM 66/12 capture through the new file bridge and Crawlee worker,
  no model enabled: 43 official cards, 41 reading entries, 41 populated abstract
  fields (39 Abstract + 2 Extract). Automatically rendered report in
  `/tmp/papertrack-workflow-output/acs/jcim/66/12.md`.
- At 17 saved articles, deliberately closed the task-owned browser tab. Two
  conventional attempts failed, worker exited `needs_attention`, checkpoint
  retained 17 articles/24 missing, and no report was published. Recreated the
  owned tab/driver and restarted the same job: finished the remaining 24. Existing
  text is compared and retained during every resume.
- The first queue integration used an ineffective storageDir option, noticed in
  concurrent tests. Corrected to `storageClientOptions.localDataDirectory` and
  added an assertion for the actual job-specific request-queue directory. The
  corrected 13-test JS suite finishes in about 2 seconds. Initial test storage was
  moved out of the repo to `/tmp/papertrack-first-run-crawlee-storage`.
- Real DeepSeek (`deepseek-flash`) calls via the official OpenAI JS SDK succeeded.
  In a separately labeled synthetic closed-tab scenario, agent called inspect
  then requested human reconnection. This verifies live SDK + decision/tool loop,
  not a claim that the model repaired a real publisher layout.
- OpenAI `gpt-5.6-luna` tool-call and opt-in image request shapes contract-tested;
  no live OpenAI call, because OPENAI_API_KEY is not configured in this runtime.
- 13 isolated Node tests: interruption/resume, monotonic contents, bad identities,
  actual Crawlee retries/storage, one-writer lock, no model on healthy execution,
  completion reuse, bounded recovery, malformed model output, missing credentials,
  response correlation, expiry and error redaction.
- 2 report integration tests: checked-state retention + backups, invalid capture
  and lost abstract cannot overwrite a report.
- Full Python suite: 579 passed (577 existing + 2 new isolated renderer tests).
- 68 preexisting Python source/test/config files were hashed before and after:
  zero changes, including pyproject.toml. This is in addition to full regression,
  not a replacement for it. No arXiv code, CLI, default dependencies, cron scripts,
  production reports or processed-issue state were changed by this work.

## Boundaries

This version is an optional prototype module with a real worker and live browser
acceptance. The Chrome host must pump the supported tool connection; it is not an
unattended browser service. It accepts explicit issues and does not implement
natural-language discovery, arbitrary model-driven actions, code repair, or
automatic screenshot escalation. Host-side reconnection remains external.

The new renderer is offline/official-capture-only; existing PaperTrack CLI retains
its third-party enrichment. Existing report filtering is reused; this work does
not broaden journal membership rules across all publishers. Only JCIM was newly
captured through this worker; previous ACS/AIP/APS collectors keep their earlier
acceptance evidence. All output is local review material, not a deployment.

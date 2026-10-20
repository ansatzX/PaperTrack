# Experiments

`journal-workflow/` is the older, optional queue/worker prototype. It has its own
Node dependencies and tests and is outside the daily `run.sh` pipeline.

Production journal orchestration lives in `src/papertrack/journals/`; browser
collection has one implementation in `src/papertrack/browser/`. Do not add new
production paths to the prototype.

From the repository root, run its tests with:

```bash
npm --prefix experiments/journal-workflow test
```

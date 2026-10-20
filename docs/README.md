# Documentation map

- [README](../README.md): current workflow and commands.
- [INSTALL](../INSTALL.md): installation and deployment.
- [AGENTS](../AGENTS.md): source, state, resource and browser contracts.
- [Browser collector](../browser/README.md): fixed collector and compatibility entrypoints.
- [Legacy material](legacy/README.md): preserved research and experiments.
- [Maintenance scripts](../scripts/README.md): supported local audit utilities.

`superpowers/` contains historical design plans. Use root documentation and current
source for supported behavior. Runtime evidence belongs in the data directory's
`.papertrack-runs/`, not in docs.

## Directory boundaries

| Directory | Purpose |
| --- | --- |
| `src/papertrack/` | Python library, report templates and the single browser collector implementation |
| `tests/` | Python regression tests and fixtures |
| `browser/` | Compatibility exports and JavaScript tests; do not copy collector implementations here |
| `scripts/` | Explicitly invoked maintenance utilities |
| `experiments/journal-workflow/` | Optional older queue prototype, outside the daily refresh path |
| `docs/legacy/` | Archived notes, notebook and historical script |
| `docs/superpowers/` | Historical plans and design records, retained at their existing paths |
| `.state-machine/` | Local verification records and migration baselines |

Keep runtime evidence separate: the configured data directory owns reports and
`.papertrack-runs/`; `.papertrack_diagnostics/` holds local diagnostics.
Preserve `.papertrack_state.json` for historical lookup. These are durable state,
not disposable build caches. Only Python bytecode and pytest caches are routinely
safe to regenerate. Environment directories and worktrees are not cleanup targets.

## Production code map

- `cli.py`, `journals/sync.py`: commands and independent daily pipelines.
- `journals/discovery.py`, `journals/inventory.py`: official archive inventory.
- `journals/agent.py`, `journals/task.py`, `browser/`: bounded access and capture.
- `journals/capture.py`, `journals/recent.py`, `reports/reading.py`: local evidence validation and parsing.
- `reports/render.py`, `reports/templates/`, `reports/markdown.py`: Markdown generation.
- `reports/migration.py`, `reports/presentation.py`, `reports/layout.py`: durable report migrations.
- `journals/cache.py`, `journals/cache_repair.py`: local cache reuse and repair.

`journals/metadata.py` only normalizes capture DOIs and stamps provenance. Retired
Crossref conversion and multi-provider metadata union helpers are removed; formal
issue membership comes from validated official captures.

The CLI (`papertrack`, `python -m papertrack`, `main.py`, `run.sh`) is unchanged.
Python module locations now follow these directories; `config.py` replaces the
misleading `codex.py` name. Update internal scripts to import the new modules
directly. One-off scripts in `.work/` are historical records and may retain old
imports; inspect them before reusing them.

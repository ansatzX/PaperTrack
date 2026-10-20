# Claude Code repository guidance

Read [AGENTS.md](AGENTS.md) for the current design contract and repository rules.
Use [README.md](README.md) for commands and [INSTALL.md](INSTALL.md) for setup.
The [documentation map](docs/README.md) distinguishes current documentation from
historical plans and experiments.

Daily deployment runs the no-argument `./run.sh`. The library owns orchestration,
validation and Markdown rendering. Journal agents establish browser access and
invoke the packaged fixed collector; they must not invent parsers or write reports.

`journal refresh` reconciles dated formal issues since 2026-01-01, reusing valid
captures and fetching only cache gaps. Daily journal/arXiv pipelines run concurrently. `journal latest` and
`journal backfill` emit discovery tasks. `journal issue` launches an agent only
with `--run-agent`. Preserve reading state and all `.papertrack-runs` evidence.

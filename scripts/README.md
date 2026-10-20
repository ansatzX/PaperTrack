# Maintenance scripts

`audit_arxiv_category_membership.py` compares existing arXiv reading ledgers with
official date/category membership. It queries arXiv and is an explicit audit tool,
not the daily refresh entrypoint. Run it with the project's Python environment;
use `--help` to inspect its arguments.

Daily collection belongs to the repository-root `run.sh` and the package CLI.
Historical deployment commands live in `docs/legacy/`. Retired publisher fetch
and repair scripts are recoverable from Git checkpoint `8804413`; do not restore
them as alternate collection paths.

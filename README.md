# Zotero arXiv Daily

Zotero arXiv Daily is a local-first tool that builds a compact interest profile from a
Zotero library and uses it to produce a daily arXiv reading list. Raw Zotero records,
notes, annotations, and PDF content remain local.

The project has completed the local Zotero synchronization milestone of v0.1.0.
Implemented commands are `doctor` and `profile sync`; profile export and recommendation
commands will be added in later milestones of the active
[v0.1.0 plan](docs/plans/v0.1.0-initial-mvp.md).

## Requirements

- Python 3.12 or newer
- `uv` for reproducible development environments
- Zotero Desktop with its Local API enabled, when checking the local connection

## Development setup

```bash
uv sync --all-groups
uv run zotero-arxiv-daily doctor
```

To use a non-default configuration file, pass a TOML or JSON file explicitly:

```bash
uv run zotero-arxiv-daily --config ./zotero-arxiv-daily.toml doctor
```

Configuration values are resolved in this order: built-in defaults, configuration file,
environment variables, then command-line options. Credentials must be supplied through
environment variables or an external secret store; do not commit them to a configuration
file. See [`.env.example`](.env.example) for supported environment variable names.

`doctor` performs a short local Zotero probe by default and reports each missing or
unreachable dependency independently. It never prints secret values. Use
`--skip-zotero-check` when diagnosing configuration away from the desktop machine.

## Local Zotero synchronization

After `doctor` confirms that Zotero's Local API is reachable, run an initial local sync:

```bash
uv run zotero-arxiv-daily profile sync
```

The default database is `runtime/zotero.sqlite3`, which is ignored by Git. The command
fetches the complete library on its first run and uses the last successful library version
for later incremental runs. It prints only counts, never notes, annotations, or bibliographic
content. Override the storage location with `--database PATH` or `ZAD_LOCAL_DATABASE_PATH`.
Stop or retry an interrupted sync normally: the SQLite transaction retains the previous usable
state until the new batch is valid and complete.

Build a compact local remote-profile candidate after synchronization:

```bash
uv run zotero-arxiv-daily profile build
```

It writes `runtime/remote-profile.json` with owner-only permissions. The export contains only
bounded topic terms and inferred arXiv categories; it excludes titles, abstracts, notes,
annotations, identifiers, collections, and matching evidence. Unchanged local inputs reuse
derived digest cache entries rather than regenerating them.

To publish a validated exported profile to a GitHub Actions Secret, authenticate `gh` locally and
set `ZAD_GITHUB_REPOSITORY`; the profile JSON is sent on standard input rather than in command-line
arguments:

```bash
uv run zotero-arxiv-daily profile publish-github
```

## Current status and operation

The current release plan, operating assumptions, recovery design, and later command
surface are documented in [the v0.1.0 plan](docs/plans/v0.1.0-initial-mvp.md). No
recommendation workflow, external-model call, Pages deployment, or data publication is
implemented yet. Raw Zotero content stays in the ignored local SQLite database.

## Quality checks

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
uv build
python scripts/check_artifacts.py
```

The checks use only synthetic fixtures and run offline by default.

## License

This project is licensed under the [MIT License](LICENSE).

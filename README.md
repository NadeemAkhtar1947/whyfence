# whyfence

Temporal memory for coding agents: *why* the code is the way it is.

## `whyfence lint`: catch stale agent instructions

`CLAUDE.md` and `AGENTS.md` tell agents how to build and test your project. When a
script gets renamed or a make target is deleted, every agent session keeps running
a command that no longer exists. `whyfence lint` finds those commands.

```console
$ uvx --from . whyfence lint            # scan the current repo
$ whyfence lint docs/AGENTS.md -v       # one file, show verified commands too
$ whyfence lint --json                  # machine-readable, for agents and CI
$ whyfence lint --strict                # also fail on commands it can't verify
```

It reads fenced shell blocks and inline code, follows `cd dir &&` and directory
flags, and checks each command against what it would actually run:

| Command | Checked against |
|---|---|
| `make test`, `make -C api migrate` | Makefile targets, including `include`d files |
| `npm run dev`, `yarn lint`, `pnpm test`, `bun run build` | `package.json` scripts, package binaries |
| `uv run serve`, `poetry run serve` | `[project.scripts]`, declared dependencies, `.venv` |
| `python -m app.jobs`, `uv run -m app` | local modules (and `__main__.py`), stdlib, dependencies |
| `python scripts/seed.py`, `./scripts/release.sh` | the file on disk |

When a command is stale it suggests the closest match, including targets and
files that moved into a subdirectory. Anything that depends on runtime state
(computed names, workspace filters, globally installed tools) is reported as
unverifiable instead of guessed at.

Exit code is `1` when a command is stale, so it works as a CI gate.

Try it on the bundled example: `uv run whyfence lint examples/stale-repo`.

## `whyfence ingest`: index git history

```console
$ whyfence ingest                       # index this repo into .whyfence/index.db
```

The index is a local SQLite database (WAL mode, FTS5 search over events). It is
rebuildable from history, so it is gitignored and safe to delete.

## Development

```console
$ uv sync
$ uv run pytest
```

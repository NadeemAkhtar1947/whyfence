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

It checks `make`, `npm`, `yarn`, `pnpm` and `bun run` commands from fenced shell
blocks and inline code against the Makefile or `package.json` they would run
against. It follows `cd dir &&`, `make -C`, `npm --prefix`, Makefile `include`s,
yarn/pnpm built-ins and package binaries, and it suggests the closest match,
including targets that moved into a subdirectory.

Exit code is `1` when a command is stale, so it works as a CI gate.

Try it on the bundled example: `uv run whyfence lint examples/stale-repo`.

## Development

```console
$ uv sync
$ uv run pytest
```

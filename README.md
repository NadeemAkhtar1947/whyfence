# whyfence

[![CI](https://github.com/NadeemAkhtar1947/whyfence/actions/workflows/ci.yml/badge.svg)](https://github.com/NadeemAkhtar1947/whyfence/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/NadeemAkhtar1947/whyfence/blob/main/LICENSE)
![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)

**Your coding agent knows *what* your code does. whyfence tells it *why*.**

whyfence gives AI coding agents a memory of how your code got the way it is: the
decisions behind it, the approaches that already failed, the corrections you made,
and the rules you set. Your agent can check that memory over MCP before it
"simplifies" something that was deliberately written that way.

Local-first. No server, no account, no code leaves your machine.

---

## Chesterton's Fence, for AI agents

> There is a fence across a road. A reformer says, "I don't see the use of this; let
> us clear it away." The wiser one replies: "If you don't see the use of it, I
> certainly won't let you clear it away. Go away and think. When you can come back
> and tell me that you *do* see the use of it, I may allow you to destroy it."
> (paraphrasing G. K. Chesterton)

Coding agents meet fences all day: a retry loop that looks redundant, a lock that
looks unnecessary, a model called `TaskFile` instead of the obvious `File`. They
can read the code, but they can't see why it is there. That knowledge lives in old
commit messages, in your head, and in last week's agent session, which the new
session has forgotten.

So agents tear fences down. They re-add the retry you removed on purpose, retry the
approach that deadlocked on Tuesday, or rename `TaskFile` back to `File` and break
every import of `fastapi.File`.

whyfence collects that "why" from places you already have it and pins it to the
function it is about:

```text
why("app.models.TaskFile")

  decision · 2026-10-31 · from a Claude Code session
  "The file model is `TaskFile`, not `File`, because `File` would clash with
   `fastapi.File`, which the documents and invoice routers already import."
```

---

## What it remembers

| Kind | Where it comes from | Example |
|---|---|---|
| `decision` | Commit messages (`Why:`, "because", "so that"); the agent's own stated choices in sessions | "`Decimal` instead of `float` to avoid rounding errors" |
| `constraint` | Rules you state in a session or a commit | "Never call `send()` from the event loop" |
| `correction` | You pushing back on the agent | "No, don't retry inside `refresh()`; the HTTP client already retries" |
| `dead_end` | The agent realizing an approach was wrong | "Turns out `celery.current_app` is a lazy proxy, so patching it doesn't stick" |
| `incident` | `fix:` commits, "Fixes #12", bugs, crashes | "fix: refresh tokens before they expire" |
| `revert` | Commit subjects like `Revert "…"` | |

Each event is linked to the **functions, methods and classes** it is about, and
those links follow the code through history. whyfence indexes every Python,
JavaScript and TypeScript symbol at every commit and keeps its identity when it
is renamed, moved to another file, or renamed and edited in the same commit.

---

## Try it in one minute

```console
$ git clone https://github.com/NadeemAkhtar1947/whyfence && cd whyfence
$ ./demo.sh
```

[`demo.sh`](https://github.com/NadeemAkhtar1947/whyfence/blob/main/demo.sh) builds a throwaway repository and a mock Claude Code session in
a temp folder, then runs `whyfence lint`, `whyfence ingest`, `whyfence why` and
`whyfence history` on them. It needs only bash, git and
[uv](https://docs.astral.sh/uv/), and it never touches your own repositories or
`~/.claude`. The `why` step prints what an agent learns before touching `refresh()`:

```text
app.tokens.refresh  function · python
constraint  2026-02-10  From now on, never call `refresh` from the request
                        path; the background renewer owns it.
                        transcript · claude-code · inferred
dead_end    2026-02-10  I see the issue now: retrying here stacked on top of
                        the client's retries. I'll revert the loop.
                        transcript · claude-code · inferred
correction  2026-02-10  No, don't retry inside refresh(). The HTTP client
                        already retries, so this doubles every request.
                        transcript · claude-code · inferred
incident    2026-02-03  fix: refresh tokens before they expire
                        commit c69facf
```

---

## Quick start

whyfence is alpha and needs Python 3.12 or newer.

```console
$ pip install whyfence
$ whyfence ingest /path/to/your/repo
```

On the small repository `demo.sh` builds, that prints:

```text
Read 3 commits from demo-app: 3 new, 3 indexed in demo-app/.whyfence/index.db
Walked 3 commits: 2 new symbols, 5 versions, 2 renamed or moved, 0 deleted
· 2 symbols indexed
Found 1 event in commit messages and 3 events in 1 Claude Code session,
with 4 symbol links
```

Then connect your agent. For Claude Code, add `.mcp.json` to your repository
(this uses [uvx](https://docs.astral.sh/uv/); with a plain `pip install`, use
`"command": "whyfence", "args": ["mcp"]` instead):

```json
{
  "mcpServers": {
    "whyfence": {
      "type": "stdio",
      "command": "uvx",
      "args": ["whyfence", "mcp"]
    }
  }
}
```

Claude Desktop and other clients: see [README_MCP.md](https://github.com/NadeemAkhtar1947/whyfence/blob/main/README_MCP.md).

Re-run `whyfence ingest` whenever you like. It only processes new commits and
sessions, and it rebuilds symbol history automatically if history was rewritten.

---

## Temporal memory over MCP

`whyfence mcp` serves two read-only tools:

**`why(symbol_name)`**: everything recorded about why a symbol is the way it is,
newest first, each with its source (commit or session) and how confident the
extraction is.

**`history(symbol_name)`**: the symbol's timeline: each version, the commit that
introduced it, the commit that replaced it, and its name, file and lines at the time.

```json
{
  "symbol": {"name": "app.tokens.refresh_token", "kind": "function"},
  "versions": [
    {"name": "app.auth.refresh", "file": "app/auth.py",
     "valid_from": {"sha": "3f1c…", "subject": "add auth"}},
    {"name": "app.session.refresh", "file": "app/session.py",
     "valid_from": {"sha": "9ab2…", "subject": "rename the file"}},
    {"name": "app.tokens.refresh", "file": "app/tokens.py",
     "valid_from": {"sha": "c47e…", "subject": "move refresh to its own module"}},
    {"name": "app.tokens.refresh_token", "file": "app/tokens.py",
     "valid_from": {"sha": "e810…", "subject": "rename and edit at once"},
     "current": true}
  ]
}
```

Names can be qualified (`app.auth.refresh`), bare (`refresh`) or a symbol id. An
ambiguous bare name returns the candidates instead of guessing.

The same answers are available from the command line, for you or for scripts:

```console
$ whyfence why refresh              # readable summary
$ whyfence history app.auth.refresh
$ whyfence why refresh --json       # exactly what the MCP tool returns
```

The server tells agents to call `why` before changing code that looks wrong or
redundant, and to treat recorded constraints and dead ends as binding unless they
have new information.

---

## `whyfence lint`: catch stale agent instructions

Agents follow `CLAUDE.md` and `AGENTS.md` literally. When a script is renamed or a
make target is deleted, every session keeps running a command that no longer
exists. `whyfence lint` checks every command in those files against what it would
actually run:

```console
$ whyfence lint examples/stale-repo

examples/stale-repo/CLAUDE.md  7 commands · 5 stale
✗   L3  npm run lint       no `lint` script in package.json
                           → did you mean npm run lint:all?
✗   L7  npm run start      no `start` script in web/package.json
✗   L8  make -C api tests  no `tests` target in api/Makefile
                           → did you mean make -C api test?
✗   L9  make migrate       no Makefile in ./
                           → did you mean make -C api migrate?
✗  L12  make deploy        no Makefile in ./

┌──────────────────────────────────────────────────────────────────────────┐
│ ✗ 5 stale   ? 0 unverifiable   ✓ 2 verified                              │
│ 7 commands across 1 file. Agents reading these files will run commands   │
│ that no longer exist.                                                    │
└──────────────────────────────────────────────────────────────────────────┘
```

| Command | Checked against |
|---|---|
| `make test`, `make -C api migrate` | Makefile targets, including `include`d files |
| `npm run dev`, `yarn lint`, `pnpm test`, `bun run build` | `package.json` scripts and package binaries |
| `uv run serve`, `poetry run serve` | `[project.scripts]`, declared dependencies, `.venv` |
| `python -m app.jobs`, `uv run -m app` | local modules (with `__main__.py`), stdlib, dependencies |
| `python scripts/seed.py`, `./scripts/release.sh` | the file on disk |

It follows `cd dir &&` and directory flags, suggests the closest match (including
targets that moved into a subdirectory), and reports anything that depends on
runtime state as *unverifiable* instead of guessing. It exits with `1` on stale
commands, so it works as a CI gate:

```console
$ whyfence lint --json          # machine-readable
$ whyfence lint --strict        # also fail on unverifiable commands
$ whyfence lint --exclude docs  # skip paths (examples/ is skipped by default)
```

---

## How it works

```text
git history ──► commits ──► symbol index (tree-sitter, every commit, first-parent)
                                │   identity: name → body fingerprint → token similarity
                                ▼
commit messages ─┐         symbol_versions ◄── history()
Claude Code      ├─► events ──► event_links ◄── why()
transcripts ─────┘
                       SQLite (WAL, FTS5) in .whyfence/index.db
```

- **Symbol identity.** Each definition is fingerprinted with its own name removed
  and whitespace collapsed. A commit's definitions are matched to existing symbols
  by name in the same file, then by fingerprint anywhere in the commit (renames and
  moves), then by token similarity (renamed *and* edited). Only code, file or name
  changes create a new version; line shifts don't.
- **Events.** Commit events link to the symbols that commit changed. Session events
  link through the edits the agent made around them, located in today's code and
  re-parsed, plus symbols named in the text. Broad events (a feature commit
  touching 80 functions) keep only the symbols they name.
- **Precision over recall.** Extraction is deterministic pattern matching, tuned on
  real sessions. It skips tool output, subagent chatter, the agent's private
  reasoning and Claude Code's own compaction summaries. A missing event costs
  you nothing; a wrong one teaches your agent something false.

## Privacy

Everything stays in `.whyfence/index.db` inside your repository, which you can
gitignore and delete at any time; it is rebuilt from history. Transcripts are read
from `~/.claude/projects` for sessions in, under or above the repository
(`--no-transcripts` to skip them). Obvious secrets (API keys, tokens) are redacted
before anything is stored. whyfence makes no network calls.

## Status and roadmap

whyfence is an early MVP. It works end to end, but its extraction heuristics have
only been tuned on a handful of real projects.

- [x] Stale-context linter for `CLAUDE.md` / `AGENTS.md`
- [x] Symbol history with rename and move tracking (Python, JavaScript, TypeScript)
- [x] Events from commit messages and Claude Code transcripts
- [x] MCP server: `why`, `history`
- [ ] Team memory: a shared, append-only event log committed to the repo
- [ ] More agents' transcripts (Codex, Cursor, Gemini CLI)
- [ ] More languages (Go, Rust, Java)
- [ ] A `rules` tool that serves every standing constraint at session start

## Development

```console
$ git clone https://github.com/NadeemAkhtar1947/whyfence && cd whyfence
$ uv sync
$ uv run pytest
```

The test suite includes an end-to-end check that indexes whyfence's own git
history and verifies every recorded version against Python's `ast`.

## License

[MIT](https://github.com/NadeemAkhtar1947/whyfence/blob/main/LICENSE)

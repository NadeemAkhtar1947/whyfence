# Connecting whyfence to Claude

`whyfence mcp` runs an MCP server over stdio with three read-only tools:

| Tool | Returns |
|---|---|
| `rules()` | Every standing rule recorded for the repository, newest first. Agents are told to load these at session start |
| `why(symbol_name)` | Decisions, dead ends, corrections, constraints, reverts and incidents recorded against a function or class, newest first |
| `history(symbol_name)` | Each recorded version of the symbol, with the commit that introduced it (`valid_from`) and the commit that replaced it (`valid_to`) |

`symbol_name` can be a qualified name (`app.auth.refresh`), a bare name (`refresh`) or a
symbol id. If a bare name matches several symbols, the tool returns the candidates.

> **What feeds the tools:** `whyfence ingest` indexes every Python, JavaScript,
> TypeScript, Go, Rust and Java function and class across git history (`history`), and mines commit
> messages and agent session logs for reverts, fixes, decisions, corrections and
> dead ends (`why`). Re-run it to pick up new commits and sessions.

## 1. Build the index

In the repository you want Claude to ask about:

```console
$ pip install whyfence
$ whyfence ingest
```

This writes `.whyfence/index.db`. Add `.whyfence/*.db*` to that repository's `.gitignore`, so the index stays local but
shared events in `.whyfence/events/` can be committed.

## 2a. Claude Code

Create `.mcp.json` in the root of your repository:

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

Or add the same server from the command line:

```console
$ claude mcp add whyfence --scope project -- uvx whyfence mcp
```

Claude Code starts the server in your repository, so it serves `./.whyfence/index.db`.
Run `/mcp` inside Claude Code to check that `whyfence` is connected.

## 2b. Claude Desktop

Claude Desktop has no project directory, so pass the repository explicitly with `--repo`.
Open the config file (Settings → Developer → Edit Config):

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

Add `whyfence` under `mcpServers`:

```json
{
  "mcpServers": {
    "whyfence": {
      "command": "uvx",
      "args": ["whyfence", "mcp", "--repo", "/path/to/your-repo"]
    }
  }
}
```

Restart Claude Desktop afterwards.

## Paths and troubleshooting

- **`/path/to/your-repo`** is the repository you indexed.
- **Windows paths** in JSON need forward slashes (`C:/Users/me/code/my-repo`) or doubled
  backslashes (`C:\\Users\\me\\code\\my-repo`).
- **Without uv**: the configs use `uvx`, which runs whyfence from PyPI. If you installed
  with `pip`, use `"command": "whyfence"` and drop `"whyfence"` from the start of `args`.
- **`uvx` or `whyfence` not found**: apps launched from the GUI may not see your shell's
  `PATH`. Use the full path to the executable as `command`. Find it with `which uvx`
  (macOS/Linux) or `where uvx` (Windows).
- **`No whyfence index at …`**: the server found no database. Run step 1, or point the
  server at one with `--db /path/to/index.db`.
- The server only reads the index, so it is safe to leave connected while
  `whyfence ingest` runs.

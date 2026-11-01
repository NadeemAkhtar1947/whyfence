#!/usr/bin/env bash
# See whyfence work in under a minute.
#
#   1. `whyfence lint` catches stale commands in a CLAUDE.md.
#   2. `whyfence ingest` indexes a tiny git repository and a Claude Code session.
#   3. `whyfence why` / `whyfence history` explain a function the way an agent sees them over MCP.
#
# Everything happens in a temporary folder that is deleted afterwards. Your own
# repositories, git config and ~/.claude are never read or touched.
#
# Usage: ./demo.sh            (set WHYFENCE="..." to choose how whyfence is run)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# How to run whyfence: $WHYFENCE, an installed `whyfence`, or this checkout via uv.
if [ -n "${WHYFENCE:-}" ]; then
  read -r -a WF <<<"$WHYFENCE"
elif command -v whyfence >/dev/null 2>&1; then
  WF=(whyfence)
elif command -v uv >/dev/null 2>&1; then
  WF=(uv run --quiet --project "$HERE" whyfence)
elif python3 -m uv --version >/dev/null 2>&1; then
  WF=(python3 -m uv run --quiet --project "$HERE" whyfence)
elif python -m uv --version >/dev/null 2>&1; then
  WF=(python -m uv run --quiet --project "$HERE" whyfence)
else
  echo "demo.sh needs whyfence (pip install whyfence) or uv (https://docs.astral.sh/uv/)." >&2
  exit 1
fi

# Paths as Python and git see them: C:/... on Windows (Git Bash), symlinks resolved elsewhere.
native() { if command -v cygpath >/dev/null 2>&1; then cygpath -m "$1"; else printf '%s\n' "$1"; fi; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
WORK="$(cd "$WORK" && pwd -P)"

# Keep the demo's commits independent of your git config (signing, hooks, templates).
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
export GIT_AUTHOR_NAME="whyfence demo" GIT_AUTHOR_EMAIL="demo@example.com"
export GIT_COMMITTER_NAME="whyfence demo" GIT_COMMITTER_EMAIL="demo@example.com"

step() { printf '\n\033[1;36m━━ %s\033[0m\n\033[2m$ %s\033[0m\n' "$1" "$2"; }

# --------------------------------------------------------------------------- #
# 1. A CLAUDE.md that has drifted from the project
# --------------------------------------------------------------------------- #
STALE="$WORK/stale-repo"
mkdir -p "$STALE/api"
cat >"$STALE/package.json" <<'EOF'
{ "name": "stale-repo", "scripts": { "test:unit": "vitest", "lint:all": "eslint .", "build": "tsc -b" } }
EOF
printf 'test:\n\tpytest -q\nmigrate:\n\talembic upgrade head\n' >"$STALE/api/Makefile"
cat >"$STALE/CLAUDE.md" <<'EOF'
# Working on stale-repo

Always run `npm run lint` before committing, then `npm run test:unit`.

```bash
npm run build
make -C api tests
make migrate
```
EOF

step "1. whyfence lint: commands in CLAUDE.md that no longer exist" "whyfence lint stale-repo"
"${WF[@]}" lint "$(native "$STALE")" || true   # exits 1 because it found stale commands

# --------------------------------------------------------------------------- #
# 2. A repository with a little history, and a Claude Code session about it
# --------------------------------------------------------------------------- #
REPO="$WORK/demo-app"
mkdir -p "$REPO/app"
git -C "$REPO" init -q -b main

cat >"$REPO/app/auth.py" <<'EOF'
def refresh(token, *, leeway=60):
    """Renew a token shortly before it expires."""
    if token.expires_in > leeway:
        return token
    return client.renew(token.refresh_token)


def logout(session):
    session.clear()
EOF
at() { export GIT_AUTHOR_DATE="$1" GIT_COMMITTER_DATE="$1"; }
at 2026-02-01T10:00:00Z
git -C "$REPO" add -A && git -C "$REPO" commit -q -m "Add token refresh"

at 2026-02-03T10:00:00Z
sed -i.bak 's/leeway=60/leeway=120/' "$REPO/app/auth.py" && rm "$REPO/app/auth.py.bak"
git -C "$REPO" commit -q -am "fix: refresh tokens before they expire

Why: refreshing only after a 401 caused bursts of failed requests."

at 2026-02-05T10:00:00Z
git -C "$REPO" mv app/auth.py app/tokens.py
git -C "$REPO" commit -q -m "Move token code into app/tokens.py"

# A Claude Code transcript, laid out the way Claude Code stores them:
# <projects>/<project path with every non-alphanumeric character as "-">/<session>.jsonl
REPO_NATIVE="$(native "$REPO")"
PROJECTS="$WORK/claude-projects"
SESSION_DIR="$PROJECTS/$(printf '%s' "$REPO_NATIVE" | sed 's/[^A-Za-z0-9]/-/g')"
mkdir -p "$SESSION_DIR"
sed "s|__REPO__|$REPO_NATIVE|g" >"$SESSION_DIR/demo-session.jsonl" <<'EOF'
{"type":"user","uuid":"u1","cwd":"__REPO__","timestamp":"2026-02-10T10:00:00Z","message":{"role":"user","content":"Make token refresh more robust."}}
{"type":"assistant","uuid":"a1","cwd":"__REPO__","timestamp":"2026-02-10T10:01:00Z","message":{"role":"assistant","content":[{"type":"text","text":"I'll add a retry loop inside `refresh()`."},{"type":"tool_use","id":"t1","name":"Edit","input":{"file_path":"__REPO__/app/tokens.py","old_string":"x","new_string":"    for attempt in range(3):"}}]}}
{"type":"user","uuid":"u2","cwd":"__REPO__","timestamp":"2026-02-10T10:02:00Z","message":{"role":"user","content":"No, don't retry inside refresh(). The HTTP client already retries, so this doubles every request."}}
{"type":"assistant","uuid":"a2","cwd":"__REPO__","timestamp":"2026-02-10T10:03:00Z","message":{"role":"assistant","content":[{"type":"text","text":"I see the issue now: retrying here stacked on top of the client's retries. I'll revert the loop."},{"type":"tool_use","id":"t2","name":"Edit","input":{"file_path":"__REPO__/app/tokens.py","old_string":"x","new_string":"    if token.expires_in > leeway:"}}]}}
{"type":"user","uuid":"u3","cwd":"__REPO__","timestamp":"2026-02-10T10:04:00Z","message":{"role":"user","content":"Thanks. From now on, never call `refresh` from the request path; the background renewer owns it."}}
EOF

step "2. whyfence ingest: index history, symbols and the session" "whyfence ingest demo-app --transcripts claude-projects"
"${WF[@]}" ingest "$REPO_NATIVE" --transcripts "$(native "$PROJECTS")"

# --------------------------------------------------------------------------- #
# 3. Ask why, as an agent would over MCP
# --------------------------------------------------------------------------- #
step "3. whyfence why: what an agent learns before touching refresh()" "whyfence why refresh"
"${WF[@]}" why refresh --repo "$REPO_NATIVE"

step "4. whyfence history: how refresh() got here (through a file move)" "whyfence history refresh"
"${WF[@]}" history refresh --repo "$REPO_NATIVE"

printf '\n\033[2mAgents get the same answers from the MCP server: whyfence mcp (see README_MCP.md).\033[0m\n'

"""MCP server: lets coding agents ask why code is the way it is before changing it.

Runs over stdio, so stdout belongs to the protocol: nothing here may print.
Each tool call opens its own short-lived connection, which keeps the server
on the latest data while `whyfence ingest` writes (WAL allows both).
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from whyfence import store
from whyfence.store import Commit, Symbol

INSTRUCTIONS = """\
whyfence remembers why code is the way it is. Before changing, deleting or
"simplifying" a function or class that looks odd, call `why` to see the
decisions, dead ends and corrections recorded against it, and `history` to see
how it evolved. Respect recorded constraints, and don't retry a known dead end
without new information."""

DETAIL_LIMIT = 500  # characters of an event body to include


def _commit(commit: Commit | None, sha: str | None) -> dict[str, Any] | None:
    if sha is None:
        return None
    if commit is None:
        return {"sha": sha, "ingested": False}
    return {
        "sha": commit.sha,
        "date": store.to_timestamp(commit.authored_at),
        "author": commit.author,
        "subject": commit.subject,
    }


def _symbol(symbol: Symbol) -> dict[str, Any]:
    return {"id": symbol.id, "name": symbol.qualified_name, "kind": symbol.kind, "language": symbol.language}


def _resolve(conn: sqlite3.Connection, name: str) -> Symbol | dict[str, Any]:
    """The one symbol `name` refers to, or a not-found / ambiguous answer for the agent."""
    matches = store.find_symbols(conn, name)
    if len(matches) == 1:
        return matches[0]
    if not matches:
        message = f"No indexed symbol matches {name!r}."
        if store.count_symbols(conn) == 0:
            message += " The index has no symbols yet, so there is no recorded history."
        return {"status": "not_found", "query": name, "message": message}
    return {
        "status": "ambiguous",
        "query": name,
        "message": f"{name!r} matches {len(matches)} symbols; call again with a qualified name.",
        "candidates": [s.qualified_name for s in matches],
    }


def lookup_history(conn: sqlite3.Connection, symbol_name: str) -> dict[str, Any]:
    symbol = _resolve(conn, symbol_name)
    if isinstance(symbol, dict):
        return symbol
    versions = []
    for entry in store.symbol_timeline(conn, symbol.id):
        version = entry.version
        versions.append({
            "file": version.file_path,
            "lines": [version.start_line, version.end_line],
            "body_hash": version.body_hash,
            "valid_from": _commit(entry.introduced_by, version.valid_from_sha),
            "valid_to": _commit(entry.replaced_by, version.valid_to_sha),
            "current": version.valid_to_sha is None,
        })
    return {"status": "ok", "symbol": _symbol(symbol), "versions": versions}


def lookup_why(conn: sqlite3.Connection, symbol_name: str) -> dict[str, Any]:
    symbol = _resolve(conn, symbol_name)
    if isinstance(symbol, dict):
        return symbol
    events = []
    for event in store.events_for_symbol(conn, symbol.id):
        source = store.get_source(conn, event.source_id) if event.source_id else None
        detail = event.body if len(event.body) <= DETAIL_LIMIT else event.body[:DETAIL_LIMIT] + "…"
        events.append({
            "kind": event.kind,
            "summary": event.summary,
            "detail": detail or None,
            "confidence": event.confidence,
            "recorded_at": store.to_timestamp(event.recorded_at),
            "commit": event.commit_sha,
            "source": {"kind": source.kind, "agent": source.agent, "uri": source.uri} if source else None,
        })
    result: dict[str, Any] = {"status": "ok", "symbol": _symbol(symbol), "events": events}
    if not events:
        result["message"] = "Nothing has been recorded about why this symbol is the way it is."
    return result


def create_server(db_path: Path) -> MCPServer:
    server = MCPServer("whyfence", instructions=INSTRUCTIONS)

    def query(lookup, symbol_name: str) -> dict[str, Any]:
        if not db_path.is_file():
            raise ToolError(f"No whyfence index at {db_path}. Run `whyfence ingest` in the repository first.")
        with closing(store.connect(db_path)) as conn:
            return lookup(conn, symbol_name)

    read_only = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False)

    @server.tool(annotations=read_only)
    def history(symbol_name: str) -> dict[str, Any]:
        """Timeline of a function or class: each recorded version, with the commit that
        introduced it (valid_from) and the commit that replaced it (valid_to).

        `symbol_name` may be a qualified name (`app.auth.refresh`), a bare name
        (`refresh`) or a symbol id. An ambiguous bare name returns the candidates.
        """
        return query(lookup_history, symbol_name)

    @server.tool(annotations=read_only)
    def why(symbol_name: str) -> dict[str, Any]:
        """Why a function or class is the way it is: decisions, dead ends, corrections,
        constraints, reverts and incidents recorded against it, newest first.

        Check this before changing code that looks wrong or redundant.
        `symbol_name` accepts the same forms as `history`.
        """
        return query(lookup_why, symbol_name)

    return server


def serve(db_path: Path) -> None:
    """Run the server over stdio until the client disconnects."""
    create_server(db_path).run("stdio")

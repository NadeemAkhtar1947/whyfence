"""MCP server: lets coding agents ask why code is the way it is before changing it.

Runs over stdio, so stdout belongs to the protocol: nothing here may print.
Each tool call opens its own short-lived connection, which keeps the server
on the latest data while `whyfence ingest` writes (WAL allows both).
"""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from whyfence import store
from whyfence.queries import DETAIL_LIMIT, lookup_history, lookup_why  # noqa: F401  (re-exported)

INSTRUCTIONS = """\
whyfence remembers why code is the way it is. Before changing, deleting or
"simplifying" a function or class that looks odd, call `why` to see the
decisions, dead ends and corrections recorded against it, and `history` to see
how it evolved. Respect recorded constraints, and don't retry a known dead end
without new information."""


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

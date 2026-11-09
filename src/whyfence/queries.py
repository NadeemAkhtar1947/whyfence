"""Answers to "why is this symbol the way it is?" and "how did it get here?".

Shared by the MCP server and the `whyfence why` / `whyfence history` commands,
so both give the same answer. Results are plain JSON-ready dicts.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from whyfence import store
from whyfence.store import Commit, Symbol

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
            "name": version.qualified_name or symbol.qualified_name,
            "file": version.file_path,
            "lines": [version.start_line, version.end_line],
            "body_hash": version.body_hash,
            "valid_from": _commit(entry.introduced_by, version.valid_from_sha),
            "valid_to": _commit(entry.replaced_by, version.valid_to_sha),
            "current": version.valid_to_sha is None,
        })
    return {"status": "ok", "symbol": _symbol(symbol), "versions": versions}


def lookup_rules(conn: sqlite3.Connection) -> dict[str, Any]:
    """Every standing rule recorded for this repository, newest first, without repeats."""
    rules: list[dict[str, Any]] = []
    seen: set[str] = set()
    for event in store.events_between(conn, kinds=["constraint"]):
        key = " ".join(event.summary.lower().split())
        if key in seen:
            continue
        seen.add(key)
        source = store.get_source(conn, event.source_id) if event.source_id else None
        rules.append({
            "rule": event.summary,
            "about": store.symbols_linked_to(conn, event.id),
            "confidence": event.confidence,
            "recorded_at": store.to_timestamp(event.recorded_at),
            "source": {"kind": source.kind, "agent": source.agent} if source else None,
        })
    result: dict[str, Any] = {"status": "ok", "rules": rules}
    if not rules:
        result["message"] = "No standing rules have been recorded for this repository."
    return result


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

"""Answers to "why is this symbol the way it is?" and "how did it get here?".

Shared by the MCP server and the `whyfence why` / `whyfence history` commands,
so both give the same answer. Results are plain JSON-ready dicts.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from whyfence import store
from whyfence.ingest.events import _CONSTRAINT_RE
from whyfence.store import Commit, Event, Symbol
from whyfence.trust import Trust, origin

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


def retired(conn: sqlite3.Connection, event_id: str, trust: Trust) -> dict[str, Any] | None:
    """Who retired this event and why, if anyone whose word counts did.

    Your own retirements always count. A shared one counts only if a trusted author
    committed it: otherwise a stranger's pull request could silence your rules.
    """
    for retirement in store.retirements(conn, event_id):
        if retirement.origin == "local" or trust.trusts(retirement.author):
            return {"reason": retirement.reason or None, "author": retirement.author,
                    "retired_at": store.to_timestamp(retirement.retired_at)}
    return None


def is_stale(conn: sqlite3.Connection, event_id: str) -> bool:
    """A rule about code that no longer exists: every symbol it is linked to was deleted.

    A rule linked to nothing is about the whole project and never goes stale this way.
    """
    linked = store.linked_symbol_ids(conn, event_id)
    # A symbol with no recorded versions at all says nothing about deletion.
    return bool(linked) and all(store.symbol_history(conn, sid) and not store.is_current(conn, sid)
                                for sid in linked)


def _plural(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def _notes(untrusted: int = 0, stale: int = 0, retired_count: int = 0, withheld: int = 0,
           omitted: int = 0) -> str | None:
    """Say what was left out, and how to see it."""
    notes = []
    if omitted:
        notes.append(f"{_plural(omitted, 'weaker or older record', 'weaker or older records')} beyond the "
                     "limit (strongest first: rules, dead ends, corrections, reverts); pass a higher "
                     "limit to see them")
    if untrusted:
        notes.append(f"{_plural(untrusted, 'rule', 'rules')} from authors you haven't trusted (see `whyfence "
                     "trust`); pass include_untrusted=true to see them, marked")
    if stale:
        notes.append(f"{_plural(stale, 'rule', 'rules')} about code that has since been deleted; "
                     "pass include_stale=true to see them")
    if retired_count:
        notes.append(f"{_plural(retired_count, 'record', 'records')} retired as no longer true "
                     "(`whyfence retire --list`)")
    if withheld:
        notes.append(f"{_plural(withheld, 'instruction-shaped sentence', 'instruction-shaped sentences')} "
                     "from authors you haven't trusted, inside other records; pass include_untrusted=true "
                     "to see them")
    return "Left out: " + "; ".join(notes) + "." if notes else None


WITHHELD = "[instruction from an untrusted author withheld]"
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
# Shell commands are instructions however they're phrased ("... because pip install x fixes it").
_COMMAND_RE = re.compile(
    r"\b(?:curl|wget|pip3?\s+install|npm\s+(?:install|i)|npx|yarn\s+add|sudo|chmod|powershell|iex)\b|"
    r"\|\s*(?:sudo\s+)?(?:sh|bash|zsh|python3?|node)\b|\b(?:bash|sh)\s+-c\b",
    re.I,
)


def withhold_instructions(text: str) -> tuple[str, int]:
    """`text` with every instruction-shaped sentence replaced, and how many were.

    For text from authors you haven't trusted. Their rules are already left out of
    `rules` and `why`, but the same sentence can sit in the body of an incident or
    decision from the same commit; this keeps it from reaching the agent that way.
    Wrapped lines are joined first, so a command split across two lines is still seen.
    """
    sentences = [s for s in _SENTENCE_END.split(" ".join(text.split())) if s]
    kept, withheld = [], 0
    for sentence in sentences:
        if _CONSTRAINT_RE.search(sentence) or _COMMAND_RE.search(sentence):
            withheld += 1
            if not kept or kept[-1] != WITHHELD:
                kept.append(WITHHELD)
        else:
            kept.append(sentence)
    return (" ".join(kept), withheld) if withheld else (text, 0)


def _event(conn: sqlite3.Connection, event: Event, where: dict[str, Any], *,
           withhold: bool = False) -> dict[str, Any]:
    summary, body, withheld = event.summary, event.body, 0
    if withhold:  # before truncating: a cut could split an instruction and hide it from the check
        summary, in_summary = withhold_instructions(summary)
        body, in_body = withhold_instructions(body)
        withheld = in_summary + in_body
    detail = body if len(body) <= DETAIL_LIMIT else body[:DETAIL_LIMIT] + "…"
    return {
        "id": event.id,
        "kind": event.kind,
        "summary": summary,
        "detail": detail or None,
        **({"withheld": withheld} if withheld else {}),
        "confidence": event.confidence,
        "recorded_at": store.to_timestamp(event.recorded_at),
        "commit": event.commit_sha,
        "origin": where,
    }


def lookup_rules(conn: sqlite3.Connection, trust: Trust | None = None, *,
                 include_untrusted: bool = False, include_stale: bool = False) -> dict[str, Any]:
    """Standing rules recorded for this repository, newest first, without repeats.

    Left out unless asked for: rules from untrusted origins (a rule is an
    instruction-shaped sentence, and anyone who can land a commit can write one)
    and stale rules, about code that has since been deleted. Retired rules are
    always left out.
    """
    trust = trust or Trust()
    rules: list[dict[str, Any]] = []
    seen: set[str] = set()
    untrusted = stale = retired_count = 0
    candidates = [(event, origin(conn, event, trust)) for event in store.events_between(conn, kinds=["constraint"])]
    candidates.sort(key=lambda pair: not pair[1]["trusted"])  # stable: a trusted copy wins the dedupe
    for event, where in candidates:
        key = " ".join(event.summary.lower().split())
        if key in seen:
            continue
        seen.add(key)
        if retired(conn, event.id, trust):
            retired_count += 1
            continue
        if not where["trusted"] and not include_untrusted:
            untrusted += 1
            continue
        old = is_stale(conn, event.id)
        if old and not include_stale:
            stale += 1
            continue
        rules.append({
            "id": event.id,
            "rule": event.summary,
            "about": store.symbols_linked_to(conn, event.id),
            "confidence": event.confidence,
            "recorded_at": store.to_timestamp(event.recorded_at),
            "stale": old,
            "origin": where,
        })
    rules.sort(key=lambda rule: rule["recorded_at"], reverse=True)
    result: dict[str, Any] = {"status": "ok", "rules": rules}
    for name, count in (("untrusted_hidden", untrusted), ("stale_hidden", stale), ("retired_hidden", retired_count)):
        if count:
            result[name] = count
    note = _notes(untrusted, stale, retired_count)
    if note:
        result["message"] = note
    elif not rules:
        result["message"] = "No standing rules have been recorded for this repository."
    return result


WHY_LIMIT = 10  # records per `why` answer: flask.app.Flask had 22 (12 KB), all weak
# Rules, dead ends, corrections and reverts say the most about what not to do; then
# explicit records ("fix:", "Why:"); inferred ones last. Newest first within each.
_STRENGTH = {"constraint": 0, "dead_end": 1, "correction": 2, "revert": 3}


def _strength(event: dict[str, Any]) -> int:
    return _STRENGTH.get(event["kind"], 4 if event["confidence"] == "EXTRACTED" else 5)


def lookup_why(conn: sqlite3.Connection, symbol_name: str, trust: Trust | None = None, *,
               include_untrusted: bool = False, limit: int = WHY_LIMIT) -> dict[str, Any]:
    """What's recorded against a symbol. Every event says where it came from and whether
    that author is trusted; constraints from untrusted authors and retired records are
    left out."""
    trust = trust or Trust()
    symbol = _resolve(conn, symbol_name)
    if isinstance(symbol, dict):
        return symbol
    events = []
    untrusted = retired_count = withheld = 0
    for event in store.events_for_symbol(conn, symbol.id):
        if retired(conn, event.id, trust):
            retired_count += 1
            continue
        where = origin(conn, event, trust)
        if event.kind == "constraint" and not where["trusted"] and not include_untrusted:
            untrusted += 1
            continue
        source = store.get_source(conn, event.source_id) if event.source_id else None
        item = _event(conn, event, where, withhold=not where["trusted"] and not include_untrusted)
        withheld += item.get("withheld", 0)
        item["source"] = {"kind": source.kind, "agent": source.agent, "uri": source.uri} if source else None
        events.append(item)
    events.sort(key=_strength)  # stable: newest first within each strength
    limit = max(1, limit)
    omitted = max(0, len(events) - limit)
    events = events[:limit]
    result: dict[str, Any] = {"status": "ok", "symbol": _symbol(symbol), "events": events}
    if untrusted:
        result["untrusted_hidden"] = untrusted
    if retired_count:
        result["retired_hidden"] = retired_count
    if withheld:
        result["withheld"] = withheld
    if omitted:
        result["omitted"] = omitted
    note = _notes(untrusted, 0, retired_count, withheld, omitted)
    if note:
        result["message"] = note
    elif not events:
        result["message"] = "Nothing has been recorded about why this symbol is the way it is."
    return result


SEARCH_LIMIT = 20


def lookup_search(conn: sqlite3.Connection, text: str, trust: Trust | None = None, *,
                  include_untrusted: bool = False, limit: int = SEARCH_LIMIT) -> dict[str, Any]:
    """Recorded events whose summary or detail mention `text`, best match first.

    For questions `why` can't answer because there's no symbol to ask about:
    "what did we decide about retries?", "has anyone tried caching this?".
    Follows the same rules as `why`: constraints from untrusted authors and
    retired records are left out.
    """
    trust = trust or Trust()
    limit = max(1, min(limit, 100))
    if not text.split():
        return {"status": "error", "query": text, "message": "Give some words to search for."}
    hits: list[dict[str, Any]] = []
    untrusted = retired_count = withheld = 0
    for event in store.search_events(conn, text, limit=limit * 3):  # room for the ones left out
        if retired(conn, event.id, trust):
            retired_count += 1
            continue
        where = origin(conn, event, trust)
        if event.kind == "constraint" and not where["trusted"] and not include_untrusted:
            untrusted += 1
            continue
        if len(hits) == limit:
            continue
        item = _event(conn, event, where, withhold=not where["trusted"] and not include_untrusted)
        withheld += item.get("withheld", 0)
        item["symbols"] = store.symbols_linked_to(conn, event.id)
        hits.append(item)
    result: dict[str, Any] = {"status": "ok", "query": text, "events": hits}
    if untrusted:
        result["untrusted_hidden"] = untrusted
    if retired_count:
        result["retired_hidden"] = retired_count
    if withheld:
        result["withheld"] = withheld
    note = _notes(untrusted, 0, retired_count, withheld)
    if note:
        result["message"] = note
    elif not hits:
        result["message"] = f"Nothing recorded mentions {text!r}."
    return result

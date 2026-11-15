import json
import sys
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anyio
import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from whyfence import mcp_server, store
from whyfence.store import Commit, Event, Source, Symbol, SymbolVersion
from whyfence.trust import Trust

T0 = datetime(2026, 3, 1, tzinfo=timezone.utc)


def seed(conn) -> dict[str, str]:
    store.insert_commits(conn, [
        Commit("a" * 40, "Ada <ada@x.io>", T0, "Add token refresh"),
        Commit("b" * 40, "Lin <lin@x.io>", T0 + timedelta(days=7), "Retry refresh with backoff\n\nUpstream 429s."),
    ])
    store.upsert_symbol(conn, Symbol("s-auth", "function", "app.auth.refresh", "python"))
    store.upsert_symbol(conn, Symbol("s-cache", "method", "app.cache.Cache.refresh", "python"))
    store.upsert_symbol(conn, Symbol("s-retry", "function", "app.http.retry_with_backoff", "python"))
    store.record_symbol_version(conn, SymbolVersion("s-auth", "app/auth.py", 10, 24, "h1", "a" * 40))
    store.record_symbol_version(conn, SymbolVersion("s-auth", "app/auth.py", 10, 41, "h2", "b" * 40))
    store.record_symbol_version(conn, SymbolVersion("s-retry", "app/http.py", 1, 9, "h9", "c" * 40))

    store.insert_source(conn, Source("t1", "transcript", agent="claude-code", uri="session/42", captured_at=T0))
    old = store.insert_event(conn, Event("decision", "Refresh tokens on 401 only", recorded_at=T0),
                             links=[("s-auth", "about")])
    new = store.insert_event(conn, Event("decision", "Refresh proactively 60s before expiry",
                                         body="401-driven refresh caused a burst of failed requests. " * 20,
                                         source_id="t1", commit_sha="b" * 40, recorded_at=T0 + timedelta(days=7)),
                             links=[("s-auth", "about")])
    dead = store.insert_event(conn, Event("dead_end", "A global refresh lock deadlocked under load",
                                          confidence="INFERRED", recorded_at=T0 + timedelta(days=5)),
                              links=[("s-auth", "about")])
    store.supersede_event(conn, old.id, new.id)
    return {"new": new.id, "dead": dead.id}


@pytest.fixture
def conn():
    with closing(store.connect()) as connection:
        seed(connection)
        yield connection


@pytest.fixture
def db_path(tmp_path) -> Path:
    path = tmp_path / "index.db"
    with closing(store.connect(path)) as connection:
        seed(connection)
    return path


def test_history_returns_timeline_with_commits(conn):
    result = mcp_server.lookup_history(conn, "app.auth.refresh", Trust(everyone=True))
    assert result["status"] == "ok"
    assert result["symbol"] == {"id": "s-auth", "name": "app.auth.refresh", "kind": "function", "language": "python"}
    first, second = result["versions"]
    assert first["valid_from"] == {"sha": "a" * 40, "date": "2026-03-01T00:00:00Z",
                                   "author": "Ada <ada@x.io>", "subject": "Add token refresh"}
    assert first["valid_to"]["subject"] == "Retry refresh with backoff"
    assert first["current"] is False
    assert second["valid_to"] is None and second["current"] is True
    assert second["lines"] == [10, 41]


def test_history_marks_commits_that_were_never_ingested(conn):
    (version,) = mcp_server.lookup_history(conn, "retry_with_backoff")["versions"]
    assert version["valid_from"] == {"sha": "c" * 40, "ingested": False}


def test_why_returns_live_events_strongest_first(conn):
    result = mcp_server.lookup_why(conn, "s-auth", Trust(everyone=True))
    kinds = [(e["kind"], e["summary"]) for e in result["events"]]
    assert kinds == [
        ("dead_end", "A global refresh lock deadlocked under load"),  # a dead end outranks a newer decision
        ("decision", "Refresh proactively 60s before expiry"),
    ]  # the superseded decision is not reported
    dead_end, decision = result["events"]
    assert decision["source"] == {"kind": "transcript", "agent": "claude-code", "uri": "session/42"}
    assert decision["commit"] == "b" * 40
    assert len(decision["detail"]) == mcp_server.DETAIL_LIMIT + 1 and decision["detail"].endswith("…")
    assert dead_end["confidence"] == "INFERRED" and dead_end["detail"] is None


def test_why_with_no_events_says_so(conn):
    result = mcp_server.lookup_why(conn, "retry_with_backoff")
    assert result["events"] == [] and "Nothing has been recorded" in result["message"]


def test_bare_names_resolve_or_report_ambiguity(conn):
    ambiguous = mcp_server.lookup_why(conn, "refresh")
    assert ambiguous["status"] == "ambiguous"
    assert ambiguous["candidates"] == ["app.auth.refresh", "app.cache.Cache.refresh"]
    assert mcp_server.lookup_why(conn, "Cache.refresh")["symbol"]["id"] == "s-cache"
    assert mcp_server.lookup_why(conn, "fresh")["status"] == "not_found"        # whole names only
    assert mcp_server.lookup_why(conn, "RETRY_WITH_BACKOFF")["status"] == "not_found"  # case-sensitive
    assert mcp_server.lookup_why(conn, "retry%backoff")["status"] == "not_found"       # LIKE wildcards escaped


def test_empty_index_explains_missing_symbols():
    with closing(store.connect()) as empty:
        result = mcp_server.lookup_history(empty, "anything")
    assert result["status"] == "not_found" and "no symbols yet" in result["message"]


def run_client(db: Path, calls: list[tuple[str, dict]], repo: Path | None = None):
    """Start `python -m whyfence mcp` over stdio, as Claude would, and make tool calls."""
    async def session():
        args = ["-m", "whyfence", "mcp", "--db", str(db)] + (["--repo", str(repo)] if repo else [])
        params = StdioServerParameters(command=sys.executable, args=args)
        with anyio.fail_after(60):
            async with stdio_client(params) as (read, write), ClientSession(read, write) as client:
                await client.initialize()
                tools = await client.list_tools()
                results = [await client.call_tool(name, arguments) for name, arguments in calls]
                return tools, results

    return anyio.run(session)


def test_stdio_server_end_to_end(db_path):
    tools, (why, history) = run_client(db_path, [
        ("why", {"symbol_name": "app.auth.refresh"}),
        ("history", {"symbol_name": "app.auth.refresh"}),
    ])
    by_name = {tool.name: tool for tool in tools.tools}
    assert set(by_name) == {"why", "history", "rules", "search"}
    assert by_name["why"].annotations.read_only_hint is True
    assert by_name["history"].input_schema["required"] == ["symbol_name"]

    assert not why.is_error
    payload = why.structured_content  # dict results are sent as structured output
    assert json.loads(why.content[0].text) == payload  # with a JSON text copy for older clients
    assert [e["kind"] for e in payload["events"]] == ["dead_end", "decision"]  # strongest first
    assert not history.is_error
    payload = history.structured_content
    assert len(payload["versions"]) == 2


def test_stdio_server_reports_missing_index_as_tool_error(tmp_path):
    _, (result,) = run_client(tmp_path / "missing.db", [("why", {"symbol_name": "x"})])
    assert result.is_error
    assert "whyfence ingest" in result.content[0].text


def test_search_finds_events_by_text_with_their_symbols(conn):
    result = mcp_server.lookup_search(conn, "deadlocked load", Trust(everyone=True))
    assert [e["summary"] for e in result["events"]] == ["A global refresh lock deadlocked under load"]
    assert result["events"][0]["symbols"] == ["app.auth.refresh"]
    assert result["events"][0]["origin"]["kind"] == "unknown"
    assert mcp_server.lookup_search(conn, "401", Trust(everyone=True))["events"][0]["summary"] == \
        "Refresh proactively 60s before expiry"
    assert mcp_server.lookup_search(conn, "nothing-like-this")["message"].startswith("Nothing recorded mentions")
    assert mcp_server.lookup_search(conn, "   ")["status"] == "error"
    assert mcp_server.lookup_search(conn, 'prod: "p99 OR *')["status"] == "ok"  # FTS syntax can't break it


def test_search_over_stdio_and_cli(db_path, capsys):
    from whyfence.cli import main

    _, (result,) = run_client(db_path, [("search", {"text": "deadlocked"})])
    assert not result.is_error
    assert result.structured_content["events"][0]["kind"] == "dead_end"
    assert main(["search", "deadlocked", "--db", str(db_path)]) == 0
    out = capsys.readouterr().out  # the seed event has no known author: its text is withheld
    assert "deadlocked" not in out and "withheld" in out and "app.auth.refresh" in out
    assert main(["search", "deadlocked", "--db", str(db_path), "--include-untrusted"]) == 0
    out = capsys.readouterr().out
    assert "deadlocked" in out and "untrusted author" in out
    assert main(["search", "   ", "--db", str(db_path)]) == 1


# --------------------------------------------------------------------------- #
# A busy symbol: strongest records first, a bounded answer, and a count of the rest
# --------------------------------------------------------------------------- #

def busy_symbol(conn) -> None:
    store.upsert_symbol(conn, Symbol("s-app", "class", "app.App", "python"))
    store.insert_source(conn, Source("t1", "transcript", agent="claude-code", uri="s/1", captured_at=T0))
    for i in range(30):  # old, weak: "Fixes #N" subjects
        store.insert_event(conn, Event("incident", f"Tweak {i}. Fixes #{i}", confidence="INFERRED",
                                       recorded_at=T0 + timedelta(days=i), body="x" * 600),
                           links=[("s-app", "about")])
    strong = [("incident", "fix: app crashed on empty config", "EXTRACTED", 1),
              ("revert", 'Revert "Lazy-load extensions"', "EXTRACTED", 2),
              ("correction", "No, don't cache the app instance", "INFERRED", 3),
              ("dead_end", "A per-request app copy deadlocked", "INFERRED", 4),
              ("constraint", "Never create App() at import time", "INFERRED", 5)]
    for kind, summary, confidence, day in strong:
        store.insert_event(conn, Event(kind, summary, source_id="t1", confidence=confidence,
                                       recorded_at=T0 + timedelta(days=day)), links=[("s-app", "about")])


def test_why_ranks_strong_records_first_and_says_what_it_left_out():
    with closing(store.connect()) as conn:
        busy_symbol(conn)
        answer = mcp_server.lookup_why(conn, "app.App", Trust(everyone=True))
    kinds = [e["kind"] for e in answer["events"]]
    assert kinds[:5] == ["constraint", "dead_end", "correction", "revert", "incident"]
    assert answer["events"][4]["confidence"] == "EXTRACTED"
    assert len(answer["events"]) == mcp_server.WHY_LIMIT
    assert answer["omitted"] == 35 - mcp_server.WHY_LIMIT
    assert "limit" in answer["message"]
    assert len(json.dumps(answer)) < 8_000  # small enough for an agent's context


def test_why_limit_can_be_raised_and_weak_records_stay_newest_first():
    with closing(store.connect()) as conn:
        busy_symbol(conn)
        everything = mcp_server.lookup_why(conn, "app.App", Trust(everyone=True), limit=100)
    assert len(everything["events"]) == 35 and "omitted" not in everything
    weak = [e["summary"] for e in everything["events"][5:]]
    assert weak[0] == "Tweak 29. Fixes #29" and weak[-1] == "Tweak 0. Fixes #0"


# --------------------------------------------------------------------------- #
# Which whyfence is this?
# --------------------------------------------------------------------------- #

def test_cli_reports_its_version(capsys):
    from importlib.metadata import version

    from whyfence.cli import main

    with pytest.raises(SystemExit) as stop:
        main(["--version"])
    assert stop.value.code == 0
    assert capsys.readouterr().out.strip() == f"whyfence {version('whyfence')}"


def test_mcp_server_reports_its_version_to_clients(db_path):
    from importlib.metadata import version

    async def handshake():
        params = StdioServerParameters(command=sys.executable, args=["-m", "whyfence", "mcp", "--db", str(db_path)])
        with anyio.fail_after(60):
            async with stdio_client(params) as (read, write), ClientSession(read, write) as client:
                return await client.initialize()

    info = anyio.run(handshake).server_info
    assert info.name == "whyfence" and info.version == version("whyfence")

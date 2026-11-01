import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone

import pytest

from whyfence import store
from whyfence.store import Commit, Event, Source, Symbol, SymbolVersion

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def at(days: int) -> datetime:
    return T0 + timedelta(days=days)


@pytest.fixture
def conn():
    with closing(store.connect()) as connection:
        yield connection


def commit(sha: str, days: int, parents: tuple[str, ...] = ()) -> Commit:
    return Commit(sha, "Ada <ada@example.com>", at(days), f"commit {sha}\n\nbody", parents)


def test_file_database_uses_wal_and_init_is_idempotent(tmp_path):
    path = tmp_path / "nested" / "index.db"
    with closing(store.connect(path)) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA user_version").fetchone()[0] == store.SCHEMA_VERSION
        store.insert_commits(conn, [commit("a", 0)])
    with closing(store.connect(path)) as conn:  # reopening must not recreate the schema
        assert store.count_commits(conn) == 1


def test_newer_schema_is_rejected(tmp_path):
    path = tmp_path / "index.db"
    with closing(sqlite3.connect(path)) as raw:
        raw.execute(f"PRAGMA user_version = {store.SCHEMA_VERSION + 1}")
    with pytest.raises(store.StoreError, match="newer"):
        store.connect(path)


def test_commits_round_trip_and_ignore_duplicates(conn):
    assert store.insert_commits(conn, [commit("a", 0), commit("b", 1, ("a",))]) == 2
    assert store.insert_commits(conn, [commit("b", 1, ("a",)), commit("c", 2, ("b",))]) == 1
    stored = store.get_commit(conn, "b")
    assert stored == commit("b", 1, ("a",))
    assert stored.subject == "commit b"
    assert store.get_commit(conn, "missing") is None


def test_commits_between_is_half_open_and_ordered(conn):
    store.insert_commits(conn, [commit(sha, day) for sha, day in (("a", 0), ("b", 1), ("c", 2), ("d", 3))])
    assert [c.sha for c in store.commits_between(conn, at(1), at(3))] == ["c", "b"]
    assert [c.sha for c in store.commits_between(conn, since=at(2), newest_first=False)] == ["c", "d"]
    assert [c.sha for c in store.commits_between(conn, until=at(1))] == ["a"]
    assert [c.sha for c in store.commits_between(conn, limit=2)] == ["d", "c"]


def test_timestamps_normalize_to_utc_and_reject_naive(conn):
    ist = timezone(timedelta(hours=5, minutes=30))
    store.insert_commits(conn, [Commit("x", "A <a@x>", datetime(2026, 1, 1, 5, 30, tzinfo=ist), "m")])
    assert conn.execute("SELECT authored_at FROM commits").fetchone()[0] == "2026-01-01T00:00:00Z"
    assert store.get_commit(conn, "x").authored_at == T0
    with pytest.raises(ValueError, match="naive"):
        store.commits_between(conn, since=datetime(2026, 1, 1))


def test_symbol_versions_close_and_answer_as_of(conn):
    store.insert_commits(conn, [commit("a", 0), commit("b", 10), commit("c", 20)])
    store.upsert_symbol(conn, Symbol("s1", "function", "app.auth.refresh", "python"))
    store.record_symbol_version(conn, SymbolVersion("s1", "app/auth.py", 10, 20, "h1", "a"))
    store.record_symbol_version(conn, SymbolVersion("s1", "app/auth.py", 12, 30, "h2", "b"))
    # A rename keeps the stable id.
    store.upsert_symbol(conn, Symbol("s1", "function", "app.session.refresh", "python"))
    store.record_symbol_version(conn, SymbolVersion("s1", "app/session.py", 1, 25, "h3", "c"))

    history = store.symbol_history(conn, "s1")
    assert [(v.body_hash, v.valid_from_sha, v.valid_to_sha) for v in history] == [
        ("h1", "a", "b"), ("h2", "b", "c"), ("h3", "c", None),
    ]
    assert store.get_symbol(conn, "s1").qualified_name == "app.session.refresh"
    assert [v.body_hash for _, v in store.symbols_as_of(conn, at(5))] == ["h1"]
    assert [v.body_hash for _, v in store.symbols_as_of(conn, at(10))] == ["h2"]
    assert [v.file_path for _, v in store.symbols_as_of(conn, at(30))] == ["app/session.py"]
    assert store.symbols_as_of(conn, at(-1)) == []


def test_events_links_time_queries_and_supersede(conn):
    store.upsert_symbol(conn, Symbol("s1", "function", "app.retry", "python"))
    store.insert_source(conn, Source("src1", "transcript", agent="claude-code", captured_at=at(0)))
    old = store.insert_event(conn, Event("decision", "Retry 3 times with backoff", source_id="src1",
                                         recorded_at=at(1)), links=[("s1", "about")])
    dead = store.insert_event(conn, Event("dead_end", "Tried a global lock; deadlocked under load",
                                          recorded_at=at(2), confidence="INFERRED"), links=[("s1", "about")])
    new = store.insert_event(conn, Event("decision", "Retry 5 times; upstream SLA changed", recorded_at=at(3)),
                             links=[("s1", "about"), ("s1", "changed")])
    store.supersede_event(conn, old.id, new.id)

    assert [e.id for e in store.events_between(conn)] == [new.id, dead.id]
    assert [e.id for e in store.events_between(conn, include_superseded=True)] == [new.id, dead.id, old.id]
    assert [e.id for e in store.events_between(conn, at(2), at(3))] == [dead.id]
    assert [e.id for e in store.events_between(conn, kinds=["dead_end"])] == [dead.id]
    assert [e.id for e in store.events_for_symbol(conn, "s1")] == [new.id, dead.id]
    assert store.events_between(conn, kinds=["dead_end"])[0] == dead
    with pytest.raises(store.StoreError):
        store.supersede_event(conn, "nope", new.id)


def test_check_constraints_reject_unknown_kinds(conn):
    with pytest.raises(sqlite3.IntegrityError):
        store.insert_event(conn, Event("vibe", "not a real kind"))


def test_full_text_search_tolerates_query_syntax(conn):
    lock = store.insert_event(conn, Event("dead_end", "Global lock deadlocked", body="Seen in prod: p99 > 30s"))
    store.insert_event(conn, Event("decision", "Use Decimal for money", body="float rounding broke invoices"))
    assert [e.id for e in store.search_events(conn, "deadlocked")] == [lock.id]
    assert [e.id for e in store.search_events(conn, 'prod: "p99')] == [lock.id]
    assert store.search_events(conn, "   ") == []
    conn.execute("UPDATE events SET summary = 'Mutex starvation' WHERE id = ?", (lock.id,))
    assert store.search_events(conn, "deadlocked") == []
    assert [e.id for e in store.search_events(conn, "starvation")] == [lock.id]

"""SQLite storage: commits, code symbols, and the events that explain them.

The database is a local index. Everything in it can be rebuilt from git history
and the shared event log, so it is safe to delete. It runs in WAL mode so the
MCP server can read while an ingestor writes.

Timestamps are stored as UTC text in one fixed format (`2026-11-01T09:30:00Z`),
so comparing the strings compares the times.
"""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Sequence

SCHEMA_VERSION = 2

EVENT_KINDS = ("decision", "dead_end", "correction", "constraint", "revert", "incident")
SOURCE_KINDS = ("commit", "pr", "transcript", "adr", "manual")
LINK_RELATIONS = ("about", "changed", "reverted", "broke")
CONFIDENCE = ("EXTRACTED", "INFERRED")

SCHEMA = f"""
CREATE TABLE commits (
    sha         TEXT PRIMARY KEY,
    author      TEXT NOT NULL,            -- "Name <email>"
    authored_at TEXT NOT NULL,
    message     TEXT NOT NULL,
    parents     TEXT NOT NULL DEFAULT ''  -- space-separated parent SHAs
);
CREATE INDEX commits_authored_at ON commits (authored_at);

CREATE TABLE symbols (
    id             TEXT PRIMARY KEY,      -- stable across renames and moves
    kind           TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    language       TEXT NOT NULL
);
CREATE INDEX symbols_qualified_name ON symbols (qualified_name);

CREATE TABLE symbol_versions (
    symbol_id      TEXT NOT NULL REFERENCES symbols (id) ON DELETE CASCADE,
    file_path      TEXT NOT NULL,
    start_line     INTEGER NOT NULL,
    end_line       INTEGER NOT NULL,
    body_hash      TEXT NOT NULL,
    valid_from_sha TEXT NOT NULL,
    valid_to_sha   TEXT,                  -- NULL = still current
    qualified_name TEXT,                  -- name at this version; a rename starts a new one
    PRIMARY KEY (symbol_id, valid_from_sha)
);
CREATE INDEX symbol_versions_file ON symbol_versions (file_path);
CREATE INDEX symbol_versions_current ON symbol_versions (symbol_id) WHERE valid_to_sha IS NULL;

CREATE TABLE sources (
    id           TEXT PRIMARY KEY,
    kind         TEXT NOT NULL CHECK (kind IN {SOURCE_KINDS}),
    agent        TEXT,                    -- claude-code, codex, cursor, ...
    uri          TEXT,
    content_hash TEXT,
    captured_at  TEXT NOT NULL
);

CREATE TABLE events (
    id            TEXT PRIMARY KEY,
    kind          TEXT NOT NULL CHECK (kind IN {EVENT_KINDS}),
    summary       TEXT NOT NULL,
    body          TEXT NOT NULL DEFAULT '',
    source_id     TEXT REFERENCES sources (id),
    commit_sha    TEXT,
    confidence    TEXT NOT NULL CHECK (confidence IN {CONFIDENCE}),
    recorded_at   TEXT NOT NULL,
    superseded_by TEXT REFERENCES events (id)
);
CREATE INDEX events_recorded_at ON events (recorded_at);
CREATE INDEX events_commit_sha ON events (commit_sha);

CREATE TABLE event_links (
    event_id  TEXT NOT NULL REFERENCES events (id) ON DELETE CASCADE,
    symbol_id TEXT NOT NULL REFERENCES symbols (id) ON DELETE CASCADE,
    relation  TEXT NOT NULL CHECK (relation IN {LINK_RELATIONS}),
    PRIMARY KEY (event_id, symbol_id, relation)
);
CREATE INDEX event_links_symbol ON event_links (symbol_id);

-- Keyed by event id rather than declared with content='events': VACUUM may
-- renumber the rowids of a table without an INTEGER PRIMARY KEY, which would
-- silently detach an external-content index from its rows.
-- Ingestor bookkeeping, e.g. the last commit whose symbols were indexed.
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE VIRTUAL TABLE events_fts USING fts5 (event_id UNINDEXED, summary, body);

CREATE TRIGGER events_fts_insert AFTER INSERT ON events BEGIN
    INSERT INTO events_fts (event_id, summary, body) VALUES (new.id, new.summary, new.body);
END;
CREATE TRIGGER events_fts_delete AFTER DELETE ON events BEGIN
    DELETE FROM events_fts WHERE event_id = old.id;
END;
CREATE TRIGGER events_fts_update AFTER UPDATE OF summary, body ON events BEGIN
    UPDATE events_fts SET summary = new.summary, body = new.body WHERE event_id = old.id;
END;
"""

# Steps from each older schema version to the next.
MIGRATIONS = {
    1: """
ALTER TABLE symbol_versions ADD COLUMN qualified_name TEXT;
CREATE INDEX symbol_versions_current ON symbol_versions (symbol_id) WHERE valid_to_sha IS NULL;
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
""",
}


class StoreError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Commit:
    sha: str
    author: str
    authored_at: datetime
    message: str
    parents: tuple[str, ...] = ()

    @property
    def subject(self) -> str:
        return self.message.split("\n", 1)[0]


@dataclass(frozen=True)
class Symbol:
    id: str
    kind: str
    qualified_name: str
    language: str


@dataclass(frozen=True)
class SymbolVersion:
    symbol_id: str
    file_path: str
    start_line: int
    end_line: int
    body_hash: str
    valid_from_sha: str
    valid_to_sha: str | None = None
    qualified_name: str | None = None


@dataclass(frozen=True)
class Source:
    id: str
    kind: str
    agent: str | None = None
    uri: str | None = None
    content_hash: str | None = None
    captured_at: datetime = field(default_factory=_now)


@dataclass(frozen=True)
class Event:
    kind: str
    summary: str
    body: str = ""
    source_id: str | None = None
    commit_sha: str | None = None
    confidence: str = "EXTRACTED"
    recorded_at: datetime = field(default_factory=_now)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    superseded_by: str | None = None


# --------------------------------------------------------------------------- #
# Connection
# --------------------------------------------------------------------------- #


_BATCHING: set[int] = set()  # id() of connections inside batch()


@contextmanager
def batch(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Group store writes into one transaction: all of them land, or none do."""
    if id(conn) in _BATCHING:
        yield conn
        return
    _BATCHING.add(id(conn))
    try:
        with conn:
            yield conn
    finally:
        _BATCHING.discard(id(conn))


def _tx(conn: sqlite3.Connection) -> AbstractContextManager:
    """Each write commits on its own, unless it runs inside batch()."""
    return nullcontext() if id(conn) in _BATCHING else conn


def to_timestamp(when: datetime) -> str:
    if when.tzinfo is None:
        raise ValueError("naive datetimes are ambiguous; pass a timezone-aware datetime")
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def from_timestamp(text: str) -> datetime:
    return datetime.fromisoformat(text)


def connect(path: str | Path = ":memory:") -> sqlite3.Connection:
    """Open (and if needed create) a whyfence database."""
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")  # safe under WAL; fsyncs at checkpoints
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create the schema on a new database; no-op on a current one."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version == SCHEMA_VERSION:
        return
    if version > SCHEMA_VERSION:
        raise StoreError(f"database schema v{version} is newer than this whyfence (v{SCHEMA_VERSION})")
    if version == 0:
        script = SCHEMA
    elif all(v in MIGRATIONS for v in range(version, SCHEMA_VERSION)):
        script = "\n".join(MIGRATIONS[v] for v in range(version, SCHEMA_VERSION))
    else:
        raise StoreError(f"no migration from schema v{version}")
    try:
        conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {SCHEMA_VERSION};\nCOMMIT;")
    except sqlite3.OperationalError as exc:
        conn.rollback()
        if "fts5" in str(exc):
            raise StoreError("this Python's SQLite was built without FTS5") from exc
        raise


# --------------------------------------------------------------------------- #
# Commits
# --------------------------------------------------------------------------- #


def _commit_from_row(row: sqlite3.Row) -> Commit:
    return Commit(row["sha"], row["author"], from_timestamp(row["authored_at"]), row["message"],
                  tuple(row["parents"].split()))


def insert_commits(conn: sqlite3.Connection, commits: Iterable[Commit]) -> int:
    """Insert commits, skipping SHAs already stored. Returns how many were new."""
    rows = [(c.sha, c.author, to_timestamp(c.authored_at), c.message, " ".join(c.parents)) for c in commits]
    with _tx(conn):
        before = conn.total_changes
        conn.executemany(
            "INSERT OR IGNORE INTO commits (sha, author, authored_at, message, parents) VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        return conn.total_changes - before


def get_commit(conn: sqlite3.Connection, sha: str) -> Commit | None:
    row = conn.execute("SELECT * FROM commits WHERE sha = ?", (sha,)).fetchone()
    return _commit_from_row(row) if row else None


def count_commits(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT count(*) FROM commits").fetchone()[0]


def _time_filter(column: str, since: datetime | None, until: datetime | None) -> tuple[list[str], list[object]]:
    clauses: list[str] = []
    params: list[object] = []
    if since is not None:
        clauses.append(f"{column} >= ?")
        params.append(to_timestamp(since))
    if until is not None:
        clauses.append(f"{column} < ?")
        params.append(to_timestamp(until))
    return clauses, params


def commits_between(
    conn: sqlite3.Connection,
    since: datetime | None = None,
    until: datetime | None = None,
    *,
    limit: int | None = None,
    newest_first: bool = True,
) -> list[Commit]:
    """Commits authored in [since, until), either bound optional."""
    clauses, params = _time_filter("authored_at", since, until)
    sql = "SELECT * FROM commits"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += f" ORDER BY authored_at {'DESC' if newest_first else 'ASC'}, rowid"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return [_commit_from_row(row) for row in conn.execute(sql, params)]


# --------------------------------------------------------------------------- #
# Symbols
# --------------------------------------------------------------------------- #


def upsert_symbol(conn: sqlite3.Connection, symbol: Symbol) -> None:
    """Insert a symbol, or update its name in place (a rename keeps the id)."""
    with _tx(conn):
        conn.execute(
            """INSERT INTO symbols (id, kind, qualified_name, language) VALUES (?, ?, ?, ?)
               ON CONFLICT (id) DO UPDATE SET
                   kind = excluded.kind, qualified_name = excluded.qualified_name, language = excluded.language""",
            (symbol.id, symbol.kind, symbol.qualified_name, symbol.language),
        )


def get_symbol(conn: sqlite3.Connection, symbol_id: str) -> Symbol | None:
    row = conn.execute("SELECT * FROM symbols WHERE id = ?", (symbol_id,)).fetchone()
    return _symbol_from_row(row) if row else None


def _symbol_from_row(row: sqlite3.Row) -> Symbol:
    return Symbol(row["id"], row["kind"], row["qualified_name"], row["language"])


# Separators between a qualified name's scope and the symbol: Python/JS `.`,
# Rust/C++ `::`, Ruby `#`, path-style `/`.
_SCOPE_SEPARATORS = (".", "::", "#", "/")


def count_symbols(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT count(*) FROM symbols").fetchone()[0]


def find_symbols(conn: sqlite3.Connection, name: str, *, limit: int = 20) -> list[Symbol]:
    """Resolve a name to symbols: an exact id or qualified name, else a trailing-name match.

    `refresh` matches `app.auth.refresh` and `Session::refresh`, but not `prefresh`.
    """
    rows = conn.execute("SELECT * FROM symbols WHERE id = ? OR qualified_name = ? ORDER BY qualified_name",
                        (name, name)).fetchall()
    if not rows:
        escaped = name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        patterns = [f"%{sep}{escaped}" for sep in _SCOPE_SEPARATORS]
        rows = conn.execute(
            f"""SELECT * FROM symbols WHERE {' OR '.join("qualified_name LIKE ? ESCAPE '\\'" for _ in patterns)}
                ORDER BY length(qualified_name), qualified_name LIMIT ?""",
            (*patterns, limit),
        ).fetchall()
        # LIKE is case-insensitive for ASCII; symbol names are not.
        rows = [r for r in rows if r["qualified_name"].endswith(name)]
    return [_symbol_from_row(row) for row in rows]


def record_symbol_version(conn: sqlite3.Connection, version: SymbolVersion) -> None:
    """Add a version of a symbol, closing whichever version was current until now."""
    with _tx(conn):
        conn.execute(
            """UPDATE symbol_versions SET valid_to_sha = ?
               WHERE symbol_id = ? AND valid_to_sha IS NULL AND valid_from_sha != ?""",
            (version.valid_from_sha, version.symbol_id, version.valid_from_sha),
        )
        conn.execute(
            """INSERT INTO symbol_versions
                   (symbol_id, file_path, start_line, end_line, body_hash, valid_from_sha, valid_to_sha,
                    qualified_name)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (version.symbol_id, version.file_path, version.start_line, version.end_line,
             version.body_hash, version.valid_from_sha, version.valid_to_sha, version.qualified_name),
        )


def close_symbol_version(conn: sqlite3.Connection, symbol_id: str, sha: str) -> None:
    """End a symbol's current version at `sha` (the symbol was deleted there)."""
    with _tx(conn):
        conn.execute("UPDATE symbol_versions SET valid_to_sha = ? WHERE symbol_id = ? AND valid_to_sha IS NULL",
                     (sha, symbol_id))


def current_symbol_versions(conn: sqlite3.Connection) -> list[tuple[Symbol, SymbolVersion]]:
    """Every symbol that exists at the last indexed commit, with its current version."""
    rows = conn.execute(
        """SELECT s.id, s.kind, s.qualified_name AS symbol_name, s.language, v.*
           FROM symbol_versions v JOIN symbols s ON s.id = v.symbol_id
           WHERE v.valid_to_sha IS NULL ORDER BY v.file_path, v.start_line""",
    )
    return [(Symbol(r["id"], r["kind"], r["symbol_name"], r["language"]), _version_from_row(r)) for r in rows]


def clear_symbol_versions(conn: sqlite3.Connection) -> None:
    """Drop all versions so symbol history can be rebuilt. Symbols (and event links) stay."""
    with _tx(conn):
        conn.execute("DELETE FROM symbol_versions")


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    with _tx(conn):
        conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                     (key, value))


def _version_from_row(row: sqlite3.Row) -> SymbolVersion:
    return SymbolVersion(row["symbol_id"], row["file_path"], row["start_line"], row["end_line"],
                         row["body_hash"], row["valid_from_sha"], row["valid_to_sha"], row["qualified_name"])


def symbol_history(conn: sqlite3.Connection, symbol_id: str) -> list[SymbolVersion]:
    """Every recorded version of a symbol, in the order history produced them."""
    # Insertion order follows history; author dates can run backwards after a rebase.
    rows = conn.execute("SELECT * FROM symbol_versions WHERE symbol_id = ? ORDER BY rowid", (symbol_id,))
    return [_version_from_row(row) for row in rows]


@dataclass(frozen=True)
class TimelineEntry:
    version: SymbolVersion
    introduced_by: Commit | None  # None if that commit hasn't been ingested
    replaced_by: Commit | None  # None while the version is current, or if not ingested


def symbol_timeline(conn: sqlite3.Connection, symbol_id: str) -> list[TimelineEntry]:
    """A symbol's versions, oldest first, with the commits that bound each one."""
    return [
        TimelineEntry(
            version,
            get_commit(conn, version.valid_from_sha),
            get_commit(conn, version.valid_to_sha) if version.valid_to_sha else None,
        )
        for version in symbol_history(conn, symbol_id)
    ]


def symbols_as_of(conn: sqlite3.Connection, when: datetime) -> list[tuple[Symbol, SymbolVersion]]:
    """Symbols as they stood at `when`, judged by the author dates of the bounding commits."""
    stamp = to_timestamp(when)
    rows = conn.execute(
        """SELECT s.id, s.kind, s.qualified_name, s.language, v.*
           FROM symbol_versions v
           JOIN symbols s ON s.id = v.symbol_id
           JOIN commits c_from ON c_from.sha = v.valid_from_sha
           LEFT JOIN commits c_to ON c_to.sha = v.valid_to_sha
           WHERE c_from.authored_at <= ? AND (v.valid_to_sha IS NULL OR c_to.authored_at > ?)
           ORDER BY s.qualified_name""",
        (stamp, stamp),
    )
    return [(Symbol(r["id"], r["kind"], r["qualified_name"], r["language"]), _version_from_row(r)) for r in rows]


# --------------------------------------------------------------------------- #
# Sources and events
# --------------------------------------------------------------------------- #


def insert_source(conn: sqlite3.Connection, source: Source) -> None:
    with _tx(conn):
        conn.execute(
            """INSERT OR IGNORE INTO sources (id, kind, agent, uri, content_hash, captured_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (source.id, source.kind, source.agent, source.uri, source.content_hash,
             to_timestamp(source.captured_at)),
        )


def get_source(conn: sqlite3.Connection, source_id: str) -> Source | None:
    row = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
    if row is None:
        return None
    return Source(row["id"], row["kind"], row["agent"], row["uri"], row["content_hash"],
                  from_timestamp(row["captured_at"]))


def insert_event(
    conn: sqlite3.Connection,
    event: Event,
    links: Iterable[tuple[str, str]] = (),
) -> Event:
    """Store an event and link it to symbols as `(symbol_id, relation)` pairs."""
    with _tx(conn):
        conn.execute(
            """INSERT INTO events (id, kind, summary, body, source_id, commit_sha, confidence,
                                   recorded_at, superseded_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (event.id, event.kind, event.summary, event.body, event.source_id, event.commit_sha,
             event.confidence, to_timestamp(event.recorded_at), event.superseded_by),
        )
        conn.executemany(
            "INSERT OR IGNORE INTO event_links (event_id, symbol_id, relation) VALUES (?, ?, ?)",
            [(event.id, symbol_id, relation) for symbol_id, relation in links],
        )
    return event


def supersede_event(conn: sqlite3.Connection, old_id: str, new_id: str) -> None:
    """Mark `old_id` as replaced by `new_id`. The old event is kept as history."""
    with _tx(conn):
        updated = conn.execute("UPDATE events SET superseded_by = ? WHERE id = ?", (new_id, old_id)).rowcount
    if not updated:
        raise StoreError(f"no event {old_id}")


def _event_from_row(row: sqlite3.Row) -> Event:
    return Event(
        kind=row["kind"], summary=row["summary"], body=row["body"], source_id=row["source_id"],
        commit_sha=row["commit_sha"], confidence=row["confidence"],
        recorded_at=from_timestamp(row["recorded_at"]), id=row["id"], superseded_by=row["superseded_by"],
    )


def events_between(
    conn: sqlite3.Connection,
    since: datetime | None = None,
    until: datetime | None = None,
    *,
    kinds: Sequence[str] | None = None,
    include_superseded: bool = False,
    limit: int | None = None,
) -> list[Event]:
    """Events recorded in [since, until), newest first."""
    clauses, params = _time_filter("recorded_at", since, until)
    if kinds:
        clauses.append(f"kind IN ({', '.join('?' * len(kinds))})")
        params.extend(kinds)
    if not include_superseded:
        clauses.append("superseded_by IS NULL")
    sql = "SELECT * FROM events"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY recorded_at DESC, rowid DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return [_event_from_row(row) for row in conn.execute(sql, params)]


def events_for_symbol(conn: sqlite3.Connection, symbol_id: str, *, include_superseded: bool = False) -> list[Event]:
    """Everything known about why a symbol is the way it is, newest first."""
    sql = """SELECT DISTINCT e.*, e.rowid FROM events e JOIN event_links l ON l.event_id = e.id
             WHERE l.symbol_id = ?"""
    if not include_superseded:
        sql += " AND e.superseded_by IS NULL"
    sql += " ORDER BY e.recorded_at DESC, e.rowid DESC"
    return [_event_from_row(row) for row in conn.execute(sql, (symbol_id,))]


def _fts_query(text: str) -> str:
    # Quote every term so user text can't trip FTS5 query syntax (`-`, `:`, `"`, `*`).
    return " ".join('"' + term.replace('"', '""') + '"' for term in text.split())


def search_events(conn: sqlite3.Connection, text: str, *, limit: int = 20,
                  include_superseded: bool = False) -> list[Event]:
    """Full-text search over event summaries and bodies, best match first."""
    query = _fts_query(text)
    if not query:
        return []
    sql = """SELECT e.* FROM events_fts f JOIN events e ON e.id = f.event_id
             WHERE events_fts MATCH ?"""
    if not include_superseded:
        sql += " AND e.superseded_by IS NULL"
    sql += " ORDER BY bm25(events_fts) LIMIT ?"
    return [_event_from_row(row) for row in conn.execute(sql, (query, limit))]

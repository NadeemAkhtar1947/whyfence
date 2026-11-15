"""Every query whyfence runs on a big table must use an index.

On django (256k symbol versions) one unindexed lookup per commit event cost
381 ms; the whole ingest was dominated by it. This records the SQL that ingest,
the queries and the PR check actually run (SQLite's trace hook), and checks
each statement's plan, so a new query can't quietly reintroduce a full scan.
"""

import re
from contextlib import closing

from test_symbols import Repo, needs_git, repo  # noqa: F401  (fixture)

from whyfence import check, queries, store, team
from whyfence.ingest import events, git, symbols
from whyfence.trust import Trust

pytestmark = needs_git

BIG_TABLES = ("symbol_versions", "events", "event_links", "commits", "symbols", "retirements")

# Full scans that are the point of the query, not an accident.
ALLOWED_SCANS = [
    (r"^SELECT count\(\*\) FROM", "counting rows"),
    (r"^SELECT \* FROM commits ORDER BY", "mining walks every commit once"),
    (r"^SELECT \* FROM events (?:WHERE kind IN|ORDER BY)", "listing every rule or event"),
    (r"FROM symbols WHERE .*LIKE", "suffix match on a bare name (`refresh` -> `app.auth.refresh`)"),
    (r"^SELECT \* FROM retirements ORDER BY", "listing every retirement"),
    (r"^DELETE FROM symbol_versions$", "rebuilding history"),
]


def ingest(target: Repo, conn) -> None:
    git.ingest_git(conn, target.path)
    symbols.ingest_symbols(conn, target.path)
    events.ingest_events(conn, target.path, transcripts=False)
    team.import_shared(conn, target.path)


def full_scans(conn, statements: list[str]) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for sql in statements:
        if not re.match(r"\s*(SELECT|UPDATE|DELETE)", sql, re.I) or "sqlite_" in sql or "PRAGMA" in sql:
            continue
        if any(re.search(pattern, " ".join(sql.split()), re.I) for pattern, _ in ALLOWED_SCANS):
            continue
        plan = [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql)]
        # "SCAN t USING INDEX i" still reads every row (in index order); only SEARCH is a lookup.
        scans = [step for step in plan if re.match(rf"SCAN (?:\w+ AS )?(?:{'|'.join(BIG_TABLES)})\b", step)
                 or re.match(rf"SCAN (?:{'|'.join(BIG_TABLES)}) ", step)]
        if scans:
            found[" ".join(sql.split())[:160]] = scans
    return found


def test_hot_queries_use_indexes(repo):  # noqa: F811
    code = "def refresh(token):\n    return renew(token)\n\n\ndef logout(s):\n    s.clear()\n"
    repo.commit("Add auth\n\nNever call refresh() from a request handler.", {"app/auth.py": code})
    repo.commit("fix: renew earlier\n\nWhy: tokens expired mid-request.",
                {"app/auth.py": code.replace("renew(token)", "renew(token, early=True)")})
    repo.git("checkout", "-q", "-b", "feature")
    repo.commit("Edit logout", {"app/auth.py": code.replace("s.clear()", "s.clear(all=True)")})

    statements: list[str] = []
    with closing(store.connect()) as conn:
        conn.set_trace_callback(statements.append)
        ingest(repo, conn)
        trust = Trust(everyone=True)
        queries.lookup_rules(conn, trust)
        queries.lookup_why(conn, "refresh", trust)
        queries.lookup_history(conn, "app.auth.refresh")
        queries.lookup_search(conn, "tokens", trust)
        check.check(conn, repo.path, "main")
        store.find_event_ids(conn, "abcdef")
        conn.set_trace_callback(None)
        assert len(statements) > 50  # the trace really saw the workload
        found = full_scans(conn, statements)
        assert not found, "full scans:\n" + "\n".join(f"{sql}\n    -> {plan}" for sql, plan in found.items())


def test_a_v3_index_gains_the_new_indexes_and_keeps_its_data(tmp_path):
    import sqlite3

    path = tmp_path / "v3.db"
    with closing(store.connect(path)) as conn:
        store.insert_event(conn, store.Event("decision", "kept across the upgrade"))
    raw = sqlite3.connect(path)  # what 0.3.0 left behind
    raw.executescript("DROP INDEX symbol_versions_from; DROP INDEX symbol_versions_to; "
                      "ALTER TABLE commits DROP COLUMN committer; ALTER TABLE sources DROP COLUMN committer; "
                      "PRAGMA user_version = 3;")
    raw.close()
    with closing(store.connect(path)) as conn:
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
        assert {"symbol_versions_from", "symbol_versions_to"} <= names
        assert [e.summary for e in store.events_between(conn)] == ["kept across the upgrade"]
        assert conn.execute("PRAGMA user_version").fetchone()[0] == store.SCHEMA_VERSION == 5


def test_every_ingest_stage_reports_progress_against_a_known_total(repo):  # noqa: F811
    for i in range(5):
        repo.commit(f"step {i}", {"a.py": f"def f():\n    return {i}\n"})
    repo.git("checkout", "-q", "-b", "side")
    repo.commit("side work", {"b.py": "def g():\n    return 1\n"})
    repo.git("checkout", "-q", "main")
    repo.git("merge", "-q", "--no-ff", "-m", "merge", "side")
    calls: dict[str, list[tuple[int, int]]] = {"git": [], "symbols": [], "events": []}
    with closing(store.connect()) as conn:
        git.ingest_git(conn, repo.path, progress=lambda d, t: calls["git"].append((d, t)))
        symbols.ingest_symbols(conn, repo.path, progress=lambda d, t: calls["symbols"].append((d, t)))
        events.ingest_events(conn, repo.path, transcripts=False,
                             progress=lambda d, t: calls["events"].append((d, t)))
        # A second run has nothing new to walk: it says so rather than counting history again.
        again: list[tuple[int, int]] = []
        symbols.ingest_symbols(conn, repo.path, progress=lambda d, t: again.append((d, t)))
    assert calls["git"][-1] == (7, 7)  # every reachable commit
    assert calls["symbols"][-1] == (6, 6)  # first-parent commits only
    assert calls["events"][-1] == (7, 7)
    for stage in calls.values():
        assert [done for done, _ in stage] == sorted(done for done, _ in stage)  # never goes backwards
        assert len({total for _, total in stage}) == 1  # the total is known up front
    assert again in ([], [(0, 0)])

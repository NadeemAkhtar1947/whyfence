import hashlib
import json
import os
from contextlib import closing
from pathlib import Path

import pytest
from test_symbols import Repo, needs_git

from whyfence import queries, store, team
from whyfence.cli import main
from whyfence.ingest import agent_logs, events, git, symbols
from whyfence.ingest.symbols import extract_symbols
from whyfence.store import Event, Symbol

AUTH = "def refresh(token):\n    audit.log('renew', token)\n    return client.renew(token)\n"


def names(path: str, code: str) -> list[tuple[str, str, int, int]]:
    return [(s.local_name, s.kind, s.start_line, s.end_line) for s in extract_symbols(path, code.encode())]


# --------------------------------------------------------------------------- #
# Languages
# --------------------------------------------------------------------------- #


def test_go_functions_structs_and_methods_by_receiver():
    code = (
        "package p\n"
        "type Server struct{ port int }\n"
        "type Handler interface{ Serve() }\n"
        "type Port int\n"
        "func New() *Server {\n"
        "  return nil\n"
        "}\n"
        "func (s *Server) Start() error { return nil }\n"
        "func (b Box[T]) Get() T { return b.v }\n"
    )
    assert names("pkg/server.go", code) == [
        ("Server", "class", 2, 2),
        ("Handler", "class", 3, 3),
        ("New", "function", 5, 7),
        ("Server.Start", "method", 8, 8),
        ("Box.Get", "method", 9, 9),
    ]


def test_rust_items_impl_scopes_and_modules():
    code = (
        "struct Pool { size: u32 }\n"
        "enum Mode { Fast }\n"
        "trait Run { fn required(&self); fn provided(&self) {} }\n"
        "impl<T> Pool<T> {\n"
        "    fn new() -> Self { todo!() }\n"
        "}\n"
        "impl fmt::Display for a::Pool { fn fmt(&self) {} }\n"
        "mod tests {\n"
        "    fn check() { fn helper() {} }\n"
        "}\n"
    )
    assert names("src/pool.rs", code) == [
        ("Pool", "class", 1, 1),
        ("Mode", "class", 2, 2),
        ("Run", "class", 3, 3),
        ("Run.provided", "method", 3, 3),
        ("Pool.new", "method", 5, 5),
        ("Pool.fmt", "method", 7, 7),
        ("tests.check", "function", 9, 9),
        ("tests.check.helper", "function", 9, 9),
    ]


def test_java_types_constructors_and_bodiless_methods():
    code = (
        "package a;\n"
        "public class Cache<T> {\n"
        "  Cache() {}\n"
        "  public int size() { return 1; }\n"
        "  static class Entry {}\n"
        "}\n"
        "interface Store { void put(); default void clear() {} }\n"
        "enum Mode { A; void flip() {} }\n"
        "record Point(int x) { Point { } }\n"
    )
    assert names("src/Cache.java", code) == [
        ("Cache", "class", 2, 6),
        ("Cache.Cache", "method", 3, 3),
        ("Cache.size", "method", 4, 4),
        ("Cache.Entry", "class", 5, 5),
        ("Store", "class", 7, 7),
        ("Store.clear", "method", 7, 7),
        ("Mode", "class", 8, 8),
        ("Mode.flip", "method", 8, 8),
        ("Point", "class", 9, 9),
        ("Point.Point", "method", 9, 9),
    ]


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Ada Lovelace")
    root = tmp_path / "repo"
    root.mkdir()
    created = Repo(root)
    created.git("config", "user.name", "Ada Lovelace")
    return created


@needs_git
def test_go_method_keeps_its_identity_through_a_rename(repo):
    body = "{\n  return s.port + 1\n}\n"
    c1 = repo.commit("add", {"server.go": f"package p\nfunc (s *Server) Port() int {body}"})
    c2 = repo.commit("rename", {"server.go": f"package p\nfunc (s *Server) NextPort() int {body}"})
    with closing(store.connect()) as conn:
        git.ingest_git(conn, repo.path)
        symbols.ingest_symbols(conn, repo.path)
        (symbol,) = store.find_symbols(conn, "NextPort")
        assert [(v.qualified_name, v.valid_from_sha) for v in store.symbol_history(conn, symbol.id)] == [
            ("server.Server.Port", c1), ("server.Server.NextPort", c2)]
        assert symbol.language == "go"


# --------------------------------------------------------------------------- #
# Rules
# --------------------------------------------------------------------------- #


def test_rules_lists_standing_constraints_once_newest_first():
    with closing(store.connect()) as conn:
        store.upsert_symbol(conn, Symbol("s1", "function", "app.send", "python"))
        from datetime import datetime, timedelta, timezone

        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        old = store.insert_event(conn, Event("constraint", "Never block the loop", recorded_at=t0))
        store.insert_event(conn, Event("constraint", "never  block the LOOP", recorded_at=t0 + timedelta(days=1)))
        store.insert_event(conn, Event("constraint", "Must use Decimal for money", recorded_at=t0 + timedelta(days=2)),
                           links=[("s1", "about")])
        replaced = store.insert_event(conn, Event("constraint", "Use floats", recorded_at=t0))
        store.supersede_event(conn, replaced.id, old.id)
        store.insert_event(conn, Event("decision", "Not a rule", recorded_at=t0))
        answer = queries.lookup_rules(conn)
    assert [r["rule"] for r in answer["rules"]] == ["Must use Decimal for money", "never  block the LOOP"]
    assert answer["rules"][0]["about"] == ["app.send"]


def test_rules_with_nothing_recorded_says_so():
    with closing(store.connect()) as conn:
        assert "No standing rules" in queries.lookup_rules(conn)["message"]


# --------------------------------------------------------------------------- #
# Agent logs
# --------------------------------------------------------------------------- #


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records) + "not json\n", encoding="utf-8")


def rollout(cwd: Path, *items: dict) -> list[dict]:
    header = {"timestamp": "2026-03-01T10:00:00Z", "type": "session_meta", "payload": {"id": "abc", "cwd": str(cwd)}}
    rows = [{"timestamp": f"2026-03-01T10:0{i}:00Z", "type": "response_item", "payload": item}
            for i, item in enumerate(items, 1)]
    return [header, {"timestamp": "x", "type": "event_msg", "payload": {"type": "user_message", "message": "No, dup"}},
            *rows]


def message(role: str, text: str, kind: str = "input_text") -> dict:
    return {"type": "message", "role": role, "content": [{"type": kind, "text": text}]}


def test_parse_codex_patch():
    patch = ("*** Begin Patch\n*** Update File: app/auth.py\n@@ def refresh\n-    old\n+    new one\n+    new two\n"
             " context\n+    later\n*** Add File: app/new.py\n+x = 1\n*** End Patch")
    edits, touched = agent_logs.parse_patch(patch)
    assert edits == [("app/auth.py", "    new one\n    new two"), ("app/auth.py", "    later")]
    assert touched == ["app/auth.py", "app/new.py"]


def test_read_codex_skips_injected_context_and_duplicate_event_stream(tmp_path):
    path = tmp_path / "rollout-1.jsonl"
    write_jsonl(path, rollout(
        tmp_path,
        message("developer", "No, system text"),
        message("user", "<environment_context>No, injected</environment_context>"),
        message("user", "# AGENTS.md instructions, never touch this"),
        message("user", "No, keep the cache."),
        {"type": "reasoning", "summary": [{"text": "I see the issue"}]},
        message("assistant", "Done.", "output_text"),
    ))
    assert [(t.role, t.text) for t in agent_logs.read_codex(path)] == [
        ("user", "No, keep the cache."), ("assistant", "Done.")]


@needs_git
def test_codex_session_answers_why(repo, tmp_path):
    repo.commit("add", {"app/auth.py": AUTH})
    home = tmp_path / "codex"
    patch = "*** Begin Patch\n*** Update File: app/auth.py\n+    audit.log('renew', token)\n*** End Patch"
    write_jsonl(home / "sessions" / "2026" / "03" / "01" / "rollout-a.jsonl", rollout(
        repo.path,
        message("user", "Add auditing."),
        message("assistant", "I see the issue now: renewals were never logged.", "output_text"),
        {"type": "custom_tool_call", "call_id": "1", "name": "apply_patch", "input": patch},
        message("user", "Never call refresh() from the request path."),
    ))
    write_jsonl(home / "sessions" / "2026" / "03" / "02" / "rollout-b.jsonl", rollout(
        tmp_path / "elsewhere", message("assistant", "ok", "output_text"), message("user", "No, never call refresh().")))
    with closing(store.connect()) as conn:
        git.ingest_git(conn, repo.path)
        symbols.ingest_symbols(conn, repo.path)
        result = events.ingest_events(conn, repo.path, codex_dir=home)
        assert (result.transcript_events, result.sessions) == (3, 1)
        answer = queries.lookup_why(conn, "refresh")
        assert sorted(e["kind"] for e in answer["events"]) == ["constraint", "correction", "dead_end"]
        assert {e["source"]["agent"] for e in answer["events"]} == {"codex"}
        assert events.ingest_events(conn, repo.path, codex_dir=home).transcript_events == 0


def gemini_document(project_hash: str, **extra) -> dict:
    return {
        "sessionId": "s1", "projectHash": project_hash, "startTime": "2026-03-01T10:00:00Z", **extra,
        "messages": [
            {"id": "m1", "timestamp": "2026-03-01T10:00:00Z", "type": "user", "content": [{"text": "Add auditing."}]},
            {"id": "m2", "timestamp": "2026-03-01T10:01:00Z", "type": "gemini", "content": "Turns out renewals were silent.",
             "thoughts": [{"subject": "x", "description": "I see the issue"}],
             "toolCalls": [{"id": "t", "name": "replace", "args": {
                 "file_path": "app/auth.py", "old_string": "x", "new_string": "    audit.log('renew', token)"}}]},
            {"id": "m3", "timestamp": "2026-03-01T10:02:00Z", "type": "info", "content": "No, ignored."},
            {"id": "m4", "timestamp": "2026-03-01T10:03:00Z", "type": "user", "content": "No, don't change refresh()."},
        ],
    }


@needs_git
@pytest.mark.parametrize("layout", ["json", "jsonl"])
def test_gemini_session_answers_why_in_both_layouts(repo, tmp_path, layout):
    repo.commit("add", {"app/auth.py": AUTH})
    root = git.repo_root(repo.path)
    project_hash = hashlib.sha256(str(root).encode()).hexdigest()
    home = tmp_path / "gemini"
    chats = home / "tmp" / project_hash / "chats"
    chats.mkdir(parents=True)
    document = gemini_document(project_hash)
    if layout == "json":
        (chats / "session-1.json").write_text(json.dumps(document), encoding="utf-8")
    else:
        lines = [{k: v for k, v in document.items() if k != "messages"}, *document["messages"]]
        (chats / "session-1.jsonl").write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")
    (chats / "subagent.json").write_text(json.dumps(gemini_document(project_hash, kind="subagent", sessionId="s2")),
                                         encoding="utf-8")
    other = home / "tmp" / ("0" * 64) / "chats"
    other.mkdir(parents=True)
    (other / "session-9.json").write_text(json.dumps(gemini_document("0" * 64, sessionId="s9")), encoding="utf-8")

    with closing(store.connect()) as conn:
        git.ingest_git(conn, repo.path)
        symbols.ingest_symbols(conn, repo.path)
        result = events.ingest_events(conn, repo.path, gemini_dir=home)
        assert result.sessions == 1
        answer = queries.lookup_why(conn, "refresh")
        assert sorted(e["kind"] for e in answer["events"]) == ["constraint", "correction", "dead_end"]
        assert {e["source"]["agent"] for e in answer["events"]} == {"gemini-cli"}


@needs_git
def test_gemini_tool_results_and_rewritten_messages_keep_the_edit_window(repo, tmp_path):
    # The shape of a real session: a message is written again once its tool calls are
    # known, and each tool result is filed as a user message with no text.
    repo.commit("add", {"app/auth.py": AUTH})
    root = git.repo_root(repo.path)
    project_hash = hashlib.sha256(str(root).encode()).hexdigest()
    at = "2026-03-01T10:00:00Z"
    edit = {"name": "replace", "args": {"file_path": "app/auth.py", "old_string": "x",
                                        "new_string": "    audit.log('renew', token)"}}
    rows = [
        {"sessionId": "s1", "projectHash": project_hash, "startTime": at, "lastUpdated": at, "kind": "main"},
        {"id": "u0", "timestamp": at, "type": "user", "content": [{"text": "<session_context> Never change this."}]},
        {"id": "u1", "timestamp": at, "type": "user", "content": [{"text": "Add auditing."}]},
        {"id": "g1", "timestamp": at, "type": "gemini", "content": ""},
        {"id": "g1", "timestamp": at, "type": "gemini", "content": "", "toolCalls": [edit]},
        {"$set": {"lastUpdated": at}},
        {"id": "u2", "timestamp": at, "type": "user", "content": [{"functionResponse": {"name": "replace"}}]},
        {"id": "g2", "timestamp": at, "type": "gemini", "content": "Added it."},
        {"id": "u3", "timestamp": at, "type": "user", "content": [{"text": "No, that is wrong."}]},
    ]
    chats = tmp_path / "gemini" / "tmp" / "demo" / "chats"
    chats.mkdir(parents=True)
    (chats / "session-1.jsonl").write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")

    with closing(store.connect()) as conn:
        git.ingest_git(conn, repo.path)
        symbols.ingest_symbols(conn, repo.path)
        events.ingest_events(conn, repo.path, gemini_dir=tmp_path / "gemini")
        # The message names nothing, so only the edit it reacts to can link it.
        assert [e["summary"] for e in queries.lookup_why(conn, "refresh")["events"]] == ["No, that is wrong."]
        assert queries.lookup_rules(conn)["rules"] == []  # the preamble is not the user speaking


def test_naming_one_log_location_reads_only_that_one(tmp_path, monkeypatch):
    home = tmp_path / "codex"
    monkeypatch.setattr(agent_logs, "codex_home", lambda: pytest.fail("read the default Codex home"))
    monkeypatch.setattr(agent_logs, "gemini_home", lambda: pytest.fail("read the default Gemini home"))
    assert list(events._sessions(tmp_path, tmp_path / "claude", None, None)) == []
    assert list(events._sessions(tmp_path, None, home, None)) == []


# --------------------------------------------------------------------------- #
# Team memory
# --------------------------------------------------------------------------- #


def indexed_session(repo: Repo, tmp_path: Path, conn) -> Path:
    """A repository whose local index holds events from one agent session."""
    repo.commit("add", {"app/auth.py": AUTH})
    repo.commit("fix: renew earlier\n\nWhy: tokens expired mid-request.", {"app/auth.py": AUTH + "\n# end\n"})
    home = tmp_path / "codex"
    write_jsonl(home / "sessions" / "rollout-a.jsonl", rollout(
        repo.path,
        message("assistant", "ok", "output_text"),
        message("user", "Never call refresh() from the request path.\n\nHere is my private context about the client."),
        message("user", "From now on, run the migrations first."),
        message("user", "No, that is wrong."),
    ))
    git.ingest_git(conn, repo.path)
    symbols.ingest_symbols(conn, repo.path)
    events.ingest_events(conn, repo.path, codex_dir=home)
    return git.repo_root(repo.path)


@needs_git
def test_share_writes_summaries_only_and_is_explicit(repo, tmp_path):
    with closing(store.connect()) as conn:
        root = indexed_session(repo, tmp_path, conn)
        assert not (root / team.EVENTS_DIR).exists()  # ingesting never publishes anything

        preview = team.share(conn, repo.path, dry_run=True)
        assert len(preview.events) == 3
        assert not (root / team.EVENTS_DIR).exists()

        result = team.share(conn, repo.path)
        assert result.path.name == "ada-lovelace.jsonl"
        text = result.path.read_text(encoding="utf-8")
        records = [json.loads(line) for line in text.splitlines()]
        assert sorted(r["kind"] for r in records) == ["constraint", "constraint", "correction"]
        assert "private context" not in text and "tokens expired" not in text  # no bodies, no commit events
        assert team.share(conn, repo.path).events == []  # nothing is written twice


@needs_git
def test_teammate_gets_shared_events_with_links(repo, tmp_path):
    with closing(store.connect()) as mine:
        root = indexed_session(repo, tmp_path, mine)
        team.share(mine, repo.path)
    with closing(store.connect()) as theirs:
        git.ingest_git(theirs, root)
        symbols.ingest_symbols(theirs, root)
        events.ingest_events(theirs, root, transcripts=False)
        assert team.import_shared(theirs, root) == 3
        assert team.import_shared(theirs, root) == 0
        answer = queries.lookup_why(theirs, "refresh")
        rule = next(e for e in answer["events"] if e["kind"] == "constraint")
        assert rule["summary"] == "Never call refresh() from the request path."
        assert rule["source"]["agent"] == "codex" and rule["source"]["uri"].startswith(".whyfence/events/")
        assert len(queries.lookup_rules(theirs)["rules"]) == 2


@needs_git
@pytest.mark.parametrize("bad", [
    {"id": "../../evil", "kind": "constraint", "summary": "x", "recorded_at": "2026-01-01T00:00:00Z"},
    {"id": "a" * 24, "kind": "made-up", "summary": "x", "recorded_at": "2026-01-01T00:00:00Z"},
    {"id": "a" * 24, "kind": "constraint", "summary": "", "recorded_at": "2026-01-01T00:00:00Z"},
    {"id": "a" * 24, "kind": "constraint", "summary": "x", "recorded_at": "not a date"},
    {"id": "a" * 24, "kind": "constraint", "summary": "x", "recorded_at": "2026-01-01T00:00:00"},
    ["not", "an", "object"],
])
def test_malformed_shared_records_are_skipped(repo, bad):
    repo.commit("add", {"app/auth.py": AUTH})
    repo.write({".whyfence/events/x.jsonl": json.dumps(bad) + "\nnot json\n"})
    with closing(store.connect()) as conn:
        git.ingest_git(conn, repo.path)
        assert team.import_shared(conn, repo.path) == 0


@needs_git
def test_shared_links_to_unknown_symbols_are_dropped_and_text_is_capped(repo):
    repo.commit("add", {"app/auth.py": AUTH})
    record = {"id": "b" * 24, "kind": "constraint", "summary": "x" * 900, "recorded_at": "2026-01-01T00:00:00Z",
              "links": [{"symbol": "nope", "relation": "about"}, {"symbol": "nope", "relation": "made-up"}, "junk"]}
    repo.write({".whyfence/events/x.jsonl": json.dumps(record) + "\n"})
    with closing(store.connect()) as conn:
        git.ingest_git(conn, repo.path)
        assert team.import_shared(conn, repo.path) == 1
        event = store.get_event(conn, "b" * 24)
        assert len(event.summary) == team.SUMMARY_LIMIT and store.event_links_of(conn, event.id) == []


@needs_git
def test_rules_and_share_commands(repo, tmp_path, capsys):
    db = tmp_path / "cli.db"
    home = tmp_path / "codex"
    repo.commit("add", {"app/auth.py": AUTH})
    write_jsonl(home / "sessions" / "rollout-a.jsonl", rollout(repo.path, message("user", "From now on, run migrations first.")))
    assert main(["ingest", str(repo.path), "--db", str(db), "--codex-dir", str(home)]) == 0
    capsys.readouterr()

    assert main(["rules", "--db", str(db)]) == 0
    assert "run migrations first" in capsys.readouterr().out
    assert main(["rules", "--db", str(db), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["rules"][0]["rule"].startswith("From now on")

    assert main(["share", str(repo.path), "--db", str(db), "--dry-run"]) == 0
    assert "Would add 1 event" in capsys.readouterr().out.replace("\n", " ")
    assert main(["share", str(repo.path), "--db", str(db)]) == 0
    assert main(["share", str(repo.path), "--db", str(tmp_path / "missing.db")]) == 2

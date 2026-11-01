import json
import os
import re
from contextlib import closing
from pathlib import Path

import pytest
from test_mcp_server import run_client
from test_symbols import Repo, needs_git

from whyfence import mcp_server, store
from whyfence.ingest import events, git, symbols
from whyfence.ingest.events import classify_commit, classify_turn, read_session

pytestmark = needs_git

AUTH_V1 = '''\
import time


def refresh(token, *, leeway=60):
    """Renew a token shortly before it expires."""
    if token.expires_in > leeway:
        return token
    return client.renew(token.refresh_token)


def logout(session):
    session.clear()
    return None
'''
AUTH_V2 = AUTH_V1.replace("leeway=60", "leeway=120").replace(
    "    return client.renew(token.refresh_token)\n",
    '    fresh = client.renew(token.refresh_token)\n    audit.log("refreshed", user=token.user)\n    return fresh\n',
)


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("message, expected", [
    ('Revert "Add retry loop"\n\nThis reverts commit 1234567abc.', ("revert", "EXTRACTED")),
    ("fix(auth): refresh before expiry", ("incident", "EXTRACTED")),
    ("Fix crash when the cache is empty", ("incident", "INFERRED")),
    ("Handle empty cache\n\nFixes #42", ("incident", "INFERRED")),
    ("Use Decimal for money\n\nWhy: float rounding broke invoice totals.", ("decision", "EXTRACTED")),
    ("Switch to WAL mode so that readers never block the ingestor", ("decision", "INFERRED")),
    ("Add README", None),
    ("Merge branch 'fix/crash'", None),
    ("docs: prefix with debugging tips", None),
    # Found on real history: "why" as a noun, and a feature commit that mentions a crash.
    ("feat: MCP server with why/history tools", None),
    ("feat: symbol indexing\n\nReading Node.start_point crashes on Windows.", None),
    ("Pin tree-sitter\n\nThat's why lines come from byte offsets.", ("decision", "INFERRED")),
])
def test_classify_commit(message, expected):
    finding = classify_commit(message)
    assert (finding.kind, finding.confidence) == expected if expected else finding is None


def turn(role: str, text: str) -> events._Turn:
    return events._Turn(role, "u", None, None, text)


@pytest.mark.parametrize("role, text, kind", [
    ("user", "No, don't retry inside refresh.", "correction"),
    ("user", "Actually, keep the old signature.", "correction"),
    ("user", "Looks good, but don't touch the migration.", "correction"),
    ("user", "That's not what I asked for.", "correction"),
    ("user", "Please add a test for logout.", None),
    ("user", "Nobody uses that flag anymore, delete it.", None),  # "No" only as a whole word
    ("assistant", "Reading the file. I see the issue now: the lock is taken twice.", "dead_end"),
    ("assistant", "Turns out the cache was never invalidated.", "dead_end"),
    ("assistant", "That approach won't work because the API is synchronous.", "dead_end"),
    ("assistant", "I'll add the test now.", None),
])
def test_classify_turn(role, text, kind):
    finding = classify_turn(turn(role, text))
    assert (finding.kind if finding else None) == kind


def test_corrections_need_context_and_short_messages():
    # A session's opening message reacts to nothing.
    assert classify_turn(turn("user", "No, start over with a new design."), after_assistant=False) is None
    # Long instructions often say "don't ..." without correcting anything.
    instructions = "Build the parser next. " * 20 + "Don't add new dependencies."
    assert classify_turn(turn("user", instructions)) is None
    assert classify_turn(turn("user", "Actually, " + instructions)).kind == "correction"
    # Mentioning a "dead end" isn't realizing one.
    assert classify_turn(turn("assistant", "The demo shows an agent avoiding a known dead end.")) is None


def test_injected_tags_are_dropped_and_wrappers_unwrapped():
    text = ('<pasted_content id="4cae">No, revert that.</pasted_content id="4cae">'
            "<system-reminder>internal</system-reminder>")
    assert events._strip_injected(text) == "No, revert that."


def test_broad_commits_link_only_the_symbols_they_name(repo_factory):
    repo = repo_factory()
    functions = "".join(f"def f{i}():\n    return {i}\n\n\n" for i in range(10))
    repo.commit("add", {"m.py": functions})
    repo.commit("fix: bump every constant; f3() was off by one", {"m.py": functions.replace("return", "return 1 +")})
    repo.commit("fix: bump them again", {"m.py": functions.replace("return", "return 2 +")})
    with closing(store.connect()) as conn:
        git.ingest_git(conn, repo.path)
        symbols.ingest_symbols(conn, repo.path)
        events.ingest_events(conn, repo.path, transcripts=False)
        linked = {e.summary: [store.get_symbol(conn, sid).qualified_name
                              for (sid,) in conn.execute("SELECT symbol_id FROM event_links WHERE event_id = ?",
                                                         (e.id,))]
                  for e in store.events_between(conn)}
        assert linked == {"fix: bump every constant; f3() was off by one": ["m.f3"], "fix: bump them again": []}


def test_realization_summary_is_the_matching_sentence():
    finding = classify_turn(turn("assistant", "Let me check the logs. I see the issue now: it races. Fixing it."))
    assert finding.summary == "I see the issue now: it races. Fixing it."


# --------------------------------------------------------------------------- #
# Transcripts
# --------------------------------------------------------------------------- #


def project_folder(projects: Path, cwd: Path) -> Path:
    # How Claude Code names a project's transcript folder.
    folder = projects / re.sub(r"[^A-Za-z0-9]", "-", str(cwd))
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def write_session(path: Path, records: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


def user(uuid: str, cwd: Path, at: str, content, **extra) -> dict:
    return {"type": "user", "uuid": uuid, "cwd": str(cwd), "timestamp": at, "sessionId": "s",
            "message": {"role": "user", "content": content}, **extra}


def assistant(uuid: str, cwd: Path, at: str, blocks: list[dict], **extra) -> dict:
    return {"type": "assistant", "uuid": uuid, "cwd": str(cwd), "timestamp": at, "sessionId": "s",
            "message": {"role": "assistant", "content": blocks}, **extra}


def edit(path: Path, new_string: str) -> dict:
    return {"type": "tool_use", "name": "Edit", "id": "t",
            "input": {"file_path": str(path), "old_string": "x", "new_string": new_string}}


def test_read_session_skips_tool_output_sidechains_thinking_and_injected_tags(tmp_path):
    session = tmp_path / "s.jsonl"
    write_session(session, [
        user("1", tmp_path, "2026-02-10T10:00:00Z",
             "<system-reminder>ignore me</system-reminder>No, wrong file."),
        user("2", tmp_path, "2026-02-10T10:00:01Z", [{"type": "tool_result", "content": "No, error"}]),
        user("3", tmp_path, "2026-02-10T10:00:02Z", "No, not that.", isSidechain=True),
        assistant("4", tmp_path, "2026-02-10T10:00:03Z", [
            {"type": "thinking", "thinking": "I see the issue"},
            {"type": "text", "text": "Done."},
        ]),
        {"type": "summary", "summary": "x"},
    ])
    session.write_text(session.read_text(encoding="utf-8") + "not json\n", encoding="utf-8")
    turns = list(read_session(session))
    assert [(t.role, t.text) for t in turns] == [("user", "No, wrong file."), ("assistant", "Done.")]


def test_transcript_folders_for_repo_its_children_and_parents(tmp_path):
    # The encoding is lossy ("my_app_v2" and "my_app/v2" collide), so matching folders are only
    # candidates; events are kept only if their session ran in, or touched, the repository.
    repo = tmp_path / "work" / "my_app"
    for cwd in (repo, repo / "web", tmp_path / "work", tmp_path / "work" / "my_application", tmp_path / "other"):
        (project_folder(tmp_path / "projects", cwd) / "s.jsonl").write_text("", encoding="utf-8")
    found = {p.parent.name for p in events.transcript_files(tmp_path / "projects", repo)}
    encode = lambda p: re.sub(r"[^A-Za-z0-9]", "-", str(p))  # noqa: E731
    assert found == {encode(repo), encode(repo / "web"), encode(tmp_path / "work")}


def test_secrets_are_redacted():
    assert events._redact("key sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123 here") == "key [redacted] here"
    assert events._redact("token ghp_" + "a" * 36) == "token [redacted]"


# --------------------------------------------------------------------------- #
# End to end
# --------------------------------------------------------------------------- #


@pytest.fixture
def repo_factory(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")

    def make(name: str = "repo") -> Repo:
        root = tmp_path / name
        root.mkdir()
        return Repo(root)

    return make


@pytest.fixture
def scenario(tmp_path, repo_factory):
    repo = repo_factory()
    root = repo.path
    repo.commit("add auth", {"app/auth.py": AUTH_V1})
    fix = repo.commit("fix: refresh tokens before they expire\n\n"
                      "Why: refreshing on 401 caused bursts of failed requests.", {"app/auth.py": AUTH_V2})
    repo.commit("docs: add readme", {"README.md": "# demo\n"})

    auth = root / "app" / "auth.py"
    projects = tmp_path / "projects"
    write_session(project_folder(projects, root) / "session-1.jsonl", [
        user("u1", root, "2026-02-10T10:00:00Z", "Make token refresh more robust"),
        assistant("a1", root, "2026-02-10T10:01:00Z", [
            {"type": "text", "text": "I'll add retries inside refresh."},
            edit(auth, "    for attempt in range(3):\n        retry()"),
        ]),
        user("u2", root, "2026-02-10T10:02:00Z", [{"type": "tool_result", "content": "ok"}]),
        user("u3", root, "2026-02-10T10:03:00Z",
             [{"type": "text", "text": "No, don't retry inside refresh() — the HTTP client already retries."}]),
        assistant("a2", root, "2026-02-10T10:05:00Z", [
            {"type": "text", "text": "I see the issue now: the 401 handler and refresh both renewed the token, "
                                     "so requests raced. I'll log renewals instead."},
            edit(auth, '    audit.log("refreshed", user=token.user)'),
        ]),
        user("u4", root, "2026-02-10T10:06:00Z", "Thanks, that works."),
        assistant("a3", root, "2026-02-10T10:07:00Z", [{"type": "text", "text": "My mistake earlier."}],
                  isSidechain=True),
    ])
    # A session started in the parent folder that names a file inside the repo.
    write_session(project_folder(projects, tmp_path) / "session-2.jsonl", [
        assistant("p0", tmp_path, "2026-02-11T08:59:00Z", [{"type": "text", "text": "I'll tidy the auth module."}]),
        user("p1", tmp_path, "2026-02-11T09:00:00Z", f"Don't touch `logout` in {auth}, sessions depend on it."),
        user("p2", tmp_path, "2026-02-11T09:01:00Z", "No, not that folder."),  # nothing to link: skipped
    ])
    # Another project entirely.
    write_session(project_folder(projects, tmp_path / "elsewhere") / "session-3.jsonl", [
        user("x1", tmp_path / "elsewhere", "2026-02-12T09:00:00Z", "No, don't touch refresh()."),
    ])
    return root, projects, fix


def ingest_all(conn, root: Path, projects: Path) -> events.EventIngestResult:
    git.ingest_git(conn, root)
    symbols.ingest_symbols(conn, root)
    return events.ingest_events(conn, root, projects_dir=projects)


def test_why_returns_commit_and_transcript_events_for_a_symbol(scenario, tmp_path):
    root, projects, fix = scenario
    with closing(store.connect(tmp_path / "index.db")) as conn:
        result = ingest_all(conn, root, projects)
        assert (result.commit_events, result.transcript_events, result.sessions) == (1, 3, 2)

        answer = mcp_server.lookup_why(conn, "app.auth.refresh")
        assert answer["status"] == "ok"
        assert [(e["kind"], e["summary"]) for e in answer["events"]] == [
            ("dead_end", "I see the issue now: the 401 handler and refresh both renewed the token, "
                         "so requests raced. I'll log renewals instead."),
            ("correction", "No, don't retry inside refresh() — the HTTP client already retries."),
            ("incident", "fix: refresh tokens before they expire"),
        ]
        dead_end, correction, incident = answer["events"]
        assert incident["commit"] == fix and incident["confidence"] == "EXTRACTED"
        assert "refreshing on 401 caused bursts" in incident["detail"]
        assert incident["source"] == {"kind": "commit", "agent": None, "uri": fix}
        assert correction["source"]["kind"] == "transcript" and correction["source"]["agent"] == "claude-code"
        assert correction["source"]["uri"].endswith("session-1.jsonl")
        assert correction["recorded_at"] == "2026-02-10T10:03:00Z"

        logout = mcp_server.lookup_why(conn, "logout")
        assert [(e["kind"], e["source"]["uri"].endswith("session-2.jsonl")) for e in logout["events"]] == [
            ("correction", True)]

        # Nothing from the unrelated project, and re-running adds nothing.
        assert all("elsewhere" not in e.body for e in store.events_between(conn))
        again = events.ingest_events(conn, root, projects_dir=projects)
        assert (again.commit_events, again.transcript_events, again.links) == (0, 0, 0)

    # The real MCP server, over stdio, answers the same question.
    _, (why,) = run_client(tmp_path / "index.db", [("why", {"symbol_name": "refresh"})])
    assert not why.is_error
    assert [e["kind"] for e in why.structured_content["events"]] == ["dead_end", "correction", "incident"]


def test_transcripts_can_be_disabled(scenario, tmp_path):
    root, projects, _ = scenario
    with closing(store.connect()) as conn:
        git.ingest_git(conn, root)
        symbols.ingest_symbols(conn, root)
        result = events.ingest_events(conn, root, projects_dir=projects, transcripts=False)
        assert (result.commit_events, result.transcript_events) == (1, 0)

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
from whyfence.store import Event, Source
from whyfence.trust import Trust

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
    # Found on httpx: squash-merge bodies list every commit on the branch.
    ('Remove unused ignore\n\n* Revert "x"\n\nThis reverts commit 1234567abc.', None),
    ("Rename concurrency to backends\n\n* fix tests\n* a bug in the old name", None),
    ("Add CLI\n\n* keep it small because it is new", None),
    ("Fix typo in docstring", None),
    ("fix: spelling in the README", None),
    ("Use ruff instead of flake8", None),
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


@pytest.mark.parametrize("text, rule", [
    ("Build the UI. Don't touch the billing module, finance audits it.", "Don't touch the billing module, finance audits it."),
    ("Must use Decimal for money.", "Must use Decimal for money."),
    ("Remember to run migrations before tests.", "Remember to run migrations before tests."),
    ("From now on, put new endpoints under /v2.", "From now on, put new endpoints under /v2."),
    ("Uploads must stay synchronous; the client polls.", "Uploads must stay synchronous; the client polls."),
    ("Never call refresh() from the event loop.", "Never call refresh() from the event loop."),
    ("Should I always use the cache?", None),            # a question isn't a rule
    ("Never mind, it works.", None),
    ("Add a test for logout.", None),
])
def test_find_constraint(text, rule):
    assert events.find_constraint(text) == rule


@pytest.mark.parametrize("text, decision", [
    ("- The file model is `TaskFile`, not `File`, because `File` would clash with `fastapi.File`.",
     "The file model is `TaskFile`, not `File`, because `File` would clash with `fastapi.File`."),
    ("I used `Decimal` instead of `float` to avoid rounding errors in totals.",
     "I used `Decimal` instead of `float` to avoid rounding errors in totals."),
    ("We went with `httpx` because it supports async.", "We went with `httpx` because it supports async."),
    ("I renamed `File` because of a clash.", None),                     # a reason, but no choice
    ("The model is `TaskFile`, not `File`.", None),                     # a choice, but no reason
    ("I chose this approach instead of a queue because it's simpler.", None),  # no code named
])
def test_find_decision(text, decision):
    assert events.find_decision(text) == decision


@pytest.mark.parametrize("text, kind", [
    ("Confirming the real bug is genuinely fixed.", None),
    ("The real bug is that the proxy is lazy.", "dead_end"),
])
def test_real_bug_needs_a_realization(text, kind):
    finding = classify_turn(turn("assistant", text))
    assert (finding.kind if finding else None) == kind


def test_compaction_summaries_are_not_user_turns(tmp_path):
    session = tmp_path / "s.jsonl"
    write_session(session, [
        user("1", tmp_path, "2026-02-10T10:00:00Z", "This session is being continued... must use Decimal.",
             isCompactSummary=True, isVisibleInTranscriptOnly=True),
        user("2", tmp_path, "2026-02-10T10:00:01Z", "Must use Decimal."),
    ])
    assert [t.uuid for t in read_session(session)] == ["2"]


def test_changelog_bullets_are_not_constraints(repo_factory):
    repo = repo_factory()
    repo.commit("add", {"m.py": "def send():\n    return 1\n"})
    repo.commit("Tidy send\n\n* Don't call send on each request", {"m.py": "def send():\n    return 2\n"})
    with closing(store.connect()) as conn:
        git.ingest_git(conn, repo.path)
        symbols.ingest_symbols(conn, repo.path)
        events.ingest_events(conn, repo.path, transcripts=False)
        assert store.events_between(conn, kinds=["constraint"]) == []


def test_commit_constraints_are_separate_events(repo_factory):
    repo = repo_factory()
    repo.commit("add", {"m.py": "def send():\n    return 1\n"})
    repo.commit("fix: send retries\n\nNever call send() from the event loop; it blocks.",
                {"m.py": "def send():\n    return 2\n"})
    with closing(store.connect()) as conn:
        git.ingest_git(conn, repo.path)
        symbols.ingest_symbols(conn, repo.path)
        events.ingest_events(conn, repo.path, transcripts=False)
        ada = Trust(frozenset({"ada@example.com"}))
        assert sorted((e["kind"], e["summary"]) for e in mcp_server.lookup_why(conn, "send", ada)["events"]) == [
            ("constraint", "Never call send() from the event loop; it blocks."),
            ("incident", "fix: send retries"),
        ]
        strangers = mcp_server.lookup_why(conn, "send")  # nobody trusted: the rule is held back, the fix isn't
        assert [e["kind"] for e in strangers["events"]] == ["incident"] and strangers["untrusted_hidden"] == 1


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
        assert (result.commit_events, result.transcript_events, result.sessions) == (1, 4, 2)

        answer = mcp_server.lookup_why(conn, "app.auth.refresh", Trust(frozenset({"ada@example.com"})))
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

        # "Don't touch `logout` ..." is both a correction (it pushes back) and a standing rule.
        logout = mcp_server.lookup_why(conn, "logout")
        assert sorted((e["kind"], e["source"]["uri"].endswith("session-2.jsonl")) for e in logout["events"]) == [
            ("constraint", True), ("correction", True)]
        rule = next(e for e in logout["events"] if e["kind"] == "constraint")
        assert rule["summary"].startswith("Don't touch `logout`")

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


def test_why_and_history_commands(scenario, tmp_path, capsys):
    from whyfence.cli import main

    root, projects, fix = scenario
    db = tmp_path / "cli.db"
    assert main(["ingest", str(root), "--db", str(db), "--transcripts", str(projects)]) == 0
    capsys.readouterr()

    assert main(["why", "refresh", "--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "app.auth.refresh" in out and "dead_end" in out and "commit " + fix[:7] in out

    assert main(["why", "refresh", "--db", str(db), "--json"]) == 0
    with closing(store.connect(db)) as conn:
        assert json.loads(capsys.readouterr().out) == mcp_server.lookup_why(conn, "refresh")

    assert main(["history", "app.auth.refresh", "--db", str(db)]) == 0
    assert "[current]" in capsys.readouterr().out

    assert main(["why", "nonexistent", "--db", str(db)]) == 1
    assert main(["history", "x", "--db", str(tmp_path / "missing.db")]) == 2


def test_cli_output_survives_narrow_encodings(scenario, tmp_path, monkeypatch):
    """Windows reports NUL as a terminal and encodes it as cp1252; output must not crash."""
    import io
    import sys

    from whyfence.cli import main

    root, projects, _ = scenario
    db = tmp_path / "narrow.db"
    for argv in (["ingest", str(root), "--db", str(db), "--transcripts", str(projects)],
                 ["why", "refresh", "--db", str(db)],
                 ["history", "refresh", "--db", str(db)]):
        narrow = io.TextIOWrapper(io.BytesIO(), encoding="ascii")
        monkeypatch.setattr(sys, "stdout", narrow)
        assert main(argv) == 0
        narrow.flush()
        assert narrow.buffer.getvalue()  # something was written, with unencodable characters replaced


# --------------------------------------------------------------------------- #
# Commit bodies wrapped at 72 columns
# --------------------------------------------------------------------------- #

WRAPPED = (
    "Fix bytecount on streamed bodies\n"
    "\n"
    "Going forward, bytecount must stay a plain attribute because the C\n"
    "extension reads it directly and a property would break it.\n"
    "\n"
    "- remove the property and its setter, which callers\n"
    "  never used outside the tests\n"
    "\n"
    "Co-Authored-By: Someone <s@example.com>\n"
)


def test_wrapped_commit_sentences_are_kept_whole():
    _, _, body = WRAPPED.partition("\n")
    assert events.find_constraint(events._prose(body)) == (
        "Going forward, bytecount must stay a plain attribute because the C extension reads it "
        "directly and a property would break it.")


def test_commit_prose_drops_wrapped_bullets_and_trailers_but_keeps_paragraphs():
    _, _, body = WRAPPED.partition("\n")
    prose = events._prose(body)
    assert "callers" not in prose and "never used" not in prose  # the bullet, including its wrapped line
    assert "Co-Authored-By" not in prose
    assert prose.count("\n") == 0  # one paragraph left, on one line
    two = events._prose("The first paragraph is long enough that it had to wrap at the\nedge.\n\n"
                        "And the second one is also long enough to wrap onto a second\nline.")
    assert two == ("The first paragraph is long enough that it had to wrap at the edge.\n"
                   "And the second one is also long enough to wrap onto a second line.")  # paragraphs stay apart


def test_transcript_sentences_still_split_on_newlines():
    # In a chat, a newline usually does end a thought; keep it that way for sessions.
    assert events.find_constraint("never call refresh() here\nthe client already retries") == \
        "never call refresh() here"


def test_short_lines_are_a_list_not_a_wrapped_sentence():
    # flask 66b1b752: one item per line, no bullet markers. Joining them made a run-on "rule".
    body = ("single default handler and formatter\ndon't remove handlers\n"
            "configure level once using setLevel\ndocument logging\nreorganize logging tests\n")
    assert events.find_constraint(events._prose(body)) == "don't remove handlers"


def test_releasing_an_obligation_is_not_a_rule():
    # flask 60a11a73: "so we don't have to keep them in sync" says the opposite of a rule.
    for text in ("Remove the per-project files so we don't have to keep them in sync.",
                 "With the cache, callers do not have to call refresh() any more.",
                 "It doesn't have to run the migrations first."):
        assert events.find_constraint(text) is None, text
    assert events.find_constraint("You have to keep the lock while writing.") is not None


# --------------------------------------------------------------------------- #
# Tooling fixes are not incidents (subjects from flask, click, requests and httpx)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("subject", [
    "Fixed formatting",
    "Fix formatting issues",
    "Fix style issues on master using Black",
    "fix flake8 bugbear errors",
    "fix pep8 with logging.py",
    "Fix linting error.",
    "Fixed some lint warnings",
    "Fix Ruff UP038 warning",
    "fix mypy finding",
    "fix pyright type errors",
    "fix type hint for `cli_runner.invoke`",
    "fix annotation for g object",
    "Fix type annotation for `before_request` and `before_app_request` decorators",
    "Fixed indentation of an example.",
    "Fixed trailing whitespace",
    'Fix tox warning, test "not installed in testenv".',
    "Fix pre-commit cache key in GitHub Actions. (#3740)",
    "Fix ci environment command syntax (#3836)",
    "Fixed incorrect version definitions in travis",
    "Fixed link for good",
    "Fixed homepage links",
    "Fixed docstring.  This fixes #45",
    "Fix inconsistent capitalisation and punctuation in attribute docstrings",
    "Fixed a comment",
    "Fix the tests and unseparate comments from code",
    "Fixed a whole bunch of resource warnings in the flask testsuite",
    "fix(ci): pin the runner image",
    "fix(tests): flaky timeout",
])
def test_tooling_fixes_are_not_incidents(subject):
    assert classify_commit(subject) is None, subject


@pytest.mark.parametrize("subject", [
    "Fixed an incorrectly formatted help text for count options.",  # user-facing output
    "Fix string format error while we're in here.",
    "Fix number formatting in progress bar",
    "Fix ResourceWarning in send_file",  # a resource leak
    "Fix URL encoding of query params",  # URLs are requests' domain
    "Fix links header parsing",  # Response.links
    "use setup.py test. This fixes #56",
    "fix: refresh tokens before they expire",
])
def test_code_fixes_that_mention_those_words_stay_incidents(subject):
    finding = classify_commit(subject)
    assert finding is not None and finding.kind == "incident", subject


@pytest.mark.parametrize("message, summary", [
    ("Use Decimal for money\n\nWhy: float rounding broke invoice totals at month end, by a cent\nor two per line.",
     "Use Decimal for money — float rounding broke invoice totals at month end, by a cent or two per line."),
    ("Keep sessions in Redis\n\nThe in-process store looked simpler. We chose Redis because\n"
     "workers restart on every deploy and users were logged out.",
     "Keep sessions in Redis — We chose Redis because workers restart on every deploy and users were logged out."),
    ("Switch to WAL mode so that readers never block the ingestor", "Switch to WAL mode so that readers never block "
                                                                    "the ingestor"),  # the subject is the reason
])
def test_a_decision_is_summarised_with_its_reason(message, summary):
    from datetime import datetime, timezone

    from whyfence.store import Commit

    commit = Commit("a" * 40, "Ada <ada@example.com>", datetime(2026, 1, 1, tzinfo=timezone.utc), message)
    finding = classify_commit(message)
    assert finding is not None and finding.kind == "decision"
    assert events._commit_event(commit, finding).summary == summary


def test_a_quoted_error_message_is_not_a_rule():
    # psf/requests 2bb49ff3: the "must be" is inside the error being reported.
    text = ("If encoding is None, decoding will throw the following TypeError: TypeError: unicode() "
            "argument 2 must be string, not None")
    assert events.find_constraint(text) is None
    assert events.find_constraint("Callers must pass a str; a TypeError is raised otherwise.") is not None


# --------------------------------------------------------------------------- #
# 0.4.1: patterns from the 80-row development set (eval/blind-80-*.csv)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("message", [
    "Revert \"Revert \"Merge Stable into master\"\"",            # click e2524373 (dev #48)
    "Revert \"Revert \"Merge Stable into master\"\" (#3008)",    # click a8b41c07 (dev #18)
    "Revert \"Revert \"Merge pull request #859 from wvh/register_error_handler\"\"",
])
def test_a_revert_of_a_revert_of_a_merge_is_housekeeping(message):
    assert classify_commit(message) is None


def test_other_reverts_still_count():
    assert classify_commit('Revert "Use shlex.quote for quoting shell arguments"').kind == "revert"
    assert classify_commit('Revert "Merge pull request #1710 from jvantuyl/master"').kind == "revert"
    assert classify_commit('Revert "Revert "Make safe_mode play nice with timeout (fixes #312)""').kind == "revert"


@pytest.mark.parametrize("message", [
    "Added support for tuple types.  This fixes #239",                                    # dev #71
    "Add @template_test() decorator for creating custom jinja2 tests, like existing "
    "@template_filter() for filters.  Fixes #332",                                        # dev #66
    "Added the JSONIFY_PRETTYPRINT_REGULAR config variable.  This fixes #725",
    "Support Python 3.13\n\nCloses #2701",
    "Adds a --quiet flag\n\nFixes #12",
])
def test_an_issue_reference_on_an_addition_is_not_an_incident(message):
    assert classify_commit(message) is None


@pytest.mark.parametrize("message", [
    "Added a workaround for a pypy bug in context managers",   # dev #77: says "bug"
    "Add skip to fix unnoticed bug with good imports",          # dev #78
    "Fix bad import.  This fixes #454",
    "Handle empty cache\n\nFixes #42",
])
def test_fixes_that_reference_issues_still_count(message):
    finding = classify_commit(message)
    assert finding is not None and finding.kind == "incident", message


def test_rules_are_not_mined_from_code_blocks_or_tracebacks():
    fenced = ("Better error message when view return type is not supported\n\nBefore, returning a `bool` "
              "from a route caused the error\n\n```\nTraceback (most recent call last):\n  File \"app.py\", "
              "line 2070, in make_response\nTypeError: The view function did not return a valid response. "
              "The return type must be a string, tuple, Response instance, or WSGI callable, but it was a bool.\n```\n")
    indented = ("Fixed #37222 -- Fixed QuerySet.distinct() crash on duplicated selections.\n\nOrdering referred "
                "to each selection by its own position, so DISTINCT ON could not be\nmatched against:\n\n"
                "    SELECT DISTINCT ON expressions must match initial ORDER BY\n    expressions\n\n"
                "Ordering now refers to the first position an expression is selected at.\n")
    raw = ("Fix crash\n\nTraceback (most recent call last):\n  File \"x.py\", line 1, in <module>\n"
           "ValueError: the value must be positive\n\nNever call connect() with a negative timeout.\n")
    for message in (fenced, indented):
        _, _, body = message.partition("\n")
        assert events.find_constraint(events._prose(body)) is None
    _, _, body = raw.partition("\n")
    assert events.find_constraint(events._prose(body)) == "Never call connect() with a negative timeout."


@pytest.mark.parametrize("fragment", [
    "must be importable for URL reversing to work correctly.",     # django fa63f1642d38 (dev #61)
    "has to be defined before the first request.",
    "should always be called with the lock held.",
])
def test_a_rule_with_no_subject_is_dropped(fragment):
    assert events.find_constraint(fragment) is None


@pytest.mark.parametrize("rule", [
    "Never call deploy() from a request handler.",
    "always pass InternalServerError instance to 500 handler",            # an imperative: implied subject
    "--defaults-file must be given before other options.",                 # dev #65
    "The namespace of the constraint must be included when making the constraint immediate.",
])
def test_rules_with_a_subject_or_imperative_are_kept(rule):
    assert events.find_constraint(rule) == rule


@pytest.mark.parametrize("path, code", [
    ("docs/api.rst", False), ("README.md", False), ("CHANGES.rst", False), ("CHANGELOG", False),
    ("src/pkg/README.md", False), ("examples/tutorial/app.py", False), ("tests/test_app.py", False),
    ("test/helpers.py", False), ("pkg/foo_test.go", False), ("web/app.spec.ts", False),
    ("src/__tests__/a.js", False), ("conftest.py", False), (".github/workflows/ci.yml", False),
    ("tox.ini", False), (".travis.yml", False), ("noxfile.py", False), ("artwork/logo.png", False),
    ("docs/_static/flask.svg", False),
    ("flask/app.py", True), ("django/test/testcases.py", True),   # Django's test framework is product code
    ("src/click/testing.py", True), ("requirements.txt", True), ("setup.py", True), ("Makefile", True),
])
def test_paths_that_cannot_change_behaviour(path, code):
    assert events.is_code_path(path) is code, path


def test_events_from_commits_touching_only_docs_tests_or_ci_are_skipped(repo_factory):
    repo = repo_factory()
    repo.commit("Add app", {"app/core.py": "def run():\n    return 1\n", "docs/index.rst": "Docs\n",
                            "tests/test_core.py": "def test_run():\n    assert True\n"})
    repo.commit("Fix crash in the docs build", {"docs/index.rst": "Docs, fixed\n"})
    repo.commit("Fix flaky timeout handling", {"tests/test_core.py": "def test_run():\n    assert 1\n"})
    repo.commit("fix: run() returned the wrong value\n\nWhy: callers expect 2.",
                {"app/core.py": "def run():\n    return 2\n", "tests/test_core.py": "def test_run():\n    assert 2\n"})
    repo.commit("Clarify that run() must be called once\n\nNever call run() twice in one process.",
                {"docs/index.rst": "Docs: never call run() twice\n"})
    repo.git("mv", "docs/index.rst", "app/index_doc.py")
    repo.commit("fix: move the help page into the package")
    with closing(store.connect()) as conn:
        git.ingest_git(conn, repo.path)
        symbols.ingest_symbols(conn, repo.path)
        events.ingest_events(conn, repo.path, transcripts=False)
        found = sorted(e.summary for e in store.events_between(conn))
    assert found == ["fix: move the help page into the package", "fix: run() returned the wrong value"]


def test_events_mined_by_an_older_whyfence_are_re_mined_without_rebuilding_symbols(repo_factory):
    repo = repo_factory()
    repo.commit("Add app", {"app/core.py": "def run():\n    return 1\n", "docs/index.rst": "Docs\n"})
    repo.commit("Fix crash in the docs build", {"docs/index.rst": "Docs, fixed\n"})
    with closing(store.connect()) as conn:
        git.ingest_git(conn, repo.path)
        symbols.ingest_symbols(conn, repo.path)
        # What 0.4.0 left behind: the docs-only "incident", mined before the new rules existed.
        store.insert_source(conn, Source("commit:x", "commit"))
        sha = repo.git("rev-parse", "HEAD")
        store.insert_event(conn, Event("incident", "Fix crash in the docs build", source_id="commit:x",
                                       commit_sha=sha, confidence="INFERRED"))
        store.set_meta(conn, events.MINING_KEY, "0.4.0")
        versions_before = conn.execute("SELECT count(*) FROM symbol_versions").fetchone()[0]
        events.ingest_events(conn, repo.path, transcripts=False)
        assert [e.summary for e in store.events_between(conn)] == []
        assert store.get_meta(conn, events.MINING_KEY) == events.MINING_VERSION
        assert conn.execute("SELECT count(*) FROM symbol_versions").fetchone()[0] == versions_before


def test_abbreviations_do_not_end_a_sentence():
    # django 00dca6f0: "..., e.g. two volatile extra() selections must keep ordering ..." was cut at "e.g."
    text = ("Raw selections are left out as equal SQL is not necessarily interchangeable, e.g. two volatile "
            "extra() selections must keep ordering by their own position.")
    assert events._sentences(text) == [text]
    for abbreviation in ("i.e.", "etc.", "vs.", "cf."):
        sentence = f"Callers {abbreviation} the request path must use the pool."
        assert events._sentences(sentence) == [sentence], abbreviation
    assert events._sentences("One. Two.") == ["One.", "Two."]

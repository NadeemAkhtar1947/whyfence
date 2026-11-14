import json
import os
import subprocess
import sys
from contextlib import closing

import pytest
from test_symbols import Repo, needs_git, repo  # noqa: F401  (fixture)

from whyfence import check, store, team
from whyfence.cli import main
from whyfence.ingest import events, git, symbols

pytestmark = needs_git

AUTH = '''\
def refresh(token, *, leeway=60):
    """Renew a token shortly before it expires."""
    if token.expires_in > leeway:
        return token
    return client.renew(token.refresh_token)


def logout(session):
    session.clear()
    return None
'''


def ingest(target: Repo, conn) -> None:
    git.ingest_git(conn, target.path)
    symbols.ingest_symbols(conn, target.path)
    events.ingest_events(conn, target.path, transcripts=False)
    team.import_shared(conn, target.path)


def shared(event_id: str, summary: str, symbol: str) -> str:
    return json.dumps({"id": event_id, "kind": "constraint", "summary": summary,
                       "recorded_at": "2026-02-03T00:00:00Z", "agent": "claude-code",
                       "links": [{"symbol": symbol, "relation": "about"}]}) + "\n"


def symbol_id(target: Repo, name: str) -> str:
    with closing(store.connect()) as conn:
        ingest(target, conn)
        [match] = store.find_symbols(conn, name)
        return match.id


@pytest.fixture
def history(repo: Repo) -> Repo:  # noqa: F811
    repo.commit("Add token refresh", {"app/auth.py": AUTH})
    repo.commit("fix: refresh tokens before they expire\n\nWhy: refreshing after a 401 caused bursts of failures.",
                {"app/auth.py": AUTH.replace("leeway=60", "leeway=120")})
    repo.git("checkout", "-q", "-b", "feature")
    return repo


def test_branch_that_moves_and_edits_a_fenced_function(history):
    moved = AUTH.replace("leeway=60", "leeway=120").replace("return token\n", "return token  # fresh\n")
    history.write({"app/auth.py": None})
    history.commit("Move token code\n\nWhy: auth.py mixed sessions and tokens.", {"app/tokens.py": moved})
    with closing(store.connect()) as conn:
        ingest(history, conn)
        result = check.check(conn, history.path, "main")
    assert result.changed == 2  # refresh and logout both moved
    [fence] = result.fences  # logout has no history
    assert (fence.name, fence.new_name, fence.deleted) == ("app.auth.refresh", "app.tokens.refresh", False)
    assert [e["kind"] for e in fence.events] == ["incident"]
    assert fence.events[0]["summary"].startswith("fix: refresh tokens")  # the branch's own "Why:" isn't listed
    assert fence.events[0]["detail"] == "Why: refreshing after a 401 caused bursts of failures."
    assert "<br>Why: refreshing after a 401" in check.to_markdown(result)


def test_github_merge_commit_and_deleted_symbol(history):
    history.commit("Drop refresh", {"app/auth.py": "def logout(session):\n    session.clear()\n    return None\n"})
    history.git("checkout", "-q", "main")
    history.git("merge", "-q", "--no-ff", "-m", "Merge pull request #7", "feature")
    with closing(store.connect()) as conn:
        ingest(history, conn)
        result = check.check(conn, history.path, "HEAD^1")
    [fence] = result.fences
    assert fence.name == "app.auth.refresh" and fence.deleted


def test_only_shared_events_from_the_base_count(history):
    refresh = symbol_id(history, "refresh")
    rule = shared("a" * 24, "Never retry inside refresh().", refresh)
    history.commit("Share a rule", {".whyfence/events/ada.jsonl": rule})
    history.git("checkout", "-q", "-B", "main")  # the rule is now on main
    history.git("checkout", "-q", "-b", "pr")
    history.commit("Edit refresh and add my own note", {
        "app/auth.py": AUTH.replace("leeway=60", "leeway=90"),
        ".whyfence/events/ada.jsonl": rule + shared("b" * 24, "refresh() must stay fast.", refresh),
    })
    with closing(store.connect()) as conn:
        ingest(history, conn)
        [fence] = check.check(conn, history.path, "main").fences
    summaries = [e["summary"] for e in fence.events]
    assert summaries[0] == "Never retry inside refresh()."  # rules come first
    assert "refresh() must stay fast." not in summaries


def test_unchanged_branch_and_stale_index(history):
    with closing(store.connect()) as conn:
        ingest(history, conn)
        assert check.check(conn, history.path, "main").fences == []
        history.commit("Edit", {"app/auth.py": AUTH})
        with pytest.raises(check.CheckError, match="run `whyfence ingest`"):
            check.check(conn, history.path, "main")
        with pytest.raises(check.CheckError, match="not a commit"):
            check.check(conn, history.path, "no-such-branch")


def test_markdown_is_inert():
    fence = check.Fence("id", "app.auth.refresh", None, False, "app/a`b.py", [{
        "kind": "constraint", "summary": "Ping @octocat <img src=x> [click](https://evil.example) **now**",
        "recorded_at": "2026-02-03T00:00:00Z", "commit": None, "source": "transcript", "agent": "claude-code",
        "confidence": "EXTRACTED"}])
    text = check.to_markdown(check.CheckResult("b", "h", 1, [fence]))
    assert text.startswith(check.MARKER)
    assert "@​octocat" in text and r"\<img" in text and r"\[click\]\(https" in text
    assert r"\*\*now\*\*" in text
    assert "`app/a'b.py`" in text
    assert "**rule:**" in text and "claude-code session" in text
    empty = check.to_markdown(check.CheckResult("b", "h", 1, []))
    assert "1 function or class that already existed" in empty


def test_markdown_output_is_utf8_whatever_the_console_encoding(repo):  # noqa: F811
    repo.commit("Add token refresh", {"app/auth.py": AUTH})
    repo.commit("fix: renew earlier\n\nWhy: the 東京 proxy drops idle tokens, ask @ops.",
                {"app/auth.py": AUTH.replace("leeway=60", "leeway=120")})
    repo.git("checkout", "-q", "-b", "feature")
    repo.commit("Edit refresh", {"app/auth.py": AUTH.replace("leeway=60", "leeway=90")})
    assert main(["ingest", str(repo.path), "--no-transcripts"]) == 0
    env = {**os.environ, "PYTHONIOENCODING": "cp1252"}  # what Windows uses for redirected output
    out = subprocess.run([sys.executable, "-m", "whyfence", "check", "main", "--repo", str(repo.path),
                          "--format", "markdown"], capture_output=True, env=env, check=True).stdout
    text = out.decode("utf-8")
    assert "東京" in text and "@\N{ZERO WIDTH SPACE}ops" in text


def test_inferred_fixes_are_weak_signals_and_bullets_are_dropped(repo):  # noqa: F811
    repo.commit("Add token refresh", {"app/auth.py": AUTH})
    repo.commit("Fix crash in refresh (#12)\n\n* try one\n* try two",
                {"app/auth.py": AUTH.replace("leeway=60", "leeway=120")})
    repo.git("checkout", "-q", "-b", "feature")
    repo.commit("Edit refresh", {"app/auth.py": AUTH.replace("leeway=60", "leeway=90")})
    with closing(store.connect()) as conn:
        ingest(repo, conn)
        assert check.check(conn, repo.path, "main").fences == []
        [fence] = check.check(conn, repo.path, "main", include_weak=True).fences
    assert fence.events[0]["confidence"] == "INFERRED" and fence.events[0]["detail"] is None


def test_cli_formats_and_fail_on(history, capsys):
    history.commit("Edit refresh", {"app/auth.py": AUTH.replace("leeway=60", "leeway=90")})
    assert main(["ingest", str(history.path), "--no-transcripts"]) == 0
    capsys.readouterr()
    assert main(["check", "main", "--repo", str(history.path), "--format", "json"]) == 0
    answer = json.loads(capsys.readouterr().out)
    assert answer["fences"][0]["symbol"] == "app.auth.refresh"
    assert main(["check", "main", "--repo", str(history.path), "--fail-on", "constraint"]) == 0
    assert main(["check", "main", "--repo", str(history.path), "--fail-on", "incident"]) == 1
    assert main(["check", "main", "--repo", str(history.path), "--format", "markdown"]) == 0
    assert capsys.readouterr().out.count(check.MARKER) == 1

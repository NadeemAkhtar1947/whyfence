import os
import shutil
import subprocess
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import pytest

from whyfence import store
from whyfence.cli import main
from whyfence.ingest import git

PROJECT_ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


@pytest.fixture
def isolated_git(monkeypatch):
    """Keep the user's global git config (signing, hooks, templates) out of test repos."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def run_git(repo: Path, *args: str, date: str | None = None) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "Ada Lovelace", "GIT_AUTHOR_EMAIL": "ada@example.com",
           "GIT_COMMITTER_NAME": "Ada Lovelace", "GIT_COMMITTER_EMAIL": "ada@example.com"}
    if date:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = date
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, encoding="utf-8", env=env).stdout.strip()


def make_repo(path: Path) -> dict[str, str]:
    """A small history with a branch merge, a multi-line non-ASCII message and odd separators."""
    run_git(path, "init", "-q", "-b", "main")
    shas = {}

    def commit(name: str, message: str, date: str) -> None:
        (path / f"{name}.txt").write_text(name, encoding="utf-8")
        run_git(path, "add", ".")
        run_git(path, "commit", "-q", "-m", message, date=date)
        shas[name] = run_git(path, "rev-parse", "HEAD")

    commit("base", "Add base", "2026-01-01T10:00:00+00:00")
    run_git(path, "checkout", "-q", "-b", "feature")
    commit("feature", "Retry with backoff\n\nWhy: the upstream API rate-limits.\nNot a lock — tried that.",
           "2026-01-02T15:30:00+05:30")
    run_git(path, "checkout", "-q", "main")
    commit("hotfix", "Fix: 修正 unit\x1fseparator in message", "2026-01-03T09:00:00-08:00")
    run_git(path, "merge", "-q", "--no-ff", "feature", "-m", "Merge feature", date="2026-01-04T12:00:00+00:00")
    shas["merge"] = run_git(path, "rev-parse", "HEAD")
    return shas


def test_ingests_project_own_history(tmp_path):
    """The ingestor reads this repository's real history, end to end through the CLI."""
    if not git.has_commits(PROJECT_ROOT):
        pytest.skip("not running from a git checkout with commits")
    expected = int(subprocess.run(["git", "-C", str(PROJECT_ROOT), "rev-list", "--count", "HEAD"],
                                  check=True, capture_output=True, text=True).stdout)
    head = subprocess.run(["git", "-C", str(PROJECT_ROOT), "rev-parse", "HEAD"],
                          check=True, capture_output=True, text=True).stdout.strip()
    db = tmp_path / "index.db"

    assert main(["ingest", str(PROJECT_ROOT), "--db", str(db)]) == 0
    assert main(["ingest", str(PROJECT_ROOT), "--db", str(db)]) == 0  # idempotent re-run

    with closing(store.connect(db)) as conn:
        assert store.count_commits(conn) == expected
        assert store.get_commit(conn, head) is not None
        commits = store.commits_between(conn, newest_first=False)
        first = commits[0]
        assert first.parents == ()
        assert first.subject.startswith("Initial commit")
        assert all(c.authored_at.tzinfo is not None for c in commits)
        assert all(len(c.sha) == 40 for c in commits)


def test_parses_merges_offsets_and_unusual_messages(tmp_path, isolated_git):
    shas = make_repo(tmp_path)
    with closing(store.connect()) as conn:
        result = git.ingest_git(conn, tmp_path)
        assert result == git.IngestResult(seen=4, inserted=4)
        assert git.ingest_git(conn, tmp_path) == git.IngestResult(seen=4, inserted=0)

        merge = store.get_commit(conn, shas["merge"])
        assert merge.parents == (shas["hotfix"], shas["feature"])
        assert merge.author == "Ada Lovelace <ada@example.com>"

        feature = store.get_commit(conn, shas["feature"])
        assert feature.message == "Retry with backoff\n\nWhy: the upstream API rate-limits.\nNot a lock — tried that."
        assert feature.authored_at == datetime(2026, 1, 2, 10, 0, tzinfo=timezone.utc)  # +05:30 normalized

        hotfix = store.get_commit(conn, shas["hotfix"])
        assert hotfix.subject == "Fix: 修正 unit\x1fseparator in message"

        january_2_to_4 = store.commits_between(conn, datetime(2026, 1, 2, tzinfo=timezone.utc),
                                               datetime(2026, 1, 4, tzinfo=timezone.utc))
        assert [c.sha for c in january_2_to_4] == [shas["hotfix"], shas["feature"]]


def test_stops_early_without_error(tmp_path, isolated_git):
    make_repo(tmp_path)
    commits = git.iter_commits(tmp_path)
    assert next(commits).subject == "Merge feature"
    commits.close()  # closing mid-stream must not hang or raise
    assert [c.subject for c in git.iter_commits(tmp_path, max_count=2)] == ["Merge feature", "Fix: 修正 unit\x1fseparator in message"]


def test_empty_repository_yields_nothing(tmp_path, isolated_git):
    run_git(tmp_path, "init", "-q")
    with closing(store.connect()) as conn:
        assert git.ingest_git(conn, tmp_path) == git.IngestResult(0, 0)


def test_outside_a_repository_is_an_error(tmp_path, isolated_git, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    with pytest.raises(git.GitError):
        list(git.iter_commits(tmp_path))
    assert main(["ingest", str(tmp_path), "--db", str(tmp_path / "x.db")]) == 2

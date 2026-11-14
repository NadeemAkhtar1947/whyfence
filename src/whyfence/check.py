"""Which recorded fences does a change touch? Backs `whyfence check` and the GitHub Action.

A change is every first-parent commit in `BASE..HEAD`: the commits of a feature
branch, or the single merge commit GitHub checks out for a pull request. The
symbols those commits edited, renamed, moved or deleted come from the history
walk, so a function that was moved and edited in the same change still finds
the decisions recorded against it.

Only what was known before the change counts: events from the change's own
commits, and shared events the change itself adds, are left out.
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from whyfence import store, team
from whyfence.store import Event
from whyfence.ingest import git, symbols

MARKER = "<!-- whyfence-check -->"  # lets the Action find and update its own comment
MAX_SYMBOLS = 20
MAX_EVENTS = 5
DETAIL_LIMIT = 240  # characters of an event's body shown under its summary
_KIND_ORDER = {"constraint": 0, "dead_end": 1, "correction": 2, "revert": 3, "incident": 4, "decision": 5}
_KIND_LABELS = {"constraint": "rule", "dead_end": "dead end", "correction": "correction", "revert": "revert",
                "incident": "incident", "decision": "decision"}


class CheckError(RuntimeError):
    pass


@dataclass
class Fence:
    """A changed symbol with something recorded about why it is the way it is."""

    symbol_id: str
    name: str  # as it was before the change
    new_name: str | None  # set when the change renamed or moved it
    deleted: bool
    file: str
    events: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class CheckResult:
    base: str
    head: str
    changed: int  # symbols the change touched that existed before it
    fences: list[Fence]

    def to_json(self) -> dict[str, Any]:
        return {
            "base": self.base, "head": self.head, "changed_symbols": self.changed,
            "fences": [{"symbol": f.name, "renamed_to": f.new_name, "deleted": f.deleted, "file": f.file,
                        "events": f.events} for f in self.fences],
        }


def _run(repo: Path, *args: str) -> str:
    result = subprocess.run(git._git(repo, *args), capture_output=True, text=True, encoding="utf-8",
                            errors="replace")
    if result.returncode != 0:
        raise CheckError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout


def _shared_ids_at(repo: Path, rev: str) -> set[str]:
    """Ids of the shared events committed at `rev`."""
    ids: set[str] = set()
    for path in _run(repo, "ls-tree", "-r", "--name-only", rev, "--", team.EVENTS_DIR.as_posix()).splitlines():
        if not path.endswith(".jsonl"):
            continue
        for line in _run(repo, "show", f"{rev}:{path}").splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict) and isinstance(record.get("id"), str):
                ids.add(record["id"])
    return ids


STRONG_KINDS = frozenset({"constraint", "dead_end", "correction", "revert"})


def is_fence(event: Event) -> bool:
    """Worth interrupting a review for: a rule, a dead end, a correction, a revert, or an explicit why.

    A commit subject that merely starts with "Fix" says a bug was fixed, not that the code
    must stay this way; on a busy class nearly every PR would trip over a few of those.
    """
    return event.kind in STRONG_KINDS or event.confidence == "EXTRACTED"


_BULLET = re.compile(r"^\s*([*+-]|\d+[.)])\s")


def _excerpt(body: str) -> str | None:
    # Squash merges list every commit on the branch as bullets; that's noise, not a reason.
    text = " ".join(" ".join(line for line in body.splitlines() if not _BULLET.match(line)).split())
    if not text:
        return None
    return text if len(text) <= DETAIL_LIMIT else text[:DETAIL_LIMIT].rsplit(" ", 1)[0] + "…"


def check(conn: sqlite3.Connection, repo: Path | str, base: str, *, include_weak: bool = False) -> CheckResult:
    root = git.repo_root(repo)
    if _run(root, "rev-parse", "--is-shallow-repository").strip() == "true":
        raise CheckError("this is a shallow clone, so the history whyfence needs is missing; "
                         "fetch it with `git fetch --unshallow` (in GitHub Actions: fetch-depth: 0)")
    head = _run(root, "rev-parse", "--verify", "HEAD^{commit}").strip()
    try:
        base_sha = _run(root, "rev-parse", "--verify", f"{base}^{{commit}}").strip()
    except CheckError:
        raise CheckError(f"{base!r} is not a commit in this repository") from None
    if store.get_meta(conn, symbols.INDEXED_THROUGH) != head:
        raise CheckError("the index is not up to date with HEAD; run `whyfence ingest` first")

    walked = _run(root, "rev-list", "--first-parent", f"{base_sha}..{head}").split()
    in_change = set(_run(root, "rev-list", f"{base_sha}..{head}").split())
    known_shared = _shared_ids_at(root, base_sha)

    touched: list[str] = sorted({sid for sha in walked for sid in store.symbols_changed_in(conn, sha)})
    walked_set = set(walked)
    fences: list[Fence] = []
    changed = 0
    for symbol_id in touched:
        versions = store.symbol_history(conn, symbol_id)
        before = [v for v in versions if v.valid_from_sha not in walked_set]
        if not before:
            continue  # added by the change: nothing can have been recorded about it yet
        changed += 1
        found = []
        for event in store.events_for_symbol(conn, symbol_id):
            if event.commit_sha in in_change or not (include_weak or is_fence(event)):
                continue
            source = store.get_source(conn, event.source_id) if event.source_id else None
            if event.source_id and event.source_id.startswith("shared:") and event.id not in known_shared:
                continue
            found.append({
                "kind": event.kind,
                "summary": event.summary,
                "detail": _excerpt(event.body),
                "recorded_at": store.to_timestamp(event.recorded_at),
                "commit": event.commit_sha,
                "source": source.kind if source else None,
                "agent": source.agent if source else None,
                "confidence": event.confidence,
            })
        if not found:
            continue
        found.sort(key=lambda e: _KIND_ORDER.get(e["kind"], 9))
        old, last = before[-1], versions[-1]
        old_name = old.qualified_name or symbol_id
        deleted = last.valid_to_sha in walked_set
        new_name = None if deleted else (last.qualified_name or old_name)
        fences.append(Fence(symbol_id, old_name, new_name if new_name != old_name else None, deleted,
                            old.file_path, found))
    fences.sort(key=lambda f: (min(_KIND_ORDER.get(e["kind"], 9) for e in f.events), -len(f.events), f.name))
    return CheckResult(base_sha, head, changed, fences)


# --------------------------------------------------------------------------- #
# Markdown for a pull request comment
# --------------------------------------------------------------------------- #

_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]<>()#+!|~])")


def _md(text: str) -> str:
    """Repository text as inert Markdown: no HTML, links, formatting or @-mentions."""
    text = " ".join(text.split())
    text = text.replace("&", "&amp;")
    text = _MD_SPECIAL.sub(r"\\\1", text)
    return text.replace("@", "@\N{ZERO WIDTH SPACE}")  # stops GitHub notifying anyone


def _code(text: str) -> str:
    """Repository text inside a code span; a backtick in it would end the span early."""
    return " ".join(text.replace("`", "'").split())


def _plural_things(count: int) -> str:
    return f"{count} function{'s' * (count != 1)} or class{'es' * (count != 1)}"


def _origin(event: dict[str, Any]) -> str:
    if event["commit"]:
        origin = f"commit {event['commit'][:7]}"
    elif event["source"] == "transcript":
        origin = f"{event['agent'] or 'agent'} session"
    else:
        origin = event["source"] or "recorded"
    if event["confidence"] == "INFERRED":
        origin += ", inferred"
    return f"{event['recorded_at'][:10]}, {_md(origin)}"


def to_markdown(result: CheckResult) -> str:
    lines = [MARKER, "### whyfence"]
    if not result.fences:
        lines.append(f"This change touches {_plural_things(result.changed)} that already existed. "
                     "None has a recorded rule, dead end, revert or stated reason.")
        return "\n".join(lines) + "\n"
    count = len(result.fences)
    lines.append(f"This change touches {_plural_things(count)} with recorded history. "
                 "Worth a look before merging:")
    lines.append("")
    for fence in result.fences[:MAX_SYMBOLS]:
        what = "deleted" if fence.deleted else f"renamed to `{_code(fence.new_name)}`" if fence.new_name else "changed"
        lines.append(f"**`{_code(fence.name)}`** ({what}, `{_code(fence.file)}`)")
        for event in fence.events[:MAX_EVENTS]:
            label = _KIND_LABELS.get(event["kind"], event["kind"])
            line = f"- **{label}:** {_md(event['summary'])} <sub>{_origin(event)}</sub>"
            if event.get("detail"):
                line += f"<br>{_md(event['detail'])}"
            lines.append(line)
        if len(fence.events) > MAX_EVENTS:
            lines.append(f"- and {len(fence.events) - MAX_EVENTS} more: `whyfence why {_code(fence.name)}`")
        lines.append("")
    if count > MAX_SYMBOLS:
        lines.append(f"And {count - MAX_SYMBOLS} more. Run `whyfence check` locally for the full list.")
        lines.append("")
    lines.append("<sub>Found by [whyfence](https://github.com/NadeemAkhtar1947/whyfence) "
                 "from commit messages and the team's shared notes in `.whyfence/events/`.</sub>")
    return "\n".join(lines) + "\n"

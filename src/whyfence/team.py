"""Team memory: share what agents learned through the repository itself.

`whyfence share` appends events from your agent sessions to
`.whyfence/events/<you>.jsonl`, one file per person so merges never conflict.
You review and commit it like any other file. `whyfence ingest` then loads
everyone's files into each person's local index.

Sharing is explicit, and it publishes as little as it can: one short summary
per event, never the session text. Commit-based events aren't exported, because
every teammate derives those from git. Symbol ids are computed from git history,
so they agree across machines without coordination.

Shared files come from the repository, so what they contain is treated as
untrusted input: anything malformed is skipped.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from whyfence import store
from whyfence.ingest import git
from whyfence.ingest.events import _sentences
from whyfence.store import Event, Source

EVENTS_DIR = Path(".whyfence") / "events"
SUMMARY_LIMIT = 300
_ID_RE = re.compile(r"^[0-9a-f]{8,64}$")


@dataclass(frozen=True)
class ShareResult:
    path: Path
    events: list[Event]


def first_sentence(text: str) -> str:
    return next(iter(_sentences(text)), text)


def _author_slug(repo: Path) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (git.config_value(repo, "user.name") or "").lower()).strip("-")
    return slug or "local"


def _shared_files(repo: Path) -> list[Path]:
    folder = repo / EVENTS_DIR
    return sorted(folder.glob("*.jsonl")) if folder.is_dir() else []


def _records(path: Path) -> Iterator[dict]:
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                yield record


def _worth_sharing(conn: sqlite3.Connection, event: Event) -> bool:
    source = store.get_source(conn, event.source_id) if event.source_id else None
    if source is None or source.kind != "transcript" or event.superseded_by:
        return False
    # An unlinked correction says nothing a teammate can act on; a project-wide rule does.
    return event.kind == "constraint" or bool(store.event_links_of(conn, event.id))


def share(conn: sqlite3.Connection, repo: Path, *, dry_run: bool = False) -> ShareResult:
    """Append events not yet in any shared file to this author's file."""
    root = git.repo_root(repo)
    already = {record.get("id") for path in _shared_files(root) for record in _records(path)}
    events = [e for e in reversed(store.events_between(conn)) if e.id not in already and _worth_sharing(conn, e)]
    target = root / EVENTS_DIR / f"{_author_slug(root)}.jsonl"
    if events and not dry_run:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8", newline="\n") as handle:
            for event in events:
                source = store.get_source(conn, event.source_id)
                handle.write(json.dumps({
                    "v": 1,
                    "id": event.id,
                    "kind": event.kind,
                    # One sentence: summaries can run on into whatever was said next.
                    "summary": first_sentence(event.summary),
                    "confidence": event.confidence,
                    "recorded_at": store.to_timestamp(event.recorded_at),
                    "agent": source.agent if source else None,
                    "links": [{"symbol": sid, "relation": rel} for sid, rel in store.event_links_of(conn, event.id)],
                }, ensure_ascii=False, sort_keys=True) + "\n")
    return ShareResult(target, events)


def _valid_event(record: dict, source_id: str) -> Event | None:
    event_id, kind, summary = record.get("id"), record.get("kind"), record.get("summary")
    if not (isinstance(event_id, str) and _ID_RE.match(event_id)) or kind not in store.EVENT_KINDS:
        return None
    if not isinstance(summary, str) or not summary.strip():
        return None
    recorded_at = record.get("recorded_at")
    try:
        when = store.from_timestamp(recorded_at) if isinstance(recorded_at, str) else None
    except ValueError:
        return None
    if when is None or when.tzinfo is None:
        return None
    confidence = record.get("confidence") if record.get("confidence") in store.CONFIDENCE else "INFERRED"
    return Event(kind=kind, summary=summary[:SUMMARY_LIMIT], source_id=source_id, confidence=confidence,
                 recorded_at=when, id=event_id)


def import_shared(conn: sqlite3.Connection, repo: Path) -> int:
    """Load teammates' shared events into the local index. Returns how many were new."""
    root = git.repo_root(repo)
    added = 0
    for path in _shared_files(root):
        rel = path.relative_to(root).as_posix()
        for record in _records(path):
            agent = record.get("agent") if isinstance(record.get("agent"), str) else None
            source_id = f"shared:{path.name}:{agent}"
            event = _valid_event(record, source_id)
            if event is None or store.get_event(conn, event.id) is not None:
                continue
            links = []
            for link in record.get("links") or []:
                if not isinstance(link, dict) or link.get("relation") not in store.LINK_RELATIONS:
                    continue
                symbol = link.get("symbol")
                # Symbols this checkout hasn't indexed (another branch, say) are left out.
                if isinstance(symbol, str) and store.get_symbol(conn, symbol) is not None:
                    links.append((symbol, link["relation"]))
            with store.batch(conn):
                store.insert_source(conn, Source(source_id, "transcript", agent=agent, uri=rel,
                                                 captured_at=event.recorded_at))
                store.insert_event(conn, event, links)
            added += 1
    return added

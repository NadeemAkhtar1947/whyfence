"""Read coding-agent session logs into a common shape.

Every agent's log becomes a list of `Turn`s, so finding corrections, dead ends
and decisions, and linking them to symbols, works the same for all of them.

The Codex CLI and Gemini CLI formats are undocumented and change between
releases, so these readers skip anything they don't recognize rather than
guessing. The Gemini reader has been checked against a real session; the Codex
reader follows what open-source parsers of that format rely on and hasn't been.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

_INJECTED_CODEX_PREFIXES = ("<environment_context>", "<user_instructions>", "# AGENTS.md")
_PATCH_MARKER = "*** Begin Patch"
_GEMINI_PATH_KEYS = ("file_path", "absolute_path", "path")


@dataclass
class Turn:
    """One user or assistant message in a session."""

    role: str
    uuid: str
    cwd: str | None
    timestamp: datetime | None
    text: str
    edits: list[tuple[str, str]] = field(default_factory=list)  # (path as written, new text)
    touched: list[str] = field(default_factory=list)  # paths read or edited


@dataclass
class AgentSession:
    agent: str
    session_id: str
    uri: str
    turns: list[Turn]


def parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _path_parts(path: str | Path) -> list[str]:
    parts = list(Path(os.path.realpath(path)).parts)
    return [p.lower() for p in parts] if os.name == "nt" else parts


def _related(cwd: str, repo: Path) -> bool:
    """Whether a session ran in the repository, inside it, or in a folder containing it."""
    a, b = _path_parts(cwd), _path_parts(repo)
    shared = min(len(a), len(b))
    return a[:shared] == b[:shared]


def _text_items(content: object) -> Iterable[str]:
    if isinstance(content, str):
        yield content
    elif isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                yield item["text"]


# --------------------------------------------------------------------------- #
# Codex CLI: <home>/sessions/YYYY/MM/DD/rollout-*.jsonl
# --------------------------------------------------------------------------- #


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def _json_lines(path: Path) -> Iterator[tuple[int, dict]]:
    with path.open(encoding="utf-8", errors="replace") as handle:
        for number, line in enumerate(handle):
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                yield number, record


def _patch_text(payload: dict) -> str | None:
    """The apply_patch body of a tool call, wherever this Codex version put it."""
    raw = payload.get("input") or payload.get("arguments")
    if isinstance(raw, str) and raw.lstrip().startswith("{"):
        try:
            raw = json.loads(raw)
        except ValueError:
            pass
    if isinstance(raw, dict):
        raw = raw.get("input") or raw.get("command")
    if isinstance(raw, list):
        raw = "\n".join(part for part in raw if isinstance(part, str))
    return raw if isinstance(raw, str) and _PATCH_MARKER in raw else None


def parse_patch(patch: str) -> tuple[list[tuple[str, str]], list[str]]:
    """(edits, touched paths) from Codex's apply_patch format."""
    edits: list[tuple[str, str]] = []
    touched: list[str] = []
    current: str | None = None
    hunk: list[str] = []

    def flush() -> None:
        if current and hunk:
            edits.append((current, "\n".join(hunk)))
        hunk.clear()

    for line in patch.splitlines():
        for marker in ("*** Update File: ", "*** Add File: ", "*** Delete File: ", "*** Move to: "):
            if line.startswith(marker):
                flush()
                path = line[len(marker):].strip()
                touched.append(path)
                # New files are whole-file writes, which would point at every function in them.
                current = path if marker in ("*** Update File: ", "*** Move to: ") else None
                break
        else:
            if line.startswith("+"):
                hunk.append(line[1:])
            else:
                flush()
    flush()
    return edits, touched


def read_codex(path: Path) -> Iterator[Turn]:
    cwd: str | None = None
    for number, record in _json_lines(path):
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        if record.get("type") == "session_meta":
            cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else cwd
            continue
        if record.get("type") != "response_item":
            continue  # event_msg and turn_context repeat what response_item already holds
        timestamp = parse_timestamp(record.get("timestamp"))
        if payload.get("type") == "message" and payload.get("role") in ("user", "assistant"):
            texts = list(_text_items(payload.get("content")))
            if payload["role"] == "user" and any(t.lstrip().startswith(_INJECTED_CODEX_PREFIXES) for t in texts):
                continue
            yield Turn(payload["role"], str(number), cwd, timestamp, "\n".join(texts).strip())
        elif payload.get("type") in ("function_call", "custom_tool_call"):
            patch = _patch_text(payload)
            if patch:
                edits, touched = parse_patch(patch)
                yield Turn("assistant", str(number), cwd, timestamp, "", edits, touched)


def _codex_meta(path: Path) -> dict:
    for _, record in _json_lines(path):
        if record.get("type") == "session_meta" and isinstance(record.get("payload"), dict):
            return record["payload"]
        break  # the header is always the first record
    return {}


def codex_sessions(home: Path, repo: Path) -> Iterator[AgentSession]:
    files = [*sorted((home / "sessions").rglob("rollout-*.jsonl")),
             *sorted((home / "archived_sessions").glob("rollout-*.jsonl"))]
    for path in files:
        meta = _codex_meta(path)
        cwd = meta.get("cwd")
        if isinstance(cwd, str) and _related(cwd, repo):
            yield AgentSession("codex", f"codex:{meta.get('id') or path.stem}", str(path), list(read_codex(path)))


# --------------------------------------------------------------------------- #
# Gemini CLI: <home>/tmp/<project>/chats/... as one JSON document or JSONL
# --------------------------------------------------------------------------- #


def gemini_home() -> Path:
    return Path(os.environ.get("GEMINI_CLI_HOME") or Path.home()) / ".gemini"


def _project_hashes(repo: Path) -> set[str]:
    # Gemini CLI identifies a project by the SHA-256 of its absolute path.
    forms = {str(repo), os.path.realpath(repo), repo.as_posix()}
    return {hashlib.sha256(form.encode()).hexdigest() for form in forms}


def _load_gemini(path: Path) -> tuple[dict, list[dict]]:
    """(session metadata, messages) from either layout. Unknown shapes yield no messages."""
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    try:
        document = json.loads(text)
    except ValueError:
        document = None
    if isinstance(document, dict):
        messages = document.get("messages")
        return document, [m for m in messages if isinstance(m, dict)] if isinstance(messages, list) else []
    if isinstance(document, list):
        return {}, [m for m in document if isinstance(m, dict)]
    meta: dict = {}
    messages = []
    for _, record in _json_lines(path):
        if record.get("type") in ("user", "gemini"):
            messages.append(record)
        elif "sessionId" in record:
            meta = {**meta, **record}
    return meta, messages


def read_gemini_messages(messages: Iterable[dict], cwd: str | None) -> Iterator[Turn]:
    # JSONL logs write a message again once its tool calls are known: keep the last
    # version, in the place the message first appeared.
    latest: dict[str, dict] = {}
    for number, message in enumerate(messages):
        latest[str(message.get("id") or number)] = message
    for message_id, message in latest.items():
        role = {"user": "user", "gemini": "assistant"}.get(message.get("type"))
        if role is None:
            continue
        edits: list[tuple[str, str]] = []
        touched: list[str] = []
        for call in message.get("toolCalls") or []:
            args = call.get("args") if isinstance(call, dict) else None
            if not isinstance(args, dict):
                continue
            file_path = next((args[k] for k in _GEMINI_PATH_KEYS if isinstance(args.get(k), str)), None)
            if file_path is None:
                continue
            touched.append(file_path)
            if call.get("name") == "replace" and isinstance(args.get("new_string"), str):
                edits.append((file_path, args["new_string"]))
        text = "\n".join(_text_items(message.get("content"))).strip()
        if role == "user" and (not text or text.startswith("<session_context>")):
            continue  # tool results and the CLI's own preamble are filed as user messages
        yield Turn(role, message_id, cwd, parse_timestamp(message.get("timestamp")), text, edits, touched)


def gemini_sessions(home: Path, repo: Path) -> Iterator[AgentSession]:
    tmp = home / "tmp"
    if not tmp.is_dir():
        return
    hashes = _project_hashes(repo)
    for folder in sorted(p for p in tmp.iterdir() if p.is_dir()):
        chats = folder / "chats"
        if not chats.is_dir():
            continue
        for path in sorted(p for p in chats.rglob("*") if p.suffix in (".json", ".jsonl") and p.is_file()):
            meta, messages = _load_gemini(path)
            if meta.get("kind") == "subagent" or (folder.name not in hashes and meta.get("projectHash") not in hashes):
                continue
            session_id = meta.get("sessionId") or path.stem
            yield AgentSession("gemini-cli", f"gemini:{session_id}", str(path),
                               list(read_gemini_messages(messages, str(repo))))

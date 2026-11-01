"""Mine "why" events from commit messages and Claude Code transcripts, and link them to symbols.

Commit messages give reverts, fixes and stated rationale. Transcripts give the
two things git never records: a user correcting the agent ("No, don't retry
there") and the agent realizing an approach was wrong ("I see the issue now").

Links are what make an event answer `why(symbol)`:

- A commit event links to the symbols that commit changed (from the symbol index).
- A transcript event links through the files the agent touched around it. Each
  edit's new text is located in the file at HEAD and re-parsed, so it pins
  down the functions it changed. Symbol names mentioned in the text
  (`refresh()`) link too, when they resolve unambiguously.

Everything is idempotent: event ids are derived from their source, so re-running
adds only what is new.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterable, Iterator

from whyfence import store
from whyfence.ingest import git
from whyfence.ingest.symbols import extract_symbols, language_for, module_name
from whyfence.store import Commit, Event, Source

SUMMARY_LIMIT = 300
BODY_LIMIT = 2000
# An event touching more symbols than this is a broad change (a feature commit, a
# refactor), not a statement about any one of them: it keeps only the symbols it names.
MAX_LINKS = 8
CORRECTION_ANYWHERE_LIMIT = 300  # long messages are instructions; only short ones correct mid-sentence

# --------------------------------------------------------------------------- #
# Commit messages
# --------------------------------------------------------------------------- #

_REVERT_RE = re.compile(r'^Revert "|^revert(?:\([^)]*\))?!?:|^This reverts commit [0-9a-f]{7,40}', re.I | re.M)
_CONVENTIONAL_RE = re.compile(r"^([A-Za-z]+)(?:\([^)]*\))?!?:")
_FIX_TYPES = {"fix", "bugfix", "hotfix"}
_FIX_RE = re.compile(
    r"^fix(?:e[sd])?\b|\b(?:fix(?:es|ed)?|resolves?|closes?)\s+#\d+|\bbugs?\b|\bregression\b|\bcrash(?:es|ed|ing)?\b",
    re.I | re.M,
)
_RATIONALE_HEADER_RE = re.compile(r"^\s*(?:why|rationale|reason|motivation)\s*:", re.I | re.M)
# "why" alone is usually a noun ("the why tool"); only rationale phrasing counts.
_RATIONALE_RE = re.compile(
    r"\b(?:that'?s|this is|which is|here'?s) why\b|\bbecause\b|\bso that\b|\binstead of\b|\bin order to\b|"
    r"\bwe (?:chose|decided|opted)\b|\bdecided to\b",
    re.I,
)


@dataclass(frozen=True)
class CommitFinding:
    kind: str  # "revert", "incident" or "decision"
    confidence: str


def classify_commit(message: str) -> CommitFinding | None:
    """What a commit message says about why code changed, if anything."""
    subject = message.split("\n", 1)[0]
    if subject.startswith("Merge "):
        return None  # merge commits describe branches, not code
    if _REVERT_RE.search(message):
        return CommitFinding("revert", "EXTRACTED")
    conventional = _CONVENTIONAL_RE.match(subject)
    commit_type = conventional.group(1).lower() if conventional else None
    if commit_type in _FIX_TYPES:
        return CommitFinding("incident", "EXTRACTED")
    # A typed subject (feat:, docs:, ...) says what the commit is; a body that
    # mentions a bug or crash is context, not a fix.
    if commit_type is None and _FIX_RE.search(message):
        return CommitFinding("incident", "INFERRED")
    if _RATIONALE_HEADER_RE.search(message):
        return CommitFinding("decision", "EXTRACTED")
    if _RATIONALE_RE.search(message):
        return CommitFinding("decision", "INFERRED")
    return None


_RELATION = {"revert": "reverted", "incident": "changed", "decision": "changed"}


def _event_id(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()[:24]


def _commit_event(commit: Commit, finding: CommitFinding) -> Event:
    _, _, body = commit.message.partition("\n")
    return Event(
        kind=finding.kind,
        summary=_redact(commit.subject)[:SUMMARY_LIMIT],
        body=_redact(body.strip())[:BODY_LIMIT],
        source_id=f"commit:{commit.sha}",
        commit_sha=commit.sha,
        confidence=finding.confidence,
        recorded_at=commit.authored_at,
        id=_event_id("commit", commit.sha),
    )


# --------------------------------------------------------------------------- #
# Transcripts
# --------------------------------------------------------------------------- #

_CORRECTION_START_RE = re.compile(
    r"^(?:no\b|nope\b|don'?t\b|do not\b|stop\b|wait\b|actually\b|undo\b|revert\b|wrong\b|never\b|"
    r"that'?s (?:not|wrong)|this is wrong|not (?:like )?that|why did you|please don'?t|"
    r"you (?:shouldn'?t|should not|broke|missed|forgot))",
    re.I,
)
_CORRECTION_ANYWHERE_RE = re.compile(
    r"\bthat'?s not what i (?:asked|meant|wanted)\b|\byou (?:broke|misunderstood)\b|"
    r"\bdon'?t (?:do|use|add|change|touch|remove|delete|call)\b",
    re.I,
)
_REALIZATION_RE = re.compile(
    r"\bi see the (?:issue|problem|bug)\b|\bi see what'?s (?:happening|going on)\b|"
    # "the real bug is ..." realizes something; "the real bug is fixed" reports success.
    r"\b(?:the|that'?s the) (?:real|actual) (?:issue|problem|cause|bug)(?: here)? (?:is|was)\b(?! (?:now |genuinely )?fixed)|"
    r"\broot cause\b|\bturns out\b|"
    r"\b(?:that|this) (?:didn'?t|did not|doesn'?t|does not) work\b|"
    r"\b(?:this|that) approach (?:won'?t|will not|doesn'?t|does not|can'?t) work\b|"
    r"\bmy mistake\b|\bi was wrong\b|\bi made a mistake\b|\bthat'?s not it\b",
    re.I,
)
# Standing rules: what must (not) happen from now on, not a reaction to one mistake.
_CONSTRAINT_RE = re.compile(
    r"\b(?:don'?t|do not|never)\s+(?:ever\s+)?(?!mind\b)"
    r"(?:touch|modify|change|edit|remove|delete|rename|refactor|move|bump|upgrade|downgrade|disable|bypass|"
    r"hard-?code|commit|push|block|call)\b"
    r"|\b(?:must|has to|have to)\s+(?:always\s+)?(?:use|be|stay|remain|keep|go|run|pass|return|match|call)\b"
    r"|\balways\s+(?:use|run|call|keep|go through|pass|return|check)\b"
    r"|\bremember to\b|\bfrom now on\b|\bgoing forward\b|\bmake sure (?:you )?(?:always|never)\b",
    re.I,
)
# A design decision the agent states: it names code, makes a choice, and gives a reason,
# e.g. "The model is `TaskFile`, not `File`, because `File` would clash with `fastapi.File`."
_CODE_SPAN_RE = re.compile(r"`[A-Za-z_$][\w$.]*(?:\(\))?`")
_CHOICE_RE = re.compile(
    r",? not `[^`]+`|\binstead of\b|\brather than\b|\bover `[^`]+`|\b(?:i |we )?(?:chose|picked|went with|opted for)\b",
    re.I,
)
_REASON_RE = re.compile(r"\bbecause\b|\bto avoid\b|\bso that\b|\botherwise\b|\bwhich would\b|\bsince\b", re.I)
# Blocks the harness injects into user turns; their contents aren't the user's words.
_INJECTED_TAGS = {
    "system-reminder", "ide_selection", "ide_opened_file", "ide_diagnostics", "command-name", "command-message",
    "command-args", "local-command-stdout", "local-command-stderr", "user-prompt-submit-hook",
}
_TAG_BLOCK_RE = re.compile(r"<([A-Za-z][\w-]*)\b[^>]*>(.*?)</\1\b[^>]*>", re.S)
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+|\n+")
_PATH_RE = re.compile(
    r"(?<![\w/\\.:-])((?:[A-Za-z]:)?[/\\]?(?:[\w.-]+[/\\])*[\w.-]+\.(?:py|pyi|js|jsx|mjs|cjs|ts|tsx|mts|cts))\b"
)
_NAMED_SYMBOL_RE = re.compile(r"`([A-Za-z_$][\w$.]*)(?:\(\))?`|\b([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\(\)")
_SECRET_RE = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{20,}|sk-ant-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|"
    r"AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{35})\b"
)
_EDIT_TOOLS = {"Edit", "MultiEdit", "Write", "NotebookEdit"}
_PATH_KEYS = ("file_path", "path", "notebook_path")


def _redact(text: str) -> str:
    return _SECRET_RE.sub("[redacted]", text)


def claude_projects_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"


def _encode_project(path: Path | str) -> str:
    # Claude Code names a project's transcript folder after its path, with every
    # character that isn't a letter or digit replaced by "-".
    return re.sub(r"[^A-Za-z0-9]", "-", str(path)).lower()


def transcript_files(projects_dir: Path, repo: Path) -> list[Path]:
    """Transcripts from sessions started in `repo`, inside it, or in a folder containing it.

    `projects_dir` is Claude Code's projects folder, or one project's folder of transcripts.
    """
    if not projects_dir.is_dir():
        return []
    direct = sorted(projects_dir.glob("*.jsonl"))
    if direct:
        return direct
    target = _encode_project(repo)
    files: list[Path] = []
    for folder in sorted(p for p in projects_dir.iterdir() if p.is_dir()):
        name = folder.name.lower()
        related = (target == name
                   or (target.startswith(name) and target[len(name)] == "-")
                   or (name.startswith(target) and name[len(target)] == "-"))
        if related:
            files.extend(sorted(folder.glob("*.jsonl")))
    return files


@dataclass
class _Turn:
    """One user or assistant message in a session."""

    role: str
    uuid: str
    cwd: str | None
    timestamp: datetime | None
    text: str
    edits: list[tuple[str, str]] = field(default_factory=list)  # (path as written, new text)
    touched: list[str] = field(default_factory=list)  # paths read or edited


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _strip_injected(text: str) -> str:
    """Drop harness-injected blocks; unwrap other tags (e.g. pasted content) to their text."""
    def replace(match: re.Match) -> str:
        return "" if match.group(1).lower() in _INJECTED_TAGS else _strip_injected(match.group(2))

    return _TAG_BLOCK_RE.sub(replace, text).strip()


def read_session(path: Path) -> Iterator[_Turn]:
    """The user and assistant turns of one transcript; tool output and subagent chatter are skipped."""
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict) or record.get("type") not in ("user", "assistant"):
                continue
            if record.get("isSidechain") or record.get("isMeta"):
                continue
            if record.get("isCompactSummary") or record.get("isVisibleInTranscriptOnly"):
                continue  # Claude Code's own summary after compaction, filed as a user turn
            message = record.get("message") or {}
            content = message.get("content") if isinstance(message, dict) else None
            blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content or []
            if not isinstance(blocks, list):
                continue
            if record["type"] == "user" and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in blocks):
                continue  # tool output, not something the user said

            texts, edits, touched = [], [], []
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    texts.append(block["text"])
                elif block.get("type") == "tool_use" and isinstance(block.get("input"), dict):
                    tool_input = block["input"]
                    file_path = next((tool_input[k] for k in _PATH_KEYS if isinstance(tool_input.get(k), str)), None)
                    if file_path is None:
                        continue
                    touched.append(file_path)
                    if block.get("name") in _EDIT_TOOLS:
                        # Whole-file writes (`content`) would point at every function in the
                        # file, so only targeted replacements locate symbols.
                        pieces = [tool_input.get("new_string")]
                        pieces += [e.get("new_string") for e in tool_input.get("edits") or [] if isinstance(e, dict)]
                        edits += [(file_path, piece) for piece in pieces if isinstance(piece, str) and piece.strip()]
            text = _strip_injected("\n".join(texts))
            yield _Turn(record["type"], str(record.get("uuid") or ""), record.get("cwd"),
                        _parse_timestamp(record.get("timestamp")), text, edits, touched)


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_RE.split(text) if s.strip()]


def find_decision(text: str) -> str | None:
    """The first sentence of an agent's text that states a design choice and its reason."""
    for sentence in _sentences(text):
        if _CODE_SPAN_RE.search(sentence) and _CHOICE_RE.search(sentence) and _REASON_RE.search(sentence):
            return sentence.lstrip("-*• ").strip()[:SUMMARY_LIMIT]
    return None


def find_constraint(text: str) -> str | None:
    """The first sentence of `text` that states a standing rule, if any."""
    for sentence in _sentences(text):
        if not sentence.endswith("?") and _CONSTRAINT_RE.search(sentence):
            return sentence[:SUMMARY_LIMIT]
    return None


def _first_sentences(text: str, start: int = 0, count: int = 2) -> str:
    sentences = [s.strip() for s in _SENTENCE_RE.split(text[start:]) if s.strip()]
    return " ".join(sentences[:count])[:SUMMARY_LIMIT]


def _sentence_around(text: str, position: int) -> str:
    """The sentence containing `position`, plus the one after it."""
    starts = [0] + [m.end() for m in _SENTENCE_RE.finditer(text) if m.end() <= position]
    return _first_sentences(text, starts[-1])


@dataclass(frozen=True)
class TranscriptFinding:
    kind: str  # "correction" or "dead_end"
    summary: str


def classify_turn(turn: _Turn, *, after_assistant: bool = True) -> TranscriptFinding | None:
    """A correction (user pushing back on the agent) or a dead end (the agent realizing it was wrong).

    `after_assistant` says whether the agent spoke before this turn: a correction
    reacts to something, so a session's opening message never is one.
    """
    if not turn.text:
        return None
    if turn.role == "user":
        if not after_assistant:
            return None
        short = len(turn.text) <= CORRECTION_ANYWHERE_LIMIT
        if _CORRECTION_START_RE.match(turn.text) or (short and _CORRECTION_ANYWHERE_RE.search(turn.text)):
            return TranscriptFinding("correction", _first_sentences(turn.text))
        return None
    match = _REALIZATION_RE.search(turn.text)
    if match:
        return TranscriptFinding("dead_end", _sentence_around(turn.text, match.start()))
    return None


# --------------------------------------------------------------------------- #
# Linking
# --------------------------------------------------------------------------- #


class _Linker:
    """Resolve paths, edits and names from free text to symbols in the index."""

    def __init__(self, conn: sqlite3.Connection, repo: Path, blobs: git.BlobReader) -> None:
        self.conn, self.repo, self.blobs = conn, repo, blobs
        self.by_file: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))  # path -> local -> ids
        self.by_short_name: dict[str, set[str]] = defaultdict(set)
        for symbol, version in store.current_symbol_versions(conn):
            prefix = module_name(version.file_path) + "."
            name = version.qualified_name or symbol.qualified_name
            local = name[len(prefix):] if name.startswith(prefix) else name
            self.by_file[version.file_path][local].append(symbol.id)
            self.by_short_name[local.rsplit(".", 1)[-1]].add(symbol.id)
        self.paths = {p.lower(): p for p in self.by_file}
        self._head: dict[str, tuple[str, list] | None] = {}
        self._repo_parts = self._parts(repo)

    @staticmethod
    def _parts(path: Path) -> list[str]:
        # realpath: macOS temp dirs (/var -> /private/var) and symlinked checkouts compare equal.
        parts = list(Path(os.path.realpath(path)).parts)
        return [p.lower() for p in parts] if os.name == "nt" else parts

    def resolve_path(self, raw: str, cwd: str | None) -> str | None:
        """The repo-relative path (as git spells it) of a path mentioned in a session."""
        candidate = Path(raw.replace("\\", "/") if os.name != "nt" else raw)
        if not candidate.is_absolute():
            candidate = Path(cwd or self.repo) / candidate
        parts = list(Path(os.path.realpath(candidate)).parts)
        compare = [p.lower() for p in parts] if os.name == "nt" else parts
        if compare[:len(self._repo_parts)] != self._repo_parts:
            return None
        rel = PurePosixPath(*parts[len(self._repo_parts):]).as_posix()
        return self.paths.get(rel.lower(), rel)

    def _head_symbols(self, path: str) -> tuple[str, list] | None:
        if path not in self._head:
            source = self.blobs.read("HEAD", path) if language_for(path) else None
            self._head[path] = (source.decode("utf-8", errors="replace"), extract_symbols(path, source)) \
                if source is not None else None
        return self._head[path]

    def symbols_for_edit(self, path: str, new_text: str) -> set[str]:
        """Symbols at HEAD whose lines overlap where this edit's text now sits."""
        head = self._head_symbols(path)
        if head is None:
            return set()
        text, parsed = head
        offset = text.find(new_text.strip())
        if offset < 0:
            return set()  # the code has changed again since this edit
        first = text.count("\n", 0, offset) + 1
        last = first + new_text.strip().count("\n")
        overlapping = [s for s in parsed if s.start_line <= last and s.end_line >= first]
        # Prefer the innermost definitions: an edit inside a method is about the method.
        innermost = [s for s in overlapping
                     if not any(o is not s and o.local_name.startswith(s.local_name + ".") for o in overlapping)]
        return {sid for s in innermost for sid in self.by_file.get(path, {}).get(s.local_name, [])}

    def symbols_named(self, text: str, paths: Iterable[str]) -> set[str]:
        """Symbols named in `text`: within the given files, else uniquely across the repo."""
        files = [p for p in paths if p in self.by_file]
        found: set[str] = set()
        for match in _NAMED_SYMBOL_RE.finditer(text):
            name = match.group(1) or match.group(2)
            short = name.rsplit(".", 1)[-1]
            in_files = {sid for p in files for local, ids in self.by_file[p].items()
                        if local == name or local.endswith("." + name) or local.rsplit(".", 1)[-1] == short
                        for sid in ids}
            if in_files:
                found |= in_files
            elif len(self.by_short_name.get(short, ())) == 1 and len(short) > 3:
                found |= self.by_short_name[short]
        return found

    def paths_in(self, text: str, cwd: str | None) -> list[str]:
        return [p for raw in _PATH_RE.findall(text) if (p := self.resolve_path(raw, cwd))]


# --------------------------------------------------------------------------- #
# Ingest
# --------------------------------------------------------------------------- #


@dataclass
class EventIngestResult:
    commit_events: int = 0
    transcript_events: int = 0
    links: int = 0
    sessions: int = 0


def _store_event(conn: sqlite3.Connection, event: Event, links: Iterable[tuple[str, str]],
                 result: EventIngestResult) -> bool:
    if store.get_event(conn, event.id) is not None:
        return False
    links = sorted(set(links))
    store.insert_event(conn, event, links)
    result.links += len(links)
    return True


def _focus(located: set[str], named: set[str]) -> set[str]:
    """Keep links specific: a broad set falls back to the symbols the text names, or to nothing."""
    linked = located | named
    if len(linked) <= MAX_LINKS:
        return linked
    return named if len(named) <= MAX_LINKS else set()


def ingest_commit_events(conn: sqlite3.Connection, linker: _Linker, result: EventIngestResult) -> None:
    for commit in store.commits_between(conn, newest_first=False):
        found: list[tuple[Event, str]] = []
        finding = classify_commit(commit.message)
        if finding is not None:
            found.append((_commit_event(commit, finding), _RELATION[finding.kind]))
        _, _, body = commit.message.partition("\n")
        rule = find_constraint(body)
        if rule is not None:  # "Never call this from the event loop": a standing rule, kept on its own
            constraint = replace(_commit_event(commit, CommitFinding("constraint", "INFERRED")),
                                 summary=_redact(rule), id=_event_id("commit", commit.sha, "constraint"))
            found.append((constraint, "about"))
        found = [(event, relation) for event, relation in found if store.get_event(conn, event.id) is None]
        if not found:
            continue
        changed = set(store.symbols_changed_in(conn, commit.sha))
        named = linker.symbols_named(commit.message, linker.by_file) & changed
        with store.batch(conn):
            store.insert_source(conn, Source(f"commit:{commit.sha}", "commit", uri=commit.sha,
                                             captured_at=commit.authored_at))
            for event, relation in found:
                links = [(sid, relation) for sid in _focus(changed, named)]
                if _store_event(conn, event, links, result):
                    result.commit_events += 1


def _session_links(linker: _Linker, turns: list[_Turn], index: int, kind: str,
                   sentence: str | None = None) -> set[tuple[str, str]]:
    """Symbols an event is about: the edits it reacts to (corrections) or leads to (dead ends)."""
    turn = turns[index]
    if kind in ("correction", "constraint"):
        # The assistant turns since the previous user message are what's being corrected,
        # or the context a rule is stated in.
        window = []
        for previous in reversed(turns[:index]):
            if previous.role == "user":
                break
            window.append(previous)
    else:
        # A realization comes right before the fix: this turn and the ones up to the next user message.
        window = [turn]
        for following in turns[index + 1:]:
            if following.role == "user":
                break
            window.append(following)

    located: set[str] = set()
    files: set[str] = set()
    for t in window:
        for raw, new_text in t.edits:
            path = linker.resolve_path(raw, t.cwd)
            if path:
                files.add(path)
                located |= linker.symbols_for_edit(path, new_text)
        files |= {p for raw in t.touched if (p := linker.resolve_path(raw, t.cwd))}
    files |= set(linker.paths_in(turn.text, turn.cwd))
    named = linker.symbols_named(turn.text, files)
    if kind in ("constraint", "decision"):
        # A rule or a choice is about what it names, not about whatever was just edited.
        # Long turns name many things; resolve names from the rule's own sentence.
        located = set()
        named = linker.symbols_named(sentence or turn.text, files)
    return {(sid, "about") for sid in _focus(located, named)}


def _ingest_session(conn: sqlite3.Connection, path: Path, linker: _Linker, result: EventIngestResult) -> bool:
    """Store the events of one transcript; True if any were new."""
    turns = list(read_session(path))
    session_id = path.stem
    in_repo = any(t.cwd and linker.resolve_path(".", t.cwd) is not None for t in turns)
    captured_at = next((t.timestamp for t in turns if t.timestamp), None) or datetime.now(timezone.utc)
    added = False
    seen_assistant = False
    for index, turn in enumerate(turns):
        findings = [f for f in (classify_turn(turn, after_assistant=seen_assistant),) if f]
        seen_assistant |= turn.role == "assistant"
        if turn.role == "user" and (rule := find_constraint(turn.text)):
            findings.append(TranscriptFinding("constraint", rule))
        if turn.role == "assistant" and (choice := find_decision(turn.text)):
            findings.append(TranscriptFinding("decision", choice))
        for finding in findings:
            links = _session_links(linker, turns, index, finding.kind, finding.summary)
            if not links and not in_repo:
                continue  # a session elsewhere that never touched this repository
            event = Event(
                kind=finding.kind,
                summary=_redact(finding.summary),
                body=_redact(turn.text)[:BODY_LIMIT],
                source_id=f"transcript:{session_id}",
                confidence="INFERRED",
                recorded_at=turn.timestamp or captured_at,
                id=_event_id("transcript", session_id, turn.uuid, finding.kind),
            )
            with store.batch(conn):
                store.insert_source(conn, Source(f"transcript:{session_id}", "transcript", agent="claude-code",
                                                 uri=str(path), captured_at=captured_at))
                if _store_event(conn, event, links, result):
                    result.transcript_events += 1
                    added = True
    return added


def ingest_events(conn: sqlite3.Connection, repo: Path | str = ".", *,
                  projects_dir: Path | None = None, transcripts: bool = True) -> EventIngestResult:
    """Mine commits already in the index, then Claude Code transcripts for this repository."""
    result = EventIngestResult()
    root = git.repo_root(repo)
    with git.BlobReader(root) as blobs:
        linker = _Linker(conn, root, blobs)
        ingest_commit_events(conn, linker, result)
        if transcripts:
            for path in transcript_files(projects_dir or claude_projects_dir(), root):
                result.sessions += _ingest_session(conn, path, linker, result)
    return result

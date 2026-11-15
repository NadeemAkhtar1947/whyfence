"""Mine "why" events from commit messages and agent session logs, and link them to symbols.

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
import subprocess
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterable, Iterator

from whyfence import store
from whyfence.ingest import agent_logs, git
from whyfence.ingest.agent_logs import AgentSession, parse_timestamp as _parse_timestamp
from whyfence.ingest.agent_logs import Turn as _Turn
from whyfence.ingest.symbols import _unquote as unquote_path
from whyfence.ingest.symbols import LANGUAGES, extract_symbols, language_for, module_name, qualify
from whyfence.store import Commit, Event, Source

MINING_KEY = "events.mining"  # meta key: which commit-mining rules built the events
# Bump when commit mining finds different events, so they are re-mined (not the symbols).
# 0.4.1: commits touching only docs/tests/CI/assets, "Add ... Fixes #N", reverts of reverted
# merges, code blocks and tracebacks, rules without a subject.
MINING_VERSION = "0.4.1"
SUMMARY_LIMIT = 300
BODY_LIMIT = 2000
# An event touching more symbols than this is a broad change (a feature commit, a
# refactor), not a statement about any one of them: it keeps only the symbols it names.
MAX_LINKS = 8
CORRECTION_ANYWHERE_LIMIT = 300  # long messages are instructions; only short ones correct mid-sentence

# --------------------------------------------------------------------------- #
# Commit messages
# --------------------------------------------------------------------------- #

# Subject only: a squash merge's body lists every commit on the branch, reverts included.
_REVERT_RE = re.compile(r'^Revert "|^revert(?:\([^)]*\))?!?:', re.I)
# Re-applying a reverted merge (`Revert "Revert "Merge ...""`) is branch housekeeping: it says
# nothing about why any code is the way it is (both labellers of eval/blind-80 agreed).
_REVERTED_MERGE_RE = re.compile(r'^Revert "Revert "Merge\b', re.I)
_CONVENTIONAL_RE = re.compile(r"^([A-Za-z]+)(?:\([^)]*\))?!?:")
_SCOPE_RE = re.compile(r"^[A-Za-z]+\(([^)]*)\)!?:")  # `fix(ci): ...` -> "ci"
_FIX_TYPES = {"fix", "bugfix", "hotfix"}
_FIX_SUBJECT_RE = re.compile(r"^fix(?:e[sd])?\b|\bbugs?\b|\bregression\b|\bcrash(?:es|ed|ing)?\b", re.I)
_FIXES_ISSUE_RE = re.compile(r"\bfix(?:es|ed)?\s+#\d+", re.I)
# "Added support for tuple types. This fixes #239": the issue was a feature request.
_ADDITION_RE = re.compile(r"^(?:add(?:s|ed)?|support(?:s|ed)?)\b", re.I)
# Correcting prose isn't an incident anyone needs to know about before changing the code.
_TRIVIAL_FIX_RE = re.compile(r"\b(?:typos?|spelling|grammar|docs?|documentation|readme|changelog)\b", re.I)
# Nor is fixing the tooling around the code. These match the phrasing of a tooling fix,
# not bare words: "formatted help text", "string format error", "Fix links header parsing"
# and "Fix URL encoding" are real bugs (in click and requests), so they must stay incidents.
_TOOLING_FIX_RE = re.compile(
    # Formatting and style, when that's the whole point ("Fixed formatting", "formatting issues").
    r"\bformatting\b\s*(?:$|[.(:]|issues?\b|errors?\b|problems?\b)|\breformat\w*|\bcode style\b|"
    r"\bstyle (?:issues?|errors?|problems?|fixes?|checks?)\b|\b(?:using|with|run(?:ning)?|ran)\s+black\b|"
    r"\bwhitespace\b|\bindent(?:ation|ing)?\b|"
    # Linters, type checkers and type-only changes.
    r"\b(?:lint\w*|flake8|pep ?8|pylint|ruff|isort|mypy|pyright|typing)\b|"
    r"\btype[- ](?:hints?|annotations?|errors?|check\w*)\b|\bannotations?\b|"
    # Tests and CI.
    r"\bfix(?:e[sd]|ing)?\s+(?:the\s+|a\s+|some\s+|broken\s+|failing\s+|flaky\s+)*(?:unit\s+)?tests?\b|"
    r"\btest ?suite\b|\b(?:broken|failing|flaky)\s+tests?\b|"
    r"\b(?:ci|tox|travis|appveyor|circleci|github actions|pre-commit|codecov)\b|"
    # Links, docstrings and comments.
    r"^fix(?:e[sd])?\s+(?:the\s+|a\s+|some\s+|broken\s+|dead\s+|\w+\s+)?links?\b\s*(?:$|[.(]|for good\b|in\b|to\b)|"
    r"\b(?:broken|dead)\s+links?\b|\bdocstrings?\b|\bcomments?\b",
    re.I,
)
_TOOLING_SCOPES = {"ci", "test", "tests", "lint", "style", "format", "typing", "types", "docs", "build", "deps"}
_BULLET_RE = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s")  # "- x", "* x", "• x", "1. x", "2) x"
_RATIONALE_HEADER_RE = re.compile(r"^\s*(?:why|rationale|reason|motivation)\s*:", re.I | re.M)
# "why" alone is usually a noun ("the why tool"); only rationale phrasing counts.
_RATIONALE_RE = re.compile(
    r"\b(?:that'?s|this is|which is|here'?s) why\b|\bbecause\b|\bso that\b|\bin order to\b|"
    r"\bwe (?:chose|decided|opted)\b|\bdecided to\b",
    re.I,
)


@dataclass(frozen=True)
class CommitFinding:
    kind: str  # "revert", "incident" or "decision"
    confidence: str


_EXCEPTION_LINE_RE = re.compile(r"^[A-Za-z_][\w.]*(?:Error|Exception|Warning|Exit|Interrupt)\b")
_TRAILER_RE = re.compile(r"^[A-Z][\w-]*-by:\s|^Change-Id:\s", re.I)  # Co-Authored-By:, Signed-off-by:, ...
# Bodies wrap at 60-80 columns, so a wrapped line is nearly full. A shorter line ended on
# purpose: flask 66b1b752 lists "don't remove handlers" (21) among items of 16-36 characters,
# while its wrapped prose runs 68-71.
WRAPPED_LINE = 45


def _prose(message: str) -> str:
    """The explaining part of a commit message: one line per paragraph.

    Commit bodies are wrapped at about 72 columns, so a newline inside a paragraph
    is not the end of a sentence: lines are joined, and only blank lines separate
    paragraphs. Bullet items (with their wrapped continuation lines) list changes
    rather than explain them, and trailers such as Co-Authored-By are metadata, so
    both are left out. Agent sessions don't go through here: in a chat, a newline
    usually does end a thought.
    """
    paragraphs: list[str] = []
    current: list[str] = []
    in_bullet = in_fence = in_traceback = False
    for line in message.splitlines():
        # Code and quoted output are not prose: a fenced or indented block, or a traceback
        # (flask 8bb71852 quotes a traceback ending "... must be a string, tuple, ...";
        # django 00dca6f0 indents PostgreSQL's "... must match initial ORDER BY").
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            line = ""
        elif in_fence:
            continue
        elif line.startswith("Traceback (most recent call last)"):
            in_traceback = True
            line = ""
        elif in_traceback:
            if not line.strip() or _EXCEPTION_LINE_RE.match(line):
                in_traceback = False
            continue
        elif (line.startswith(("    ", "\t")) and line.strip() and not in_bullet):
            line = ""  # an indented code block ends the paragraph like a blank line
        if current and len(current[-1]) < WRAPPED_LINE:
            # The previous line stopped short of the wrap width: a deliberate break (a list
            # written without markers, say), not a sentence running on. Keep it on its own.
            paragraphs.append(" ".join(current))
            current = []
        if not line.strip():
            if current:
                paragraphs.append(" ".join(current))
                current = []
            in_bullet = False
        elif _BULLET_RE.match(line):
            in_bullet = True
        elif in_bullet and line[:1].isspace():
            pass  # a bullet's wrapped continuation
        elif _TRAILER_RE.match(line):
            in_bullet = False
        else:
            in_bullet = False
            current.append(line.strip())
    if current:
        paragraphs.append(" ".join(current))
    return "\n".join(paragraphs)


def classify_commit(message: str) -> CommitFinding | None:
    """What a commit message says about why code changed, if anything."""
    subject = message.split("\n", 1)[0]
    if subject.startswith("Merge "):
        return None  # merge commits describe branches, not code
    if _REVERT_RE.search(subject):
        if _REVERTED_MERGE_RE.match(subject):
            return None
        return CommitFinding("revert", "EXTRACTED")
    conventional = _CONVENTIONAL_RE.match(subject)
    commit_type = conventional.group(1).lower() if conventional else None
    scope = _SCOPE_RE.match(subject)
    trivial = (_TRIVIAL_FIX_RE.search(subject) is not None or _TOOLING_FIX_RE.search(subject) is not None
               or (scope is not None and scope.group(1).lower() in _TOOLING_SCOPES))
    if commit_type in _FIX_TYPES and not trivial:
        return CommitFinding("incident", "EXTRACTED")
    # A typed subject (feat:, docs:, ...) says what the commit is; a body that
    # mentions a bug or crash is context, not a fix.
    issue_fixed = _FIXES_ISSUE_RE.search(message) is not None and not _ADDITION_RE.match(subject)
    if commit_type is None and not trivial and (_FIX_SUBJECT_RE.search(subject) or issue_fixed):
        return CommitFinding("incident", "INFERRED")
    prose = _prose(message)
    if _RATIONALE_HEADER_RE.search(prose):
        return CommitFinding("decision", "EXTRACTED")
    if _RATIONALE_RE.search(prose):
        return CommitFinding("decision", "INFERRED")
    return None


_RELATION = {"revert": "reverted", "incident": "changed", "decision": "changed"}


def _event_id(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()[:24]


def _reason(body: str) -> str | None:
    """The sentence of a commit body that gives the reason: a `Why:` line, or "...because...".

    `body` is the message without its subject: when only the subject carries the reason
    ("Switch to WAL mode so that readers never block"), the subject already says it.
    """
    prose = _prose(body)
    for line in prose.splitlines():
        header = _RATIONALE_HEADER_RE.match(line)
        if header:
            return line[header.end():].strip() or None
    return next((sentence for sentence in _sentences(prose) if _RATIONALE_RE.search(sentence)), None)


def _commit_event(commit: Commit, finding: CommitFinding) -> Event:
    _, _, body = commit.message.partition("\n")
    summary = commit.subject
    if finding.kind == "decision":
        # What was decided (the subject) and why (the reason sentence): either alone says half.
        reason = _reason(body)
        if reason and reason not in summary:
            summary = f"{summary} — {reason}"
    return Event(
        kind=finding.kind,
        summary=_redact(summary)[:SUMMARY_LIMIT],
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
    # ...but "we don't have to keep them in sync" releases an obligation; it isn't a rule.
    r"|\b(?<!n't )(?<!not )(?:must|has to|have to)\s+(?:always\s+)?"
    r"(?:use|be|stay|remain|keep|go|run|pass|return|match|call)\b"
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
# Pasted text is kept as context ("No, revert that" pasted from a chat is still a correction),
# but it never becomes a rule: a pasted email, web page or review can carry instructions its
# author planted, and they would otherwise be trusted as coming from your own session.
_PASTED_TAGS = {"pasted_content"}
_TAG_BLOCK_RE = re.compile(r"<([A-Za-z][\w-]*)\b[^>]*>(.*?)</\1\b[^>]*>", re.S)
# A period ends a sentence, except in "e.g.", "i.e.", "etc.", "vs." and "cf.": django 00dca6f0's
# rule was cut at "e.g." to "two volatile extra() selections must keep ordering ...".
_SENTENCE_RE = re.compile(
    r"(?<=[.!?])(?<!\be\.g\.)(?<!\bi\.e\.)(?<!\betc\.)(?<!\bvs\.)(?<!\bcf\.)\s+|\n+", re.I)
_PATH_RE = re.compile(
    r"(?<![\w/\\.:-])((?:[A-Za-z]:)?[/\\]?(?:[\w.-]+[/\\])*[\w.-]+\.(?:py|pyi|js|jsx|mjs|cjs|ts|tsx|mts|cts|go|rs|java))\b"
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


def _strip_injected(text: str, *, drop_pasted: bool = False) -> str:
    """Drop harness-injected blocks and unwrap other tags to their text; `drop_pasted` drops pastes too."""
    dropped = _INJECTED_TAGS | _PASTED_TAGS if drop_pasted else _INJECTED_TAGS

    def replace(match: re.Match) -> str:
        if match.group(1).lower() in dropped:
            return ""
        return _strip_injected(match.group(2), drop_pasted=drop_pasted)

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
            raw = "\n".join(texts)
            text = _strip_injected(raw)
            own = _strip_injected(raw, drop_pasted=True)
            yield _Turn(record["type"], str(record.get("uuid") or ""), record.get("cwd"),
                        _parse_timestamp(record.get("timestamp")), text, edits, touched,
                        own_text=None if own == text else own)


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
        if sentence.endswith("?") or _QUOTED_ERROR_RE.search(sentence):
            continue  # a question, or an error message quoted ("TypeError: x must be string")
        if _NO_SUBJECT_RE.match(sentence):
            continue  # "must be importable for ...": the subject was lost in a wrapped line
        if _CONSTRAINT_RE.search(sentence):
            return sentence[:SUMMARY_LIMIT]
    return None


# Lowercase on purpose: "must be importable for ..." is the tail of a wrapped line whose
# subject was lost, while "Must use Decimal for money." is a terse rule written as a sentence.
_NO_SUBJECT_RE = re.compile(r"(?:must|has to|have to|should|shall|needs? to|ought to)\b")
_QUOTED_ERROR_RE = re.compile(r"\b[A-Z]\w*(?:Error|Exception|Warning):\s")


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


def _named_among(conn: sqlite3.Connection, text: str, candidates: set[str]) -> set[str]:
    """The candidates that `text` names, by any name they had in history.

    A commit's symbols may since have been renamed or deleted, so names are matched
    against every version's name, not just what exists at HEAD.
    """
    names = {match.group(1) or match.group(2) for match in _NAMED_SYMBOL_RE.finditer(text)}
    if not names:
        return set()
    shorts = {name.rsplit(".", 1)[-1] for name in names}
    found = set()
    for symbol_id in candidates:
        symbol = store.get_symbol(conn, symbol_id)
        known = {v.qualified_name for v in store.symbol_history(conn, symbol_id) if v.qualified_name}
        known |= {symbol.qualified_name} if symbol else set()
        if any(q.rsplit(".", 1)[-1] in shorts or any(q == n or q.endswith("." + n) for n in names) for q in known):
            found.add(symbol_id)
    return found


# --------------------------------------------------------------------------- #
# Commits that can't change behaviour
# --------------------------------------------------------------------------- #

# A commit that touches only these says nothing about why the code behaves as it does:
# "Fix broken example", `Revert "updates to docs"`, "Fix flaky timeout handling" (24 of the
# 34 rejections in eval/blind-80-reviewer.csv). Test directories count only at the top
# level: django/test/ and click/testing.py are product code.
_DOC_SUFFIXES = (".rst", ".md", ".markdown", ".adoc", ".asciidoc", ".rdoc")
_DOC_NAMES = ("readme", "changes", "changelog", "history", "news", "authors", "contributing", "contributors",
              "license", "licence", "copying", "code_of_conduct", "security", "releasing", "release_notes")
_TOP_LEVEL_NON_CODE = {"docs", "doc", "documentation", "examples", "example", "samples", "tests", "test",
                       "testing", "artwork", "assets", ".github", ".circleci", ".gitlab", ".azure-pipelines",
                       ".devcontainer", ".vscode", ".idea"}
_TEST_FILE_RE = re.compile(r"(?:^test_.*\.py|.*_test\.py|^conftest\.py|.*_test\.go|"
                           r".*\.(?:test|spec)\.[cm]?[jt]sx?)$")
_CI_FILES = {".travis.yml", "tox.ini", "noxfile.py", "appveyor.yml", ".appveyor.yml", ".gitlab-ci.yml",
             "azure-pipelines.yml", ".pre-commit-config.yaml", "codecov.yml", ".codecov.yml", ".coveragerc",
             ".readthedocs.yml", ".readthedocs.yaml", "readthedocs.yml", ".editorconfig", ".gitignore",
             ".gitattributes", ".mailmap", "mkdocs.yml"}
_ASSET_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp", ".bmp", ".pdf", ".ttf", ".otf",
                   ".woff", ".woff2", ".eot", ".psd", ".ai", ".xcf")


def is_code_path(path: str) -> bool:
    """Whether changing this file can change how the software behaves.

    False for docs, examples, tests, CI configuration, changelogs and images. When in
    doubt, True: a missed doc commit costs one noisy event, a missed code commit loses
    a real one.
    """
    parts = PurePosixPath(path).parts
    name = parts[-1].lower() if parts else ""
    if parts and parts[0].lower() in _TOP_LEVEL_NON_CODE:
        return False
    if "__tests__" in parts or _TEST_FILE_RE.match(name):
        return False
    if name.endswith(_DOC_SUFFIXES + _ASSET_SUFFIXES) or name in _CI_FILES:
        return False
    if PurePosixPath(name).stem in _DOC_NAMES:
        return False
    return True


def changed_paths(repo: Path, commits: Iterable[Commit]) -> dict[str, set[str]]:
    """{sha: every path the commit changed against its first parent}, from one git process.

    Renames are listed as both the old and the new path: moving code into docs/ is a
    code change.
    """
    lines = [f"{c.sha} {c.parents[0]}" if c.parents else c.sha for c in commits]
    if not lines:
        return {}
    result = subprocess.run(
        git._git(repo, "diff-tree", "--stdin", "-r", "--root", "--no-renames", "--name-only", "--no-color",
                 "--format=%x01%H"),
        input="".join(f"{line}\n" for line in lines).encode(), capture_output=True)
    if result.returncode != 0:
        raise git.GitError(result.stderr.decode("utf-8", errors="replace").strip() or "git diff-tree failed")
    found: dict[str, set[str]] = {}
    current: set[str] | None = None
    for raw in result.stdout.split(b"\n"):
        line = raw.decode("utf-8", errors="replace").rstrip("\r")
        if line.startswith("\x01"):
            current = found.setdefault(line[1:].strip(), set())
        elif line.strip() and current is not None:
            current.add(unquote_path(line.strip()))
    return found


def _innermost(overlapping: list) -> list:
    """Drop definitions that enclose another one: a change inside a method is about the method."""
    return [s for s in overlapping
            if not any(o is not s and o.local_name.startswith(s.local_name + ".") for o in overlapping)]


def _git_lines(repo: Path, *args: str) -> list[str]:
    return [raw.decode("utf-8", errors="replace").strip() for raw in git.stream(repo, *args, sep=b"\n")
            if raw.strip()]


_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


_INDEXED_PATHS = tuple(f"*{suffix}" for suffix in LANGUAGES)  # only files that can hold symbols


def changed_ranges(repo: Path, shas: Iterable[str]) -> dict[str, dict[str, list[tuple[int, int]]]]:
    """For each commit, the line ranges each indexed file's new version changed against its parent.

    {sha: {path: [(first, last)]}}. One `git diff-tree --stdin` process serves every
    commit: starting a `git diff` per commit cost about 55 ms each, most of mining.
    Commits that changed no indexed file are left out.
    """
    shas = list(dict.fromkeys(shas))
    if not shas:
        return {}
    result = subprocess.run(
        git._git(repo, "diff-tree", "--stdin", "-p", "-U0", "-M", "--no-color", "--no-ext-diff",
                 "--format=%x01%H", "--", *_INDEXED_PATHS),
        input="".join(f"{sha}\n" for sha in shas).encode(), capture_output=True)
    if result.returncode != 0:
        raise git.GitError(result.stderr.decode("utf-8", errors="replace").strip() or "git diff-tree failed")
    found: dict[str, dict[str, list[tuple[int, int]]]] = {}
    ranges: dict[str, list[tuple[int, int]]] = defaultdict(list)
    path: str | None = None
    in_header = False
    for raw in result.stdout.split(b"\n"):
        line = raw.decode("utf-8", errors="replace").rstrip("\r")
        if line.startswith("\x01"):
            ranges = found.setdefault(line[1:].strip(), defaultdict(list))
            in_header, path = False, None
        elif line.startswith("diff --git "):
            in_header, path = True, None
        elif in_header and line.startswith("+++ "):
            target = unquote_path(line[4:])
            path = target[2:] if target.startswith("b/") else None  # "+++ /dev/null": deleted
        elif (match := _HUNK_RE.match(line)) and path:
            in_header = False
            start, count = int(match.group(1)), int(match.group(2) or 1)
            # A pure deletion (count 0) sits between line `start` and the next one.
            ranges[path].append((start, start + count - 1) if count else (start, start + 1))
    return found


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
        self._first_parent: set[str] | None = None
        self._side_ranges: dict[str, dict[str, list[tuple[int, int]]]] = {}

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
        needle = new_text.strip()
        offset = text.find(needle)
        if offset < 0:
            return set()  # the code has changed again since this edit
        if text.find(needle, offset + 1) >= 0:
            # The same text sits in more than one place (`return None`, a repeated guard).
            # The edit's old_string can't place it either: that text is gone from HEAD.
            # Guessing would link the event to the wrong function, so link nothing.
            return set()
        first = text.count("\n", 0, offset) + 1
        last = first + needle.count("\n")
        overlapping = [s for s in parsed if s.start_line <= last and s.end_line >= first]
        return {sid for s in _innermost(overlapping) for sid in self.by_file.get(path, {}).get(s.local_name, [])}

    def prepare_side_commits(self, commits: Iterable[Commit]) -> None:
        """Diff every merged-branch commit that will need links, in one git process."""
        wanted = [c.sha for c in commits if len(c.parents) == 1 and c.sha not in self.first_parent
                  and c.sha not in self._side_ranges]
        self._side_ranges.update(changed_ranges(self.repo, wanted))
        self._side_ranges.update({sha: {} for sha in wanted if sha not in self._side_ranges})

    @property
    def first_parent(self) -> set[str]:
        if self._first_parent is None:
            self._first_parent = set(_git_lines(self.repo, "rev-list", "--first-parent", "HEAD"))
        return self._first_parent

    def symbols_for_side_commit(self, commit: Commit) -> set[str]:
        """Symbols a commit on a merged branch changed.

        The history walk follows first parents only, so it never sees these commits;
        diff the commit against its own parent and place the changed lines in its own
        version of each file. A name that several symbols have had over time is
        settled by the merge that brought the branch in, or not linked at all.
        """
        if len(commit.parents) != 1:
            return set()
        if commit.sha not in self._side_ranges:
            self._side_ranges.update(changed_ranges(self.repo, [commit.sha]))
        found: set[str] = set()
        for path, spans in self._side_ranges.get(commit.sha, {}).items():
            source = self.blobs.read(commit.sha, path) if language_for(path) else None
            if source is None:
                continue
            overlapping = [s for s in extract_symbols(path, source)
                           if any(s.start_line <= end and s.end_line >= start for start, end in spans)]
            for parsed in _innermost(overlapping):
                versions = store.versions_named(self.conn, path, qualify(path, parsed.local_name))
                if len(versions) == 1:
                    found |= set(versions)
                elif versions:
                    merge = self._merge_of(commit.sha)
                    at_merge = {sid for sid, starts in versions.items() if merge in starts}
                    if len(at_merge) == 1:
                        found |= at_merge
        return found

    def _merge_of(self, sha: str) -> str | None:
        """The first-parent commit that brought `sha` into HEAD's history."""
        chain = _git_lines(self.repo, "rev-list", "--first-parent", "--ancestry-path", f"{sha}..HEAD")
        return chain[-1] if chain else None

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


def ingest_commit_events(conn: sqlite3.Connection, linker: _Linker, result: EventIngestResult,
                         progress: git.Progress | None = None) -> None:
    total = store.count_commits(conn) if progress else 0
    pending: list[tuple[Commit, list[tuple[Event, str]]]] = []
    for done, commit in enumerate(store.commits_between(conn, newest_first=False), start=1):
        if progress:
            progress(done, total)
        found: list[tuple[Event, str]] = []
        finding = classify_commit(commit.message)
        if finding is not None:
            found.append((_commit_event(commit, finding), _RELATION[finding.kind]))
        _, _, body = commit.message.partition("\n")
        rule = find_constraint(_prose(body))
        if rule is not None:  # "Never call this from the event loop": a standing rule, kept on its own
            constraint = replace(_commit_event(commit, CommitFinding("constraint", "INFERRED")),
                                 summary=_redact(rule), id=_event_id("commit", commit.sha, "constraint"))
            found.append((constraint, "about"))
        found = [(event, relation) for event, relation in found if store.get_event(conn, event.id) is None]
        if found:
            pending.append((commit, found))
    # A commit that only touched docs, examples, tests, CI or assets can't explain the code.
    paths = changed_paths(linker.repo, [commit for commit, _ in pending])
    pending = [(commit, found) for commit, found in pending
               if not paths.get(commit.sha) or any(is_code_path(path) for path in paths[commit.sha])]
    # Classify first, then diff all the merged-branch commits that need links at once.
    linker.prepare_side_commits(c for c, _ in pending if not store.symbols_changed_in(conn, c.sha))
    for commit, found in pending:
        changed = set(store.symbols_changed_in(conn, commit.sha))
        if not changed and commit.sha not in linker.first_parent:
            changed = linker.symbols_for_side_commit(commit)  # a commit on a merged branch
        named = _named_among(conn, commit.message, changed)
        with store.batch(conn):
            store.insert_source(conn, Source(f"commit:{commit.sha}", "commit", uri=commit.sha,
                                             captured_at=commit.authored_at))
            for event, relation in found:
                targets = _focus(changed, named)
                if event.kind == "constraint":
                    # A rule is about what it names ("Never call deploy() ..."), not about
                    # everything else the commit happened to touch.
                    targets = _named_among(conn, event.summary, changed) or targets
                links = [(sid, relation) for sid in targets]
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


def _ingest_session(conn: sqlite3.Connection, session: AgentSession, linker: _Linker,
                    result: EventIngestResult) -> bool:
    """Store the events of one agent session; True if any were new."""
    turns, session_id = session.turns, session.session_id
    in_repo = any(t.cwd and linker.resolve_path(".", t.cwd) is not None for t in turns)
    captured_at = next((t.timestamp for t in turns if t.timestamp), None) or datetime.now(timezone.utc)
    added = False
    seen_assistant = False
    for index, turn in enumerate(turns):
        findings = [f for f in (classify_turn(turn, after_assistant=seen_assistant),) if f]
        seen_assistant |= turn.role == "assistant"
        own_words = turn.own_text if turn.own_text is not None else turn.text
        if turn.role == "user" and (rule := find_constraint(own_words)):
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
                store.insert_source(conn, Source(f"transcript:{session_id}", "transcript", agent=session.agent,
                                                 uri=session.uri, captured_at=captured_at))
                if _store_event(conn, event, links, result):
                    result.transcript_events += 1
                    added = True
    return added


def _sessions(root: Path, projects_dir: Path | None, codex_dir: Path | None,
              gemini_dir: Path | None) -> Iterator[AgentSession]:
    # Naming any log location means "only these"; naming none means every agent's default.
    defaults = projects_dir is None and codex_dir is None and gemini_dir is None
    if defaults:
        projects_dir, codex_dir, gemini_dir = claude_projects_dir(), agent_logs.codex_home(), agent_logs.gemini_home()
    if projects_dir is not None:
        for path in transcript_files(projects_dir, root):
            yield AgentSession("claude-code", path.stem, str(path), list(read_session(path)))
    if codex_dir is not None:
        yield from agent_logs.codex_sessions(codex_dir, root)
    if gemini_dir is not None:
        yield from agent_logs.gemini_sessions(gemini_dir, root)


def ingest_events(conn: sqlite3.Connection, repo: Path | str = ".", *,
                  projects_dir: Path | None = None, codex_dir: Path | None = None, gemini_dir: Path | None = None,
                  transcripts: bool = True, progress: git.Progress | None = None) -> EventIngestResult:
    """Mine commits already in the index, then agent session logs for this repository."""
    result = EventIngestResult()
    root = git.repo_root(repo)
    mined_by = store.get_meta(conn, MINING_KEY)
    if mined_by != MINING_VERSION:
        if mined_by is not None or store.count_commit_events(conn):
            # Mining rules changed (0.4.1: docs-only commits, reverted merges, ...): re-mine commit
            # events, which are derived from git. Symbols are untouched; session events stay.
            store.forget_commit_events(conn)
        store.set_meta(conn, MINING_KEY, MINING_VERSION)
    with git.BlobReader(root) as blobs:
        linker = _Linker(conn, root, blobs)
        ingest_commit_events(conn, linker, result, progress)
        if transcripts:
            for session in _sessions(root, projects_dir, codex_dir, gemini_dir):
                result.sessions += _ingest_session(conn, session, linker, result)
    return result

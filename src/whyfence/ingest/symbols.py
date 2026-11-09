"""Index functions and classes across git history, following them through renames and moves.

The walk follows the first-parent chain of HEAD, oldest first, so each commit
(including merges, diffed against their first parent) moves the index forward
by one step. For every commit, only the files it changed are re-parsed.

Matching a commit's definitions to existing symbols happens in three passes:

1. Same name in the same file (following git's file renames): same symbol.
2. Same fingerprint, i.e. the definition's text with its own name removed and
   whitespace collapsed: a rename or a move, including across files.
3. Similar enough tokens: renamed *and* edited in the same commit.

Anything left is a new symbol (or, on the old side, a deletion). A new version
is recorded when a symbol's code, file or name changes, so a version's line
numbers are where it stood at the commit that introduced that version.
"""

from __future__ import annotations

import bisect
import hashlib
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Iterator

from whyfence import store
from whyfence.ingest import git
from whyfence.store import Symbol, SymbolVersion

LANGUAGES = {
    ".py": "python", ".pyi": "python",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".mts": "typescript", ".cts": "typescript", ".tsx": "tsx",
}
MAX_FILE_BYTES = 1_000_000  # larger sources are almost always generated or minified
MINIFIED_MIN_BYTES = 5_000  # with a mean line this long (characters), the file is a bundle
MINIFIED_MEAN_LINE = 300
SIMILARITY = 0.9  # token similarity for "renamed and edited"; at least this, or it's a new symbol
MIN_FUZZY_TOKENS = 25  # tiny definitions look alike; never fuzzy-match them
INDEXED_THROUGH = "symbols.indexed_through"  # meta key: last commit whose symbols are indexed


class SymbolError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# Parsing one file
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ParsedSymbol:
    local_name: str  # nesting path inside the file, e.g. "Cache.refresh"
    kind: str  # "class", "function" or "method"
    start_line: int  # 1-based, inclusive
    end_line: int
    fingerprint: str
    tokens: tuple[str, ...]


def language_for(path: str) -> str | None:
    return LANGUAGES.get(PurePosixPath(path).suffix.lower())


def symbol_language(path: str) -> str:
    language = language_for(path) or "unknown"
    return "typescript" if language == "tsx" else language


def module_name(path: str) -> str:
    """The prefix of qualified names in a file: `src/app/auth.py` -> `app.auth`."""
    posix = PurePosixPath(path)
    if language_for(path) != "python":
        return posix.with_suffix("").as_posix()
    parts = list(posix.with_suffix("").parts)
    if len(parts) > 1 and parts[0] in ("src", "lib"):
        parts = parts[1:]
    if len(parts) > 1 and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def qualify(path: str, local_name: str) -> str:
    return f"{module_name(path)}.{local_name}"


def _grammar(language: str):
    # Grammars ship compiled inside these wheels, so parsing never touches the network.
    if language == "python":
        import tree_sitter_python

        return tree_sitter_python.language()
    if language == "javascript":
        import tree_sitter_javascript

        return tree_sitter_javascript.language()
    import tree_sitter_typescript

    return tree_sitter_typescript.language_tsx() if language == "tsx" else tree_sitter_typescript.language_typescript()


@lru_cache(maxsize=None)
def _parser(language: str):
    from tree_sitter import Language, Parser

    try:
        return Parser(Language(_grammar(language)))
    except (ImportError, ValueError) as exc:  # missing package, or an ABI the runtime can't load
        raise SymbolError(f"couldn't load the tree-sitter grammar for {language}: {exc}") from exc


_PY_DEFINITIONS = {"function_definition": "function", "class_definition": "class"}
_JS_DEFINITIONS = {
    "function_declaration": "function",
    "generator_function_declaration": "function",
    "class_declaration": "class",
    "abstract_class_declaration": "class",
    "method_definition": "method",
}
# `const f = () => {}`, `handler = function () {}` and class fields bound to functions.
_JS_BINDINGS = {"variable_declarator", "public_field_definition", "field_definition"}
_JS_FUNCTION_VALUES = {"arrow_function", "function_expression", "function", "generator_function"}
_JS_CLASS_VALUES = {"class"}
_TOKEN_RE = re.compile(r"\w+|[^\w\s]")


def _definition(node, in_class: bool, python: bool):
    """(name node, kind) if `node` defines a named function or class, else None."""
    if python:
        kind = _PY_DEFINITIONS.get(node.type)
        if kind is None:
            return None
        return node.child_by_field_name("name"), "method" if kind == "function" and in_class else kind

    if node.type in _JS_DEFINITIONS:
        if node.type == "method_definition" and not in_class:
            return None  # object-literal methods aren't addressable symbols
        name = node.child_by_field_name("name")
        kind = _JS_DEFINITIONS[node.type]
        return (name, kind) if name is not None else None
    if node.type in _JS_BINDINGS:
        name = node.child_by_field_name("name") or node.child_by_field_name("property")
        value = node.child_by_field_name("value")
        if name is None or value is None:
            return None
        if value.type in _JS_FUNCTION_VALUES:
            return name, "method" if in_class else "function"
        if value.type in _JS_CLASS_VALUES:
            return name, "class"
    return None


def _trim_trailing_comments(text: str) -> str:
    """Python blocks end at their last statement, but tree-sitter keeps trailing comments in them."""
    lines = text.split("\n")
    while len(lines) > 1 and (not lines[-1].strip() or lines[-1].lstrip().startswith("#")):
        lines.pop()
    return "\n".join(lines)


def _looks_minified(path: str, source: bytes) -> bool:
    """Bundles are full of one-letter functions that mean nothing to anyone asking `why`."""
    if ".min." in PurePosixPath(path).name.lower():
        return True
    return len(source) > MINIFIED_MIN_BYTES and len(source) / (source.count(b"\n") + 1) > MINIFIED_MEAN_LINE


def extract_symbols(path: str, source: bytes) -> list[ParsedSymbol]:
    """Every named function, method and class in one file, in source order."""
    language = language_for(path)
    if language is None or len(source) > MAX_FILE_BYTES or _looks_minified(path, source):
        return []
    python = language == "python"
    tree = _parser(language).parse(source)
    found: list[ParsedSymbol] = []
    # Lines come from byte offsets, never from Node.start_point: with the
    # tree-sitter 0.26 binding on Windows, reading start_point returns garbage
    # rows and then crashes the process (access violation), whatever the grammar.
    newlines = [m.start() for m in re.finditer(b"\n", source)]

    def line_of(byte: int) -> int:
        return bisect.bisect_left(newlines, byte) + 1

    def visit(node, scope: tuple[str, ...], in_class: bool) -> None:
        for child in node.named_children:
            definition = _definition(child, in_class, python)
            if definition is None:
                # Descend through exports, decorators, blocks and conditionals,
                # keeping class context (a class body's `if` still holds methods).
                visit(child, scope, in_class and child.type not in ("function_definition",))
                continue
            name_node, kind = definition
            name = source[name_node.start_byte:name_node.end_byte].decode("utf-8", errors="replace")
            # The definition with its own name cut out: renames keep the fingerprint.
            text = (source[child.start_byte:name_node.start_byte] + source[name_node.end_byte:child.end_byte]
                    ).decode("utf-8", errors="replace")
            if python:
                text = _trim_trailing_comments(text)
            normalized = " ".join(text.split())
            family = "class" if kind == "class" else "function"
            local_name = ".".join((*scope, name))
            found.append(ParsedSymbol(
                local_name=local_name,
                kind=kind,
                start_line=line_of(child.start_byte),
                end_line=line_of(child.start_byte) + text.count("\n"),
                fingerprint=hashlib.sha256(f"{family}\0{normalized}".encode()).hexdigest()[:32],
                tokens=tuple(_TOKEN_RE.findall(normalized)),
            ))
            visit(child, (*scope, name), kind == "class")

    visit(tree.root_node, (), False)
    return found


# --------------------------------------------------------------------------- #
# Walking history
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Change:
    old_path: str | None  # None when added
    new_path: str | None  # None when deleted


@dataclass
class _Live:
    """A symbol as it exists at the commit the walk has reached."""

    symbol_id: str
    path: str
    local_name: str
    kind: str
    fingerprint: str
    tokens: tuple[str, ...] | None  # None when restored from the database (no fuzzy matching)


@dataclass
class SymbolIngestResult:
    commits: int = 0
    created: int = 0
    versions: int = 0
    renamed: int = 0
    deleted: int = 0
    rebuilt: bool = False


_C_ESCAPES = {'"': '"', "\\": "\\", "a": "\a", "b": "\b", "t": "\t", "n": "\n", "v": "\v", "f": "\f", "r": "\r"}
_OCTAL_RE = re.compile(r"[0-7]{3}")


def _unquote(path: str) -> str:
    """git C-quotes paths with control characters, quotes or backslashes, even with core.quotepath=off."""
    if not (len(path) >= 2 and path[0] == path[-1] == '"'):
        return path
    body, out, i = path[1:-1], bytearray(), 0
    while i < len(body):
        if body[i] == "\\" and i + 1 < len(body):
            if _OCTAL_RE.match(body, i + 1):  # a raw byte, e.g. part of a UTF-8 sequence
                out.append(int(body[i + 1:i + 4], 8))
                i += 4
            else:
                out += _C_ESCAPES.get(body[i + 1], body[i + 1]).encode("utf-8")
                i += 2
        else:
            out += body[i].encode("utf-8")
            i += 1
    return out.decode("utf-8", errors="replace")


def first_parent_changes(repo: Path | str, since: str | None = None) -> Iterator[tuple[str, list[Change]]]:
    """(sha, changed files) for each first-parent commit after `since`, oldest first."""
    args = ["-c", "log.showRoot=true", "log", "--first-parent", "--reverse", "--diff-merges=first-parent",
            "-M", "--name-status", "--format=%x01%H", "--no-color", f"{since}..HEAD" if since else "HEAD", "--"]
    sha: str | None = None
    changes: list[Change] = []
    for raw in git.stream(repo, *args, sep=b"\n"):
        line = raw.decode("utf-8", errors="replace").rstrip("\r")
        if line.startswith("\x01"):
            if sha is not None:
                yield sha, changes
            sha, changes = line[1:].strip(), []
        elif line.strip() and sha is not None:
            status, *paths = line.split("\t")
            paths = [_unquote(p) for p in paths]
            letter = status[:1]
            if letter == "D":
                changes.append(Change(paths[0], None))
            elif letter == "R":
                changes.append(Change(paths[0], paths[1]))
            elif letter == "C":
                changes.append(Change(None, paths[1]))
            elif letter == "A":
                changes.append(Change(None, paths[0]))
            else:  # M, T
                changes.append(Change(paths[0], paths[0]))
    if sha is not None:
        yield sha, changes


def _new_symbol_id(sha: str, path: str, symbol: ParsedSymbol, occurrence: int = 0) -> str:
    # Deterministic, so rebuilding the index keeps ids that shared events refer to.
    # `occurrence` separates same-named definitions on one line (`function a(){} function a(){}`).
    key = f"{sha}\0{path}\0{symbol.local_name}\0{symbol.start_line}"
    if occurrence:
        key += f"\0{occurrence}"
    return hashlib.sha256(key.encode()).hexdigest()[:24]


def _family(kind: str) -> str:
    return "class" if kind == "class" else "function"


def _restore(conn: sqlite3.Connection) -> dict[str, list[_Live]]:
    live: dict[str, list[_Live]] = defaultdict(list)
    for symbol, version in store.current_symbol_versions(conn):
        prefix = module_name(version.file_path) + "."
        name = version.qualified_name or symbol.qualified_name
        local = name[len(prefix):] if name.startswith(prefix) else name
        live[version.file_path].append(
            _Live(symbol.id, version.file_path, local, symbol.kind, version.body_hash, None))
    return live


class _Walker:
    def __init__(self, conn: sqlite3.Connection, blobs: git.BlobReader, live: dict[str, list[_Live]],
                 result: SymbolIngestResult) -> None:
        self.conn, self.blobs, self.live, self.result = conn, blobs, live, result
        self.created_at: Counter[tuple[str, str, str, int]] = Counter()

    def apply(self, sha: str, changes: list[Change]) -> None:
        self.created_at.clear()  # positions only need to be unique within one commit
        old_pool: list[_Live] = []
        new_pool: list[tuple[str, ParsedSymbol]] = []
        matched: list[tuple[_Live, str, ParsedSymbol]] = []

        # Pass 1: same name in the same file.
        for change in changes:
            previous = self.live.pop(change.old_path, []) if change.old_path else []
            if change.new_path is None or language_for(change.new_path) is None:
                old_pool += previous
                continue
            source = self.blobs.read(sha, change.new_path)
            parsed = extract_symbols(change.new_path, source) if source is not None else []
            by_name: dict[str, list[_Live]] = defaultdict(list)
            for symbol in previous:
                by_name[symbol.local_name].append(symbol)
            for symbol in parsed:
                same_name = by_name.get(symbol.local_name)
                if same_name:
                    matched.append((same_name.pop(0), change.new_path, symbol))
                else:
                    new_pool.append((change.new_path, symbol))
            old_pool += [s for group in by_name.values() for s in group]

        # Pass 2: same fingerprint anywhere in this commit (renamed or moved).
        by_fingerprint: dict[tuple[str, str], list[_Live]] = defaultdict(list)
        for symbol in old_pool:
            by_fingerprint[(_family(symbol.kind), symbol.fingerprint)].append(symbol)
        unmatched: list[tuple[str, ParsedSymbol]] = []
        for path, symbol in new_pool:
            same_body = by_fingerprint.get((_family(symbol.kind), symbol.fingerprint), [])
            old = next((o for o in same_body if self._same_body_is_evidence(o, path, symbol)), None)
            if old is not None:
                same_body.remove(old)
                old_pool.remove(old)
                matched.append((old, path, symbol))
            else:
                unmatched.append((path, symbol))

        # Pass 3: renamed and edited.
        for path, symbol in unmatched:
            old = self._most_similar(symbol, old_pool)
            if old is not None:
                old_pool.remove(old)
                matched.append((old, path, symbol))
            else:
                self._create(sha, path, symbol)

        for old, path, symbol in matched:
            self._carry(sha, old, path, symbol)
        for old in old_pool:
            store.close_symbol_version(self.conn, old.symbol_id, sha)
            self.result.deleted += 1

    @staticmethod
    def _same_body_is_evidence(old: _Live, path: str, symbol: ParsedSymbol) -> bool:
        """Identical long bodies are the same code. Identical one-liners often aren't.

        `def set(self): self._flag = True` in one class and `def release(self): ...` in
        another can match by coincidence, so a tiny body only proves a rename within
        the same file and scope, or a move that keeps its name.
        """
        if len(symbol.tokens) >= MIN_FUZZY_TOKENS:
            return True
        old_scope, _, old_name = old.local_name.rpartition(".")
        new_scope, _, new_name = symbol.local_name.rpartition(".")
        return old_name == new_name or (old.path == path and old_scope == new_scope)

    @staticmethod
    def _most_similar(symbol: ParsedSymbol, candidates: list[_Live]) -> _Live | None:
        if len(symbol.tokens) < MIN_FUZZY_TOKENS:
            return None
        best, best_score = None, SIMILARITY
        for old in candidates:
            if old.tokens is None or _family(old.kind) != _family(symbol.kind) or len(old.tokens) < MIN_FUZZY_TOKENS:
                continue
            matcher = SequenceMatcher(None, old.tokens, symbol.tokens, autojunk=False)
            if matcher.real_quick_ratio() < best_score or matcher.quick_ratio() < best_score:
                continue
            score = matcher.ratio()
            if score >= best_score:
                best, best_score = old, score
        return best

    def _record(self, sha: str, symbol_id: str, path: str, symbol: ParsedSymbol) -> None:
        store.record_symbol_version(self.conn, SymbolVersion(
            symbol_id, path, symbol.start_line, symbol.end_line, symbol.fingerprint, sha,
            qualified_name=qualify(path, symbol.local_name),
        ))
        self.result.versions += 1

    def _create(self, sha: str, path: str, symbol: ParsedSymbol) -> None:
        position = (sha, path, symbol.local_name, symbol.start_line)
        symbol_id = _new_symbol_id(sha, path, symbol, self.created_at[position])
        self.created_at[position] += 1
        store.upsert_symbol(self.conn, Symbol(symbol_id, symbol.kind, qualify(path, symbol.local_name),
                                              symbol_language(path)))
        self._record(sha, symbol_id, path, symbol)
        self.live.setdefault(path, []).append(
            _Live(symbol_id, path, symbol.local_name, symbol.kind, symbol.fingerprint, symbol.tokens))
        self.result.created += 1

    def _carry(self, sha: str, old: _Live, path: str, symbol: ParsedSymbol) -> None:
        renamed = old.local_name != symbol.local_name or old.path != path
        if renamed or old.kind != symbol.kind:
            store.upsert_symbol(self.conn, Symbol(old.symbol_id, symbol.kind, qualify(path, symbol.local_name),
                                                  symbol_language(path)))
        if renamed or old.fingerprint != symbol.fingerprint:
            self._record(sha, old.symbol_id, path, symbol)
            self.result.renamed += renamed
        self.live.setdefault(path, []).append(
            _Live(old.symbol_id, path, symbol.local_name, symbol.kind, symbol.fingerprint, symbol.tokens))


def ingest_symbols(conn: sqlite3.Connection, repo: Path | str = ".") -> SymbolIngestResult:
    """Bring symbol history up to HEAD. Resumes from the last indexed commit; safe to re-run."""
    result = SymbolIngestResult()
    root = git.repo_root(repo)
    if not git.has_commits(root):
        return result

    since = store.get_meta(conn, INDEXED_THROUGH)
    if since and not git.is_ancestor(root, since):
        # History was rewritten (rebase, reset, branch switch): rebuild from scratch.
        # Symbol ids are deterministic, so links from events survive the rebuild.
        store.clear_symbol_versions(conn)
        since = None
        result.rebuilt = True
    live = _restore(conn) if since else defaultdict(list)

    with git.BlobReader(root) as blobs:
        walker = _Walker(conn, blobs, live, result)
        for sha, changes in first_parent_changes(root, since):
            with store.batch(conn):  # a commit's symbols land together, with the resume point
                walker.apply(sha, changes)
                store.set_meta(conn, INDEXED_THROUGH, sha)
            result.commits += 1
    return result

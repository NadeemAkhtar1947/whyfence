"""The symbols stage must stay fast without missing a single definition.

On flask, walking every named node of every parsed file was 52% of ingest
(7.9M calls for 3,161 parses). The walker now skips what can't hold a
definition. `reference_extract` below is the old walker, kept verbatim as the
judge: on every input, the fast one must find exactly the same symbols.
"""

import bisect
import hashlib
import re
from pathlib import Path

import pytest

from whyfence.ingest import symbols
from whyfence.ingest.symbols import (
    ParsedSymbol, _definition, _parser, _scope_only, _TOKEN_RE, _trim_trailing_comments, extract_symbols,
    language_for,
)

ROOT = Path(__file__).resolve().parents[1]


def reference_extract(path: str, source: bytes) -> list[ParsedSymbol]:
    """The walker as it was in 0.3.0: every named node, no shortcuts."""
    language = language_for(path)
    python = language == "python"
    tree = _parser(language).parse(source)
    found: list[ParsedSymbol] = []
    newlines = [m.start() for m in re.finditer(b"\n", source)]

    def line_of(byte: int) -> int:
        return bisect.bisect_left(newlines, byte) + 1

    def visit(node, scope, in_class):
        for child in node.named_children:
            named_scope = _scope_only(child, language, source)
            if named_scope is not None:
                visit(child, (*scope, named_scope[0]) if named_scope[0] else scope, named_scope[1])
                continue
            definition = _definition(child, in_class, language, source)
            if definition is None:
                visit(child, scope, in_class and child.type not in ("function_definition", "pair"))
                continue
            name_node, kind, owner = definition
            own_scope = (*scope, owner) if owner else scope
            name = source[name_node.start_byte:name_node.end_byte].decode("utf-8", errors="replace")
            text = (source[child.start_byte:name_node.start_byte] + source[name_node.end_byte:child.end_byte]
                    ).decode("utf-8", errors="replace")
            if python:
                text = _trim_trailing_comments(text)
            normalized = " ".join(text.split())
            family = "class" if kind == "class" else "function"
            found.append(ParsedSymbol(
                local_name=".".join((*own_scope, name)), kind=kind,
                start_line=line_of(child.start_byte), end_line=line_of(child.start_byte) + text.count("\n"),
                fingerprint=hashlib.sha256(f"{family}\0{normalized}".encode()).hexdigest()[:32],
                tokens=tuple(_TOKEN_RE.findall(normalized)),
            ))
            visit(child, (*own_scope, name), kind == "class")

    visit(tree.root_node, (), False)
    return found


SAMPLES = {
    "app/py_nested.py": (
        "import os\n\nif os.name == 'nt':\n    def win():\n        return 1\n"
        "else:\n    def posix():\n        return 2\n\n"
        "class A:\n    x = [i for i in range(3)]\n    try:\n        def m(self):\n            def inner():\n"
        "                return lambda y: y\n            return inner\n    except ImportError:\n        pass\n"
        "    with open('f') as f:\n        def w(self):\n            return f\n"
        "    match x:\n        case [1]:\n            def c(self):\n                return 1\n\n"
        "@decorator(arg=lambda: 1)\nasync def handler(request):\n    async with lock:\n        for i in range(3):\n"
        "            while True:\n                class Local:\n                    def go(self):\n"
        "                        return {'k': [v for v in i]}\n"
    ),
    "web/app.js": (
        "export function a() { return [1, 2].map(x => x * 2) }\n"
        "describe('x', () => { function helper() { return 1 } const inner = () => 2 })\n"
        "const C = class Named { m() { return { k: function () {} } } }\n"
        "(function iife() { function hidden() {} })()\n"
        "class B extends Base { static s = () => 1; get g() { return 1 } }\n"
    ),
    "web/types.ts": (
        "export abstract class Repo<T> { abstract find(id: string): Promise<T>; all = async (): Promise<T[]> => [] }\n"
        "namespace NS { export function f(): number { return 1 } }\n"
    ),
    "pkg/s.go": (
        "package p\ntype S struct{ x int }\nfunc (s *S) Get() int { return s.x }\n"
        "func New() *S { go func() { type local struct{} }(); return &S{} }\n"
    ),
    "src/lib.rs": "mod m { pub struct T; impl T { fn new() -> T { T } } }\nfn f() { let c = |x: i32| x; }\n",
    "src/A.java": (
        "class A { void m() { Runnable r = new Runnable() { public void run() {} }; } "
        "interface I { void x(); } enum E { ONE; void e() {} } }\n"
    ),
}


def own_python_files() -> list[Path]:
    return sorted(p for p in (ROOT / "src").rglob("*.py")) + sorted((ROOT / "tests").glob("*.py"))


@pytest.mark.parametrize("path", sorted(SAMPLES))
def test_same_symbols_as_the_full_walk_in_every_language(path):
    source = SAMPLES[path].encode()
    assert extract_symbols(path, source) == reference_extract(path, source)
    assert extract_symbols(path, source)  # the sample really has definitions


def test_same_symbols_as_the_full_walk_on_real_python():
    files = own_python_files()
    assert len(files) > 20
    for file in files:
        source = file.read_bytes()
        rel = file.relative_to(ROOT).as_posix()
        assert extract_symbols(rel, source) == reference_extract(rel, source), rel


def test_python_walk_skips_expressions(monkeypatch):
    """Most named nodes are expressions; a Python def can never be inside one."""
    source = (ROOT / "src/whyfence/cli.py").read_bytes()
    calls = {"n": 0}
    original = symbols._definition

    def counting(*args):
        calls["n"] += 1
        return original(*args)

    monkeypatch.setattr(symbols, "_definition", counting)
    extract_symbols("src/whyfence/cli.py", source)
    fast = calls["n"]
    calls["n"] = 0
    reference = sum(1 for _ in _named_nodes(_parser("python").parse(source).root_node))
    assert fast * 10 < reference, (fast, reference)  # at least 10x fewer nodes inspected


def _named_nodes(node):
    for child in node.named_children:
        yield child
        yield from _named_nodes(child)


def test_fuzzy_matching_indexes_the_new_symbol_once(monkeypatch):
    import difflib

    body = "def {name}(a, b):\n" + "".join(f"    x{i} = compute(a, b, {i})\n" for i in range(12)) + "    return x0\n"
    new = extract_symbols("m.py", body.format(name="renamed").replace("x11 = compute", "x11 = calc").encode())[0]
    candidates = []
    for i in range(20):
        changed = body.format(name=f"f{i}")
        for line in range(i):  # candidate i differs from the new symbol in i more lines
            changed = changed.replace(f"(a, b, {line})", f"(b, a, {line}, extra)")
        old = extract_symbols("m.py", changed.encode())[0]
        candidates.append(symbols._Live(f"id{i}", "m.py", old.local_name, old.kind, old.fingerprint, old.tokens))
    indexed = {"n": 0}
    original = difflib.SequenceMatcher.set_seq2

    def counting(self, b):
        indexed["n"] += 1
        return original(self, b)

    monkeypatch.setattr(difflib.SequenceMatcher, "set_seq2", counting)
    best = symbols._Walker._most_similar(new, candidates)
    assert best is not None and best.symbol_id == "id0"  # f0 differs from `renamed` only in one call
    assert indexed["n"] == 1, indexed


def test_merged_branch_commits_are_diffed_in_one_git_process(tmp_path, monkeypatch):
    """flask: one `git diff` per merged-branch commit was most of mining (about 55 ms each)."""
    import os
    import subprocess
    from contextlib import closing

    from test_symbols import Repo

    from whyfence import queries, store
    from whyfence.ingest import events, git
    from whyfence.trust import Trust

    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    repo = Repo(tmp_path)
    code = "".join(f"def f{i}(x):\n    return x + {i}\n\n\n" for i in range(3))
    repo.commit("Add", {"m.py": code})
    repo.git("checkout", "-q", "-b", "feature")
    for i in range(3):
        code = code.replace(f"x + {i}", f"x + {i} + 1")
        repo.commit(f"fix: f{i} was off by one\n\nWhy: callers count from one.", {"m.py": code})
    repo.git("checkout", "-q", "main")
    repo.git("merge", "-q", "--no-ff", "-m", "Merge feature", "feature")

    diffs = []
    real_run = subprocess.run

    def is_patch_diff(args) -> bool:  # line ranges; the one file-list diff-tree is not counted here
        return ("diff" in args or "diff-tree" in args) and ("-p" in args or "-U0" in args)

    def counting_run(args, *a, **kw):
        if is_patch_diff(args):
            diffs.append(args)
        return real_run(args, *a, **kw)

    real_stream = git.stream

    def counting_stream(repo_path, *args, **kw):
        if is_patch_diff(args):
            diffs.append(args)
        return real_stream(repo_path, *args, **kw)

    monkeypatch.setattr(subprocess, "run", counting_run)
    monkeypatch.setattr(git, "stream", counting_stream)
    with closing(store.connect()) as conn:
        git.ingest_git(conn, repo.path)
        symbols.ingest_symbols(conn, repo.path)
        events.ingest_events(conn, repo.path, transcripts=False)
        linked = {i: [e["summary"] for e in queries.lookup_why(conn, f"f{i}", Trust(everyone=True))["events"]]
                  for i in range(3)}
    assert linked == {i: [f"fix: f{i} was off by one"] for i in range(3)}  # each fix links to its own function
    assert len(diffs) == 1, diffs

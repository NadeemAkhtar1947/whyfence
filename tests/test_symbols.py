import ast
import os
import shutil
import subprocess
from contextlib import closing
from pathlib import Path

import pytest

from whyfence import mcp_server, store
from whyfence.ingest import git, symbols
from whyfence.ingest.symbols import extract_symbols, ingest_symbols, module_name

PROJECT_ROOT = Path(__file__).resolve().parents[1]
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def names(path: str, code: str) -> list[tuple[str, str, int, int]]:
    return [(s.local_name, s.kind, s.start_line, s.end_line) for s in extract_symbols(path, code.encode())]


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #


def test_python_definitions_nesting_and_trailing_comments():
    code = (
        "import x\n"                         # 1
        "@decorator\n"                       # 2
        "def top(a, b):\n"                   # 3
        "    def inner():\n"                 # 4
        "        return a\n"                 # 5
        "    return inner\n"                 # 6
        "    # trailing comment\n"           # 7
        "\n"                                 # 8
        "class Cache:\n"                     # 9
        "    @property\n"                    # 10
        "    def size(self):\n"              # 11
        "        return 1\n"                 # 12
        "    @size.setter\n"                 # 13
        "    def size(self, v):\n"           # 14
        "        pass\n"                     # 15
        "    if DEBUG:\n"                    # 16
        "        def dump(self): ...\n"      # 17
        "    async def fetch(self): ...\n"   # 18
        "lam = lambda: 1\n"                  # 19
    )
    assert names("app/cache.py", code) == [
        ("top", "function", 3, 6),
        ("top.inner", "function", 4, 5),
        ("Cache", "class", 9, 18),
        ("Cache.size", "method", 11, 12),
        ("Cache.size", "method", 14, 15),
        ("Cache.dump", "method", 17, 17),
        ("Cache.fetch", "method", 18, 18),
    ]


def test_javascript_functions_classes_and_bound_functions():
    code = (
        "export function load(id) { return id }\n"
        "const save = async (x) => {\n"
        "  const helper = function () {}\n"
        "}\n"
        "function* ids() {}\n"
        "export default function () {}\n"
        "const config = { method() {} }\n"
        "class Store extends Base {\n"
        "  handler = () => 1\n"
        "  get(key) { return key }\n"
        "}\n"
        "const Lazy = class {}\n"
    )
    assert names("web/store.js", code) == [
        ("load", "function", 1, 1),
        ("save", "function", 2, 4),
        ("save.helper", "function", 3, 3),
        ("ids", "function", 5, 5),
        ("Store", "class", 8, 11),
        ("Store.handler", "method", 9, 9),
        ("Store.get", "method", 10, 10),
        ("Lazy", "class", 12, 12),
    ]


def test_typescript_and_tsx():
    ts = (
        "export abstract class Repo<T> {\n"
        "  private cache = new Map<string, T>()\n"
        "  abstract find(id: string): Promise<T>\n"
        "  protected key = (id: string): string => `k:${id}`\n"
        "  async all(): Promise<T[]> { return [] }\n"
        "}\n"
        "interface Shape { area(): number }\n"
        "export const area = (s: Shape): number => s.area()\n"
    )
    assert names("src/repo.ts", ts) == [
        ("Repo", "class", 1, 6),
        ("Repo.key", "method", 4, 4),
        ("Repo.all", "method", 5, 5),
        ("area", "function", 8, 8),
    ]
    tsx = "export function Button({ label }: Props) {\n  return <button>{label}</button>\n}\n"
    assert names("ui/Button.tsx", tsx) == [("Button", "function", 1, 3)]


def test_fingerprint_ignores_name_and_whitespace_but_not_code():
    def fingerprint(code: str) -> str:
        return extract_symbols("a.py", code.encode())[0].fingerprint

    base = fingerprint("def refresh(token):\n    return renew(token)\n")
    assert fingerprint("def renew_token(token):\n    return  renew( token)\n") != base  # spacing inside tokens
    assert fingerprint("def renew_token(token):\n        return renew(token)\n") == base
    assert fingerprint("def refresh(token):\n    return renew(token, force=True)\n") != base
    assert extract_symbols("a.py", b"class refresh:\n    x = 1\n")[0].fingerprint != \
        extract_symbols("a.py", b"def refresh():\n    x = 1\n")[0].fingerprint


def test_module_names():
    assert module_name("src/whyfence/linter.py") == "whyfence.linter"
    assert module_name("app/__init__.py") == "app"
    assert module_name("lib/util.py") == "util"
    assert module_name("web/src/api/client.ts") == "web/src/api/client"


def test_unquotes_git_c_style_paths():
    assert symbols._unquote('"tab\\there.py"') == "tab\there.py"
    assert symbols._unquote('"caf\\303\\251.py"') == "café.py"
    assert symbols._unquote('plain.py') == "plain.py"


def test_unsupported_and_huge_files_are_skipped():
    assert extract_symbols("notes.md", b"def x(): pass") == []
    assert extract_symbols("big.js", b"function f() {}\n" * 70_000) == []


# --------------------------------------------------------------------------- #
# History walk on a synthetic repository
# --------------------------------------------------------------------------- #

REFRESH = '''\
def refresh(token, *, leeway=60):
    """Renew a token shortly before it expires."""
    if token.expires_in > leeway:
        return token
    fresh = client.renew(token.refresh_token)
    audit.log("refreshed", user=token.user, scopes=sorted(token.scopes))
    return fresh
'''


class Repo:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.git("init", "-q", "-b", "main")
        self.day = 0

    def git(self, *args: str) -> str:
        env = {**os.environ, "GIT_AUTHOR_NAME": "Ada", "GIT_AUTHOR_EMAIL": "ada@example.com",
               "GIT_COMMITTER_NAME": "Ada", "GIT_COMMITTER_EMAIL": "ada@example.com"}
        return subprocess.run(["git", "-C", str(self.path), *args], check=True, capture_output=True,
                              text=True, encoding="utf-8", env=env).stdout.strip()

    def write(self, files: dict[str, str | None]) -> None:
        for name, content in files.items():
            target = self.path / name
            if content is None:
                target.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")

    def commit(self, message: str, files: dict[str, str | None] | None = None) -> str:
        if files:
            self.write(files)
        self.day += 1
        date = f"2026-02-{self.day:02d}T12:00:00+00:00"
        self.git("add", "-A")
        os.environ["GIT_AUTHOR_DATE"] = os.environ["GIT_COMMITTER_DATE"] = date
        try:
            self.git("commit", "-q", "-m", message)
        finally:
            del os.environ["GIT_AUTHOR_DATE"], os.environ["GIT_COMMITTER_DATE"]
        return self.git("rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    return Repo(tmp_path)


def index(repo: Repo, conn) -> symbols.SymbolIngestResult:
    git.ingest_git(conn, repo.path)
    return ingest_symbols(conn, repo.path)


def timeline(conn, name: str) -> list[tuple]:
    (symbol,) = store.find_symbols(conn, name)
    return [(v.qualified_name, v.file_path, v.start_line, v.valid_from_sha, v.valid_to_sha)
            for v in store.symbol_history(conn, symbol.id)]


@needs_git
def test_follows_symbols_through_edits_renames_moves_and_deletes(repo):
    c1 = repo.commit("add auth", {"app/auth.py": REFRESH + "\n\ndef helper(x):\n    return x * 2\n"})
    c2 = repo.commit("rename helper, no body change", {
        "app/auth.py": REFRESH + "\n\ndef double(x):\n    return x * 2\n"})
    c3 = repo.commit("unrelated line shift", {"app/auth.py": "import os\n\n" + REFRESH +
                                              "\n\ndef double(x):\n    return x * 2\n"})
    repo.git("mv", "app/auth.py", "app/session.py")
    c4 = repo.commit("rename the file")
    c5 = repo.commit("move refresh to its own module", {
        "app/session.py": "import os\n\n\ndef double(x):\n    return x * 2\n",
        "app/tokens.py": REFRESH})
    renamed = REFRESH.replace("def refresh(", "def refresh_token(").replace("sorted(token.scopes)",
                                                                             "sorted(token.scopes), ok=True")
    c6 = repo.commit("rename and edit at once", {"app/tokens.py": renamed})
    c7 = repo.commit("drop double", {"app/session.py": "import os\n"})

    with closing(store.connect()) as conn:
        result = index(repo, conn)
        assert result.commits == 7

        assert timeline(conn, "app.tokens.refresh_token") == [
            ("app.auth.refresh", "app/auth.py", 1, c1, c4),
            ("app.session.refresh", "app/session.py", 3, c4, c5),   # file renamed
            ("app.tokens.refresh", "app/tokens.py", 1, c5, c6),     # moved across files
            ("app.tokens.refresh_token", "app/tokens.py", 1, c6, None),  # renamed and edited
        ]
        # A rename with an identical body keeps the id. Pure line shifts (c3, and c5 when
        # refresh moved out from above it) add no version; c7 deletes it.
        assert timeline(conn, "app.session.double") == [
            ("app.auth.helper", "app/auth.py", 10, c1, c2),
            ("app.auth.double", "app/auth.py", 10, c2, c4),
            ("app.session.double", "app/session.py", 12, c4, c7),
        ]
        assert store.count_symbols(conn) == 2


@needs_git
def test_small_lookalike_functions_are_not_fuzzy_matched(repo):
    c1 = repo.commit("one", {"a.py": "def get_a(self):\n    return self.a\n"})
    c2 = repo.commit("two", {"a.py": "def get_b(self):\n    return self.b\n"})
    with closing(store.connect()) as conn:
        index(repo, conn)
        assert timeline(conn, "a.get_a") == [("a.get_a", "a.py", 1, c1, c2)]
        assert timeline(conn, "a.get_b") == [("a.get_b", "a.py", 1, c2, None)]


@needs_git
def test_javascript_rename_in_history(repo):
    c1 = repo.commit("js", {"web/api.js": "export const fetchUser = async (id) => {\n  return http.get(`/u/${id}`)\n}\n"})
    c2 = repo.commit("rename", {"web/api.js": "export const loadUser = async (id) => {\n  return http.get(`/u/${id}`)\n}\n"})
    with closing(store.connect()) as conn:
        index(repo, conn)
        assert [v[0] for v in timeline(conn, "loadUser")] == ["web/api.fetchUser", "web/api.loadUser"]
        assert timeline(conn, "loadUser")[1][3] == c2


@needs_git
def test_merges_are_indexed_along_the_first_parent(repo):
    repo.commit("base", {"a.py": "def a():\n    return 1\n"})
    repo.git("checkout", "-q", "-b", "feature")
    side = repo.commit("feature", {"b.py": "def b():\n    return 2\n"})
    repo.git("checkout", "-q", "main")
    repo.commit("main", {"a.py": "def a():\n    return 10\n"})
    repo.git("merge", "-q", "--no-ff", "feature", "-m", "merge")
    merge = repo.git("rev-parse", "HEAD")
    with closing(store.connect()) as conn:
        index(repo, conn)
        ((_, path, _, introduced, _),) = timeline(conn, "b.b")
        assert path == "b.py" and introduced == merge != side


@needs_git
def test_resumes_incrementally_and_rebuilds_after_rewrites(repo):
    repo.commit("one", {"a.py": REFRESH})
    with closing(store.connect()) as conn:
        assert index(repo, conn).commits == 1
        c2 = repo.commit("two", {"a.py": REFRESH.replace("leeway=60", "leeway=30")})
        resumed = index(repo, conn)
        assert (resumed.commits, resumed.versions, resumed.rebuilt) == (1, 1, False)
        assert [v[3] for v in timeline(conn, "a.refresh")][-1] == c2

        repo.git("reset", "-q", "--hard", "HEAD~1")
        c3 = repo.commit("rewritten", {"a.py": REFRESH.replace("leeway=60", "leeway=90")})
        rebuilt = index(repo, conn)
        assert rebuilt.rebuilt and rebuilt.commits == 2
        shas = [v[3] for v in timeline(conn, "a.refresh")]
        assert c2 not in shas and shas[-1] == c3
        assert index(repo, conn).commits == 0


# --------------------------------------------------------------------------- #
# End to end: whyfence's own history
# --------------------------------------------------------------------------- #

# Real definitions from this project, checked at every commit of its history.
OWN_SYMBOLS = [
    ("src/whyfence/linter.py", "discover"),
    ("src/whyfence/linter.py", "check"),
    ("src/whyfence/linter.py", "_segments"),
    ("src/whyfence/linter.py", "Finding.failed"),
    ("src/whyfence/store.py", "connect"),
    ("src/whyfence/cli.py", "main"),
    ("src/whyfence/mcp_server.py", "lookup_history"),
]


def run_git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(PROJECT_ROOT), *args], check=True, capture_output=True,
                          text=True, encoding="utf-8").stdout


def expected_versions(path: str, local_name: str) -> list[tuple[str, int, int]]:
    """Where `local_name` changed along HEAD's first-parent history, according to Python's own ast."""
    versions, previous = [], None
    for sha in run_git("rev-list", "--first-parent", "--reverse", "HEAD").split():
        try:
            source = run_git("show", f"{sha}:{path}")
        except subprocess.CalledProcessError:
            previous = None
            continue
        scope = ast.parse(source).body
        node = None
        for part in local_name.split("."):
            node = next((n for n in scope if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                         and n.name == part), None)
            if node is None:
                break
            scope = node.body
        if node is None:
            previous = None
            continue
        key = " ".join(ast.get_source_segment(source, node).split())
        if key != previous:
            versions.append((sha, node.lineno, node.end_lineno))
        previous = key
    return versions


@needs_git
def test_indexes_whyfence_own_history(tmp_path):
    if not git.has_commits(PROJECT_ROOT):
        pytest.skip("not running from a git checkout with commits")
    with closing(store.connect(tmp_path / "index.db")) as conn:
        git.ingest_git(conn, PROJECT_ROOT)
        try:
            ingest_symbols(conn, PROJECT_ROOT)
        except symbols.SymbolError as exc:
            pytest.skip(f"tree-sitter grammars unavailable: {exc}")

        for path, local_name in OWN_SYMBOLS:
            qualified = f"{module_name(path)}.{local_name}"
            expected = expected_versions(path, local_name)
            assert expected, f"{qualified} should exist in this project's history"
            (symbol,) = store.find_symbols(conn, qualified)
            recorded = store.symbol_history(conn, symbol.id)
            assert [(v.valid_from_sha, v.start_line, v.end_line) for v in recorded] == expected, qualified
            # Each version ends where the next begins; the last is current.
            assert [v.valid_to_sha for v in recorded] == [v.valid_from_sha for v in recorded[1:]] + [None]

        # discover() gained --exclude support after the first commit, so it has real history.
        assert len(expected_versions("src/whyfence/linter.py", "discover")) >= 2

        # And the MCP tool reports the same timeline, with commit metadata.
        answer = mcp_server.lookup_history(conn, "whyfence.linter.discover")
        first = answer["versions"][0]
        root_commit = run_git("rev-list", "--max-parents=0", "HEAD").split()[0]
        assert first["valid_from"]["sha"] == root_commit
        assert first["valid_from"]["subject"].startswith("Initial commit")
        assert answer["versions"][-1]["current"] is True

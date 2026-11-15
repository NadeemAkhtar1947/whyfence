"""JavaScript and TypeScript definitions written as assignments, and TypeScript types.

express defines almost everything as `res.status = function status(code) {...}`:
lib/response.js had 9 indexed symbols (all nested helpers), none of its API.
"""

from contextlib import closing

from test_symbols import Repo, needs_git, repo  # noqa: F401  (fixture)

from whyfence import queries, store
from whyfence.ingest import git, symbols
from whyfence.ingest.symbols import extract_symbols


def names(path: str, code: str) -> list[tuple[str, str]]:
    return [(s.local_name, s.kind) for s in extract_symbols(path, code.encode())]


def test_member_assignments_define_functions_and_methods():
    code = (
        "var res = Object.create(http.ServerResponse.prototype)\n"
        "module.exports = res\n"
        "res.status = function status(code) { this.statusCode = code; return this }\n"
        "res.links = function (links) { return this }\n"
        "exports.compile = function (v) { return v }\n"
        "module.exports.helper = () => 1\n"
        "View.prototype.render = function render(options, callback) { return 1 }\n"
        "app.engines.cache = async function () {}\n"
        "Router.Route = class Route { dispatch() {} }\n"
    )
    assert names("lib/response.js", code) == [
        ("res.status", "method"),
        ("res.links", "method"),
        ("compile", "function"),
        ("helper", "function"),
        ("View.render", "method"),
        ("app.engines.cache", "method"),
        ("Router.Route", "class"),
        ("Router.Route.dispatch", "method"),
    ]


def test_module_exports_of_a_named_function_uses_its_name():
    assert names("lib/express.js", "module.exports = function createApplication() { return 1 }\n") == [
        ("createApplication", "function")]
    assert names("lib/x.js", "module.exports = function () {}\n") == []  # nothing to call it


def test_assignments_that_are_not_definitions_are_ignored():
    code = (
        "this.handler = function () {}\n"     # instance state, not an addressable symbol
        "obj[key] = function () {}\n"          # computed name
        "res.locals = Object.create(null)\n"   # not a function
        "res.get().x = function () {}\n"       # not a plain name
    )
    assert names("a.js", code) == []


def test_object_literal_methods_assigned_to_an_exported_name():
    code = (
        "exports.utils = {\n"
        "  parse(text) { return text },\n"
        "  format: function (v) { return v },\n"
        "  arrow: (v) => v,\n"
        "  size: 1,\n"
        "  nested: { inner() { return 1 } },\n"
        "}\n"
        "module.exports = { start() {}, stop: () => {} }\n"
        "const local = { notExported() {} }\n"
    )
    assert names("lib/utils.js", code) == [
        ("utils.parse", "method"),
        ("utils.format", "method"),
        ("utils.arrow", "method"),
        ("start", "method"),
        ("stop", "method"),
    ]


def test_typescript_types_and_namespaces():
    code = (
        "export interface Shape { area(): number }\n"
        "type Id = string | number\n"
        "export enum Color { Red, Green }\n"
        "namespace NS { export function f(): number { return 1 } namespace Inner { class C {} } }\n"
        "declare module 'pkg' { export function g(): void }\n"
    )
    assert names("src/types.ts", code) == [
        ("Shape", "class"),
        ("Id", "class"),
        ("Color", "class"),
        ("NS.f", "function"),
        ("NS.Inner.C", "class"),
    ]


@needs_git
def test_assigned_methods_keep_their_identity_when_moved(repo):  # noqa: F811
    body = "function status(code) {\n" + "".join(f"  this.v{i} = code + {i}\n" for i in range(8)) + "  return this\n}"
    repo.commit("Add status", {"lib/response.js": f"res.status = {body}\n"})
    repo.commit("Move to a new file", {"lib/response.js": None, "lib/res.js": f"res.status = {body}\n"})
    with closing(store.connect()) as conn:
        git.ingest_git(conn, repo.path)
        symbols.ingest_symbols(conn, repo.path)
        answer = queries.lookup_history(conn, "res.status")
    assert answer["status"] == "ok"
    assert [v["file"] for v in answer["versions"]] == ["lib/response.js", "lib/res.js"]


@needs_git
def test_an_index_from_an_older_extractor_is_rebuilt_and_relinked(repo):  # noqa: F811
    """Ingest resumes from the last indexed commit, so a better extractor must trigger a rebuild
    or upgraded users would never see `res.status` in their history."""
    from whyfence.ingest import events
    from whyfence.trust import Trust

    code = "res.status = function status(code) {\n  this.statusCode = code\n  return this\n}\n"
    repo.commit("Add status", {"lib/response.js": code})
    repo.commit("fix: status ignored 0\n\nWhy: callers send 0 for unknown.",
                {"lib/response.js": code.replace("= code", "= code || 500")})
    with closing(store.connect()) as conn:
        git.ingest_git(conn, repo.path)
        symbols.ingest_symbols(conn, repo.path)
        events.ingest_events(conn, repo.path, transcripts=False)
        store.set_meta(conn, symbols.EXTRACTOR_KEY, "0.3")  # what an older whyfence left behind
        conn.execute("DELETE FROM symbol_versions")
        conn.execute("DELETE FROM event_links")
        conn.commit()
        result = symbols.ingest_symbols(conn, repo.path)
        events.ingest_events(conn, repo.path, transcripts=False)
        assert result.rebuilt and result.upgraded
        assert store.get_meta(conn, symbols.EXTRACTOR_KEY) == symbols.EXTRACTOR_VERSION
        why = queries.lookup_why(conn, "res.status", Trust(everyone=True))
    assert [e["summary"] for e in why["events"]] == ["fix: status ignored 0"]

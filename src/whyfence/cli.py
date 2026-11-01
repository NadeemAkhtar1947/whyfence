"""Command-line entry point: `whyfence <command>`."""

from __future__ import annotations

import argparse
from contextlib import closing
from pathlib import Path

from rich.console import Console
from rich.markup import escape

from whyfence import linter, store
from whyfence.ingest import git, symbols

DEFAULT_DB = Path(".whyfence") / "index.db"


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'s' * (count != 1)}"


def _ingest(args: argparse.Namespace) -> int:
    console = Console(highlight=False)
    try:
        root = git.repo_root(args.repo)
        db_path = args.db or root / DEFAULT_DB
        with closing(store.connect(db_path)) as conn:
            commits = git.ingest_git(conn, root)
            with console.status("Indexing functions and classes…"):
                indexed = symbols.ingest_symbols(conn, root)
            total = store.count_commits(conn)
            symbol_count = store.count_symbols(conn)
    except (git.GitError, store.StoreError, symbols.SymbolError) as exc:
        console.print(f"[red]error:[/] {escape(str(exc))}")
        return 2
    console.print(f"Read {_plural(commits.seen, 'commit')} from {escape(root.name)}: "
                  f"[green]{commits.inserted} new[/], {total} indexed in {escape(str(db_path))}")
    if indexed.rebuilt:
        console.print("[yellow]History was rewritten since the last run; rebuilt symbol history.[/]")
    console.print(f"Walked {_plural(indexed.commits, 'commit')}: {_plural(indexed.created, 'new symbol')}, "
                  f"{_plural(indexed.versions, 'version')}, {indexed.renamed} renamed or moved, "
                  f"{indexed.deleted} deleted · {symbol_count} symbols indexed")
    return 0


def _default_db(repo: Path) -> Path:
    try:
        root = git.repo_root(repo)
    except git.GitError:
        root = repo
    return (root / DEFAULT_DB).resolve()


def _mcp(args: argparse.Namespace) -> int:
    from whyfence import mcp_server  # deferred: the MCP stack is slow to import

    mcp_server.serve(args.db.resolve() if args.db else _default_db(args.repo))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="whyfence",
        description="Temporal memory for coding agents: why the code is the way it is.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    linter.add_arguments(commands.add_parser(
        "lint",
        help="check that commands in CLAUDE.md / AGENTS.md still exist",
        description=linter.__doc__.splitlines()[0],
    ))

    ingest = commands.add_parser("ingest", help="index git history into the local database")
    ingest.add_argument("repo", nargs="?", type=Path, default=Path("."), help="repository (default: .)")
    ingest.add_argument("--db", type=Path, help=f"database path (default: <repo>/{DEFAULT_DB.as_posix()})")
    ingest.set_defaults(func=_ingest)

    mcp = commands.add_parser("mcp", help="serve the why/history tools to MCP clients over stdio")
    mcp.add_argument("--repo", type=Path, default=Path("."),
                     help="repository whose index to serve (default: .)")
    mcp.add_argument("--db", type=Path, help=f"database path (default: <repo>/{DEFAULT_DB.as_posix()})")
    mcp.set_defaults(func=_mcp)

    args = parser.parse_args(argv)
    return args.func(args)

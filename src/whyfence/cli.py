"""Command-line entry point: `whyfence <command>`."""

from __future__ import annotations

import argparse
from contextlib import closing
from pathlib import Path

from rich.console import Console
from rich.markup import escape

from whyfence import linter, store
from whyfence.ingest import git

DEFAULT_DB = Path(".whyfence") / "index.db"


def _ingest(args: argparse.Namespace) -> int:
    console = Console(highlight=False)
    try:
        root = git.repo_root(args.repo)
        db_path = args.db or root / DEFAULT_DB
        with closing(store.connect(db_path)) as conn:
            result = git.ingest_git(conn, root)
            total = store.count_commits(conn)
    except (git.GitError, store.StoreError) as exc:
        console.print(f"[red]error:[/] {escape(str(exc))}")
        return 2
    console.print(f"Read [bold]{result.seen}[/] commit{'s' * (result.seen != 1)} from {escape(root.name)}: "
                  f"[green]{result.inserted} new[/], {total} indexed in {escape(str(db_path))}")
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

    args = parser.parse_args(argv)
    return args.func(args)

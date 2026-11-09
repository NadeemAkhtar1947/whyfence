"""Command-line entry point: `whyfence <command>`."""

from __future__ import annotations

import argparse
import json
from contextlib import closing
from pathlib import Path

from rich.console import Console
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from whyfence import linter, store, team
from whyfence.console import harden_streams
from whyfence.ingest import events, git, symbols

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
            with console.status("Mining commits and transcripts for decisions…"):
                mined = events.ingest_events(conn, root, projects_dir=args.transcripts, codex_dir=args.codex_dir,
                                             gemini_dir=args.gemini_dir, transcripts=not args.no_transcripts)
                shared = team.import_shared(conn, root)
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
    console.print(f"Found {_plural(mined.commit_events, 'event')} in commit messages and "
                  f"{_plural(mined.transcript_events, 'event')} in {_plural(mined.sessions, 'agent session')}, "
                  f"with {_plural(mined.links, 'symbol link')}")
    if shared:
        console.print(f"Loaded {_plural(shared, 'event')} shared by your team from {team.EVENTS_DIR.as_posix()}/")
    return 0


def _share(args: argparse.Namespace) -> int:
    console = Console(highlight=False)
    db_path = args.db.resolve() if args.db else _default_db(args.repo)
    if not db_path.is_file():
        console.print(f"[red]error:[/] no whyfence index at {escape(str(db_path))}; run `whyfence ingest` first")
        return 2
    try:
        with closing(store.connect(db_path)) as conn:
            result = team.share(conn, args.repo, dry_run=args.dry_run)
    except git.GitError as exc:
        console.print(f"[red]error:[/] {escape(str(exc))}")
        return 2
    if not result.events:
        console.print("[grey50]Nothing new to share.[/]")
        return 0
    if args.dry_run:
        for event in result.events:
            console.print(f"[cyan]{event.kind}[/]  {escape(team.first_sentence(event.summary))}")
        console.print(f"\nWould add {_plural(len(result.events), 'event')} to {escape(str(result.path))}")
        return 0
    console.print(f"Added {_plural(len(result.events), 'event')} to {escape(str(result.path))}")
    console.print("Review the file before committing it: everything in it becomes visible to anyone with the repo.")
    return 0


def _default_db(repo: Path) -> Path:
    try:
        root = git.repo_root(repo)
    except git.GitError:
        root = repo
    return (root / DEFAULT_DB).resolve()


_KIND_STYLES = {
    "constraint": "bold magenta", "decision": "cyan", "correction": "yellow", "dead_end": "red",
    "incident": "bright_red", "revert": "bright_red",
}


def _lookup(args: argparse.Namespace, lookup) -> tuple[dict | None, int]:
    """Run a query against the index, or explain why it can't be answered."""
    console = Console(highlight=False, stderr=True)
    db_path = args.db.resolve() if args.db else _default_db(args.repo)
    if not db_path.is_file():
        console.print(f"[red]error:[/] no whyfence index at {escape(str(db_path))}; run `whyfence ingest` first")
        return None, 2
    with closing(store.connect(db_path)) as conn:
        answer = lookup(conn)
    if args.json:
        print(json.dumps(answer, indent=2, ensure_ascii=False))
    elif answer["status"] != "ok":
        console.print(escape(answer["message"]))
        for candidate in answer.get("candidates", []):
            console.print(f"  {escape(candidate)}")
    return (answer if answer["status"] == "ok" and not args.json else None), (0 if answer["status"] == "ok" else 1)


def _symbol_header(console: Console, symbol: dict) -> None:
    console.print(f"[bold]{escape(symbol['name'])}[/]  [grey50]{symbol['kind']} · {symbol['language']}[/]")


def _why(args: argparse.Namespace) -> int:
    from whyfence import queries

    answer, code = _lookup(args, lambda conn: queries.lookup_why(conn, args.symbol))
    if answer is None:
        return code
    console = Console(highlight=False)
    _symbol_header(console, answer["symbol"])
    if not answer["events"]:
        console.print(f"  [grey50]{escape(answer['message'])}[/]")
        return 0
    table = Table(box=None, show_header=False, padding=(0, 2, 0, 0), pad_edge=False)
    table.add_column(no_wrap=True)
    table.add_column(style="grey50", no_wrap=True)
    table.add_column(overflow="fold")
    for event in answer["events"]:
        source = event["source"] or {}
        origin = f"commit {event['commit'][:7]}" if event["commit"] else \
            " · ".join(filter(None, [source.get("kind"), source.get("agent")]))
        text = Text(event["summary"])
        text.append(f"\n{origin}" + (" · inferred" if event["confidence"] == "INFERRED" else ""), style="grey50")
        table.add_row(Text(event["kind"], style=_KIND_STYLES.get(event["kind"], "")), event["recorded_at"][:10], text)
    console.print(table)
    return 0


def _history(args: argparse.Namespace) -> int:
    from whyfence import queries

    answer, code = _lookup(args, lambda conn: queries.lookup_history(conn, args.symbol))
    if answer is None:
        return code
    console = Console(highlight=False)
    _symbol_header(console, answer["symbol"])
    table = Table(box=None, show_header=False, padding=(0, 2, 0, 0), pad_edge=False)
    for column in ({"style": "yellow", "no_wrap": True}, {"style": "grey50", "no_wrap": True},
                   {"no_wrap": True}, {"overflow": "fold"}):
        table.add_column(**column)
    for version in answer["versions"]:
        introduced = version["valid_from"] or {}
        start, end = version["lines"]
        subject = introduced.get("subject") or "(commit not indexed)"
        if version["current"]:
            subject += "  [current]"
        table.add_row(introduced.get("sha", "")[:7], (introduced.get("date") or "")[:10],
                      f"{version['file']}:{start}-{end}", Text(f"{version['name']}  ", style="bold").append(
                          subject, style="green" if version["current"] else ""))
    console.print(table)
    return 0


def _rules(args: argparse.Namespace) -> int:
    from whyfence import queries

    answer, code = _lookup(args, queries.lookup_rules)
    if answer is None:
        return code
    console = Console(highlight=False)
    if not answer["rules"]:
        console.print(f"[grey50]{escape(answer['message'])}[/]")
        return 0
    for rule in answer["rules"]:
        console.print(f"[bold magenta]•[/] {escape(rule['rule'])}")
        origin = " · ".join(filter(None, [rule["recorded_at"][:10], *(rule["about"] or [])]))
        console.print(f"  [grey50]{escape(origin)}[/]")
    return 0


def _mcp(args: argparse.Namespace) -> int:
    from whyfence import mcp_server  # deferred: the MCP stack is slow to import

    mcp_server.serve(args.db.resolve() if args.db else _default_db(args.repo))
    return 0


def main(argv: list[str] | None = None) -> int:
    harden_streams()
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
    ingest.add_argument("--transcripts", type=Path, metavar="DIR",
                        help="Claude Code projects folder, or one project's transcript folder "
                             "(default: ~/.claude/projects, or $CLAUDE_CONFIG_DIR/projects)")
    ingest.add_argument("--codex-dir", type=Path, metavar="DIR", help="Codex CLI home (default: ~/.codex, or $CODEX_HOME)")
    ingest.add_argument("--gemini-dir", type=Path, metavar="DIR", help="Gemini CLI home (default: ~/.gemini)")
    ingest.add_argument("--no-transcripts", action="store_true", help="don't read any agent session logs")
    ingest.set_defaults(func=_ingest)

    share = commands.add_parser("share", help="add what your agent sessions learned to the repo's shared event log")
    share.add_argument("repo", nargs="?", type=Path, default=Path("."), help="repository (default: .)")
    share.add_argument("--db", type=Path, help=f"database path (default: <repo>/{DEFAULT_DB.as_posix()})")
    share.add_argument("--dry-run", action="store_true", help="show what would be shared without writing anything")
    share.set_defaults(func=_share)

    for name, func, help_text in (
        ("why", _why, "show the decisions, rules and dead ends recorded against a function or class"),
        ("history", _history, "show how a function or class changed, commit by commit"),
        ("rules", _rules, "list every standing rule recorded for this repository"),
    ):
        query = commands.add_parser(name, help=help_text, description=help_text)
        if name != "rules":
            query.add_argument("symbol", help="qualified name (app.auth.refresh), bare name (refresh) or symbol id")
        query.add_argument("--repo", type=Path, default=Path("."), help="repository whose index to read (default: .)")
        query.add_argument("--db", type=Path, help=f"database path (default: <repo>/{DEFAULT_DB.as_posix()})")
        query.add_argument("--json", action="store_true", help="print the same JSON the MCP tool returns")
        query.set_defaults(func=func)

    mcp = commands.add_parser("mcp", help="serve the why/history tools to MCP clients over stdio")
    mcp.add_argument("--repo", type=Path, default=Path("."),
                     help="repository whose index to serve (default: .)")
    mcp.add_argument("--db", type=Path, help=f"database path (default: <repo>/{DEFAULT_DB.as_posix()})")
    mcp.set_defaults(func=_mcp)

    args = parser.parse_args(argv)
    return args.func(args)

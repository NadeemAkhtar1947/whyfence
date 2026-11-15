"""Command-line entry point: `whyfence <command>`."""

from __future__ import annotations

import argparse
import json
import sys
from contextlib import closing, contextmanager
from pathlib import Path

from rich.console import Console
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from whyfence import linter, store, team, trust
from whyfence._version import __version__
from whyfence.console import harden_streams
from whyfence.ingest import events, git, symbols

DEFAULT_DB = Path(".whyfence") / "index.db"


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'s' * (count != 1)}"


@contextmanager
def _stage_progress(console: Console):
    """One "N / total commits" bar per ingest stage; gone when done, so only the summary stays.

    Nothing is drawn when output isn't a terminal (CI, `> file`), so logs stay clean.
    """
    from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn

    columns = (TextColumn("{task.description}"), BarColumn(), MofNCompleteColumn(), TextColumn("commits"),
               TimeElapsedColumn())
    with Progress(*columns, console=console, transient=True, disable=not console.is_terminal) as bars:
        def stage(description: str):
            task = bars.add_task(description, total=None)

            def report(done: int, total: int) -> None:
                bars.update(task, completed=done, total=total or None)

            return report

        yield stage


def _ingest(args: argparse.Namespace) -> int:
    console = Console(highlight=False)
    try:
        root = git.repo_root(args.repo)
        db_path = args.db or root / DEFAULT_DB
        with closing(store.connect(db_path)) as conn, _stage_progress(console) as stage:
            commits = git.ingest_git(conn, root, progress=stage("Reading commits"))
            indexed = symbols.ingest_symbols(conn, root, progress=stage("Indexing functions and classes"))
            mined = events.ingest_events(conn, root, projects_dir=args.transcripts, codex_dir=args.codex_dir,
                                         gemini_dir=args.gemini_dir, transcripts=not args.no_transcripts,
                                         progress=stage("Mining commits for decisions"))
            shared = team.import_shared(conn, root)
            total = store.count_commits(conn)
            symbol_count = store.count_symbols(conn)
            who = trust.summary(conn, trust.load(root))
    except (git.GitError, store.StoreError, symbols.SymbolError) as exc:
        console.print(f"[red]error:[/] {escape(str(exc))}")
        return 2
    console.print(f"Read {_plural(commits.seen, 'commit')} from {escape(root.name)}: "
                  f"[green]{commits.inserted} new[/], {total} indexed in {escape(str(db_path))}")
    if indexed.upgraded:
        console.print("[yellow]This version of whyfence finds more kinds of definitions; "
                      "rebuilt symbol history and re-linked commit events.[/]")
    elif indexed.rebuilt:
        console.print("[yellow]History was rewritten since the last run; rebuilt symbol history.[/]")
    console.print(f"Walked {_plural(indexed.commits, 'commit')}: {_plural(indexed.created, 'new symbol')}, "
                  f"{_plural(indexed.versions, 'version')}, {indexed.renamed} renamed or moved, "
                  f"{indexed.deleted} deleted · {symbol_count} symbols indexed")
    console.print(f"Found {_plural(mined.commit_events, 'event')} in commit messages and "
                  f"{_plural(mined.transcript_events, 'event')} in {_plural(mined.sessions, 'agent session')}, "
                  f"with {_plural(mined.links, 'symbol link')}")
    if shared:
        console.print(f"Loaded {_plural(shared, 'event')} shared by your team from {team.EVENTS_DIR.as_posix()}/")
    _print_trust_summary(console, who)
    return 0


TOP_UNTRUSTED = 5


def _print_trust_summary(console: Console, who: "trust.TrustSummary") -> None:
    """Who wrote what's in the index, and the exact commands to trust them. Silent when all is trusted."""
    if who.trusted == who.total:
        return
    console.print(f"\n[bold]Trust:[/] {_plural(who.total, 'event')} found, {who.trusted} from authors you trust. "
                  "The others are kept, but agents get their rules and text only from authors you trust.")
    shown = who.untrusted_authors[:TOP_UNTRUSTED]
    if shown:
        # Command first and never wrapped, so it can be copied whole whatever the terminal width.
        commands = [f"whyfence trust {trust.email_of(author) or ''}" for author, _ in shown]
        width = max(len(command) for command in commands)
        console.print("[grey50]Most events by authors you don't trust yet:[/]")
        for command, (author, count) in zip(commands, shown):
            name = author.split(" <")[0]
            console.print(f"  [cyan]{escape(command):<{width}}[/]  {_plural(count, 'event'):>10}  "
                          f"[grey50]{escape(name)}[/]", soft_wrap=True)
        rest = who.untrusted_author_count - len(shown)
        if rest:
            console.print(f"[grey50]  and {_plural(rest, 'more author')} with fewer events.[/]")
    if who.github_merged:
        console.print(f"  [cyan]whyfence trust {trust.GITHUB_WEB_MERGE}[/]  {_plural(who.github_merged, 'event')} "
                      "claim an author you trust and a merge on github.com, whose committer name anyone "
                      "can write", soft_wrap=True)
    console.print("[grey50]Trust only people whose commits are reviewed before they reach your default branch.[/]")


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

    answer, code = _lookup(args, lambda conn: queries.lookup_why(
        conn, args.symbol, trust.load(args.repo), include_untrusted=args.include_untrusted, limit=args.limit))
    if answer is None:
        return code
    console = Console(highlight=False)
    _symbol_header(console, answer["symbol"])
    if not answer["events"]:
        if any(answer.get(key) for key in _LEFT_OUT):
            _hidden_untrusted(console, answer)
        else:
            console.print(f"  [grey50]{escape(answer['message'])}[/]")
        return 0
    table = Table(box=None, show_header=False, padding=(0, 2, 0, 0), pad_edge=False)
    table.add_column(no_wrap=True)
    table.add_column(style="grey50", no_wrap=True)
    table.add_column(overflow="fold")
    for event in answer["events"]:
        text = Text(event["summary"]) if event["summary"] is not None else Text(_NO_TEXT, style="yellow")
        text.append("\n").append_text(_origin_text(event["origin"], event["confidence"], event["id"]))
        table.add_row(Text(event["kind"], style=_KIND_STYLES.get(event["kind"], "")), event["recorded_at"][:10], text)
    console.print(table)
    _hidden_untrusted(console, answer)
    return 0


def _origin_text(origin: dict, confidence: str | None = None, event_id: str | None = None) -> Text:
    """"commit 1a2b3c4 · Ada Lovelace", "claude-code session", "shared · .whyfence/events/x.jsonl · Bob"."""
    author = (origin.get("author") or "").split(" <")[0] or None
    if origin["kind"] == "commit":
        parts = [f"commit {(origin.get('commit') or '')[:7]}", author]
    elif origin["kind"] == "shared":
        parts = ["shared", origin.get("file"), author or "uncommitted"]
    elif origin["kind"] == "session":
        parts = [f"{origin.get('agent') or 'agent'} session"]
    else:
        parts = [origin["kind"]]
    if confidence == "INFERRED":
        parts.append("inferred")
    text = Text(" · ".join(filter(None, parts)), style="grey50")
    if not origin["trusted"]:
        text.append(" · untrusted author", style="yellow")
    if event_id:
        text.append(f" · id {event_id[:ID_SHOWN]}", style="grey35")
    return text


ID_SHOWN = 8  # characters of an event id the CLI prints; `whyfence retire` accepts them
_LEFT_OUT = ("untrusted_hidden", "stale_hidden", "retired_hidden", "untrusted_text_withheld", "omitted")
_NO_TEXT = "(text from an untrusted author withheld; --include-untrusted to read it)"


def _hidden_untrusted(console: Console, answer: dict) -> None:
    """Say what was left out (untrusted, withheld, stale or retired), if anything was."""
    if any(answer.get(key) for key in _LEFT_OUT):
        wanted = (("untrusted_hidden", "--include-untrusted"), ("untrusted_text_withheld", "--include-untrusted"),
                  ("stale_hidden", "--include-stale"), ("omitted", "--limit"))
        flags = list(dict.fromkeys(flag for key, flag in wanted if answer.get(key)))
        console.print(f"[yellow]{escape(answer['message'])}[/]"
                      + (f" [grey50]({', '.join(flags)})[/]" if flags else ""))


def _history(args: argparse.Namespace) -> int:
    from whyfence import queries

    answer, code = _lookup(args, lambda conn: queries.lookup_history(
        conn, args.symbol, trust.load(args.repo), include_untrusted=args.include_untrusted))
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
        subject = introduced.get("subject") or (_NO_TEXT if introduced.get("text_withheld")
                                                else "(commit not indexed)")
        if version["current"]:
            subject += "  [current]"
        table.add_row(introduced.get("sha", "")[:7], (introduced.get("date") or "")[:10],
                      f"{version['file']}:{start}-{end}", Text(f"{version['name']}  ", style="bold").append(
                          subject, style="green" if version["current"] else ""))
    console.print(table)
    _hidden_untrusted(console, answer)
    return 0


def _rules(args: argparse.Namespace) -> int:
    from whyfence import queries

    answer, code = _lookup(args, lambda conn: queries.lookup_rules(
        conn, trust.load(args.repo), include_untrusted=args.include_untrusted,
        include_stale=args.include_stale))
    if answer is None:
        return code
    console = Console(highlight=False)
    if not answer["rules"]:
        if any(answer.get(key) for key in _LEFT_OUT):
            _hidden_untrusted(console, answer)
        else:
            console.print(f"[grey50]{escape(answer['message'])}[/]")
        return 0
    for rule in answer["rules"]:
        stale = " [yellow](stale: its code was deleted)[/]" if rule["stale"] else ""
        console.print(f"[bold magenta]•[/] {escape(rule['rule'])}{stale}")
        line = Text("  " + " · ".join(filter(None, [rule["recorded_at"][:10], *(rule["about"] or [])])) + " · ",
                    style="grey50")
        console.print(line.append_text(_origin_text(rule["origin"], event_id=rule["id"])))
    _hidden_untrusted(console, answer)
    return 0


def _retire(args: argparse.Namespace) -> int:
    from whyfence import queries

    console = Console(highlight=False)
    db_path = args.db.resolve() if args.db else _default_db(args.repo)
    if not db_path.is_file():
        console.print(f"[red]error:[/] no whyfence index at {escape(str(db_path))}; run `whyfence ingest` first")
        return 2
    with closing(store.connect(db_path)) as conn:
        if args.list or not args.ids:
            return _list_retired(console, conn, trust.load(args.repo))
        targets = []
        for prefix in args.ids:
            matches = store.find_event_ids(conn, prefix.lower()) if len(prefix) >= 6 else []
            if len(matches) != 1:
                problem = "matches several records" if matches else "doesn't match any record"
                if len(prefix) < 6:
                    problem = "is too short (give at least 6 characters)"
                console.print(f"[red]error:[/] {escape(prefix)!r} {problem}; ids are shown by "
                              "`whyfence rules`, `why` and `search`")
                return 2
            targets.append(matches[0])
        for event_id in targets:
            if queries.retired(conn, event_id, trust.load(args.repo)):
                console.print(f"[grey50]Already retired: {event_id[:ID_SHOWN]}[/]")
                continue
            try:
                result = team.retire(conn, args.repo, event_id, args.reason or "", local=args.local)
            except git.GitError as exc:
                console.print(f"[red]error:[/] {escape(str(exc))}")
                return 2
            console.print(f"Retired [cyan]{result.event.kind}[/] {escape(result.event.summary)}")
            if result.path is not None:
                console.print(f"  [grey50]Recorded in {escape(result.path.as_posix())}; commit it to retire it "
                              "for teammates who trust you.[/]")
    return 0


def _list_retired(console: Console, conn, trusted) -> int:
    found = store.retirements(conn)
    if not found:
        console.print("[grey50]Nothing has been retired.[/]")
        return 0
    for retirement in found:
        event = store.get_event(conn, retirement.event_id)
        summary = event.summary if event else "(a record this index doesn't have)"
        counts = retirement.origin == "local" or trusted.trusts(retirement.author)
        console.print(f"[bold]{escape(summary)}[/]  [grey50]id {retirement.event_id[:ID_SHOWN]}[/]")
        who = (retirement.author or "unknown").split(" <")[0] if retirement.origin == "shared" else "you"
        details = [store.to_timestamp(retirement.retired_at)[:10], f"by {who}", retirement.file,
                   f"\u201c{retirement.reason}\u201d" if retirement.reason else None]
        line = Text("  " + " · ".join(filter(None, details)), style="grey50")
        if not counts:
            line.append(" · ignored: untrusted author", style="yellow")
        console.print(line)
    return 0


def _search(args: argparse.Namespace) -> int:
    from whyfence import queries

    text = " ".join(args.text)
    answer, code = _lookup(args, lambda conn: queries.lookup_search(
        conn, text, trust.load(args.repo), include_untrusted=args.include_untrusted, limit=args.limit))
    if answer is None:
        return code
    console = Console(highlight=False)
    if not answer["events"]:
        if not any(answer.get(key) for key in _LEFT_OUT):
            console.print(f"[grey50]{escape(answer['message'])}[/]")
        _hidden_untrusted(console, answer)
        return 0
    table = Table(box=None, show_header=False, padding=(0, 2, 0, 0), pad_edge=False)
    table.add_column(no_wrap=True)
    table.add_column(style="grey50", no_wrap=True)
    table.add_column(overflow="fold")
    for event in answer["events"]:
        text_cell = Text(event["summary"]) if event["summary"] is not None else Text(_NO_TEXT, style="yellow")
        if event["symbols"]:
            text_cell.append("\n" + ", ".join(event["symbols"]), style="cyan")
        text_cell.append("\n").append_text(_origin_text(event["origin"], event["confidence"], event["id"]))
        table.add_row(Text(event["kind"], style=_KIND_STYLES.get(event["kind"], "")),
                      event["recorded_at"][:10], text_cell)
    console.print(table)
    _hidden_untrusted(console, answer)
    return 0


def _trust(args: argparse.Namespace) -> int:
    console = Console(highlight=False)
    try:
        root = git.repo_root(args.repo)
    except git.GitError as exc:
        console.print(f"[red]error:[/] {escape(str(exc))}")
        return 2
    scope = ["--global"] if args.globally else []
    for value in args.add:
        if value != trust.EVERYONE and not trust.email_of(value):
            console.print(f"[red]error:[/] {escape(value)!r} is not an email address")
            return 2
        if value.lower() not in [v.lower() for v in trust.configured(root)]:
            git_config(root, *scope, "--add", trust.CONFIG_KEY, value)
    for value in args.remove:
        git_config(root, *scope, "--fixed-value", "--unset-all", trust.CONFIG_KEY, value, check=False)
    loaded = trust.load(root)
    if loaded.everyone:
        console.print("Rules from [bold]every[/] author are trusted (whyfence.trust = *).")
    me = trust.email_of(git.config_value(root, "user.email"))
    for email in sorted(loaded.emails):
        console.print(f"  {escape(email)}" + ("  [grey50](you: user.email)[/]" if email == me else ""))
    if not loaded.emails and not loaded.everyone:
        console.print("[grey50]No one is trusted yet: set git's user.email, or run `whyfence trust EMAIL`.[/]")
    console.print("[grey50]Your own agent sessions are always trusted. Stored in git config (whyfence.trust), "
                  "never in the repository.[/]")
    return 0


def git_config(repo: Path, *args: str, check: bool = True) -> None:
    import subprocess

    result = subprocess.run(git._git(repo, "config", *args), capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    if check and result.returncode != 0:
        raise git.GitError(result.stderr.strip() or "git config failed")


def _check(args: argparse.Namespace) -> int:
    from whyfence import check

    console = Console(highlight=False)
    errors = Console(highlight=False, stderr=True)
    db_path = args.db.resolve() if args.db else _default_db(args.repo)
    if not db_path.is_file():
        errors.print(f"[red]error:[/] no whyfence index at {escape(str(db_path))}; run `whyfence ingest` first")
        return 2
    try:
        with closing(store.connect(db_path)) as conn:
            result = check.check(conn, args.repo, args.base, include_weak=args.all)
    except (check.CheckError, git.GitError) as exc:
        errors.print(f"[red]error:[/] {escape(str(exc))}")
        return 2
    if args.format in ("json", "markdown"):
        # Always UTF-8: these go to files and APIs, and Windows would otherwise write cp1252,
        # turning non-English text and the mention-breaking zero-width space into "?".
        text = json.dumps(result.to_json(), indent=2, ensure_ascii=False) + "\n" if args.format == "json" \
            else check.to_markdown(result)
        sys.stdout.flush()
        sys.stdout.buffer.write(text.encode("utf-8"))
        sys.stdout.buffer.flush()
    elif not result.fences:
        console.print(f"[grey50]{_plural(result.changed, 'existing symbol')} changed since "
                      f"{escape(args.base)}; none has a recorded rule, dead end, revert or stated reason.[/]")
    else:
        for fence in result.fences:
            what = "deleted" if fence.deleted else f"renamed to {fence.new_name}" if fence.new_name else "changed"
            console.print(f"[bold]{escape(fence.name)}[/]  [grey50]{escape(what)} · {escape(fence.file)}[/]")
            for event in fence.events:
                console.print(f"  [{_KIND_STYLES.get(event['kind'], '')}]{event['kind']}[/]  "
                              f"{escape(event['summary'])}  [grey50]{event['recorded_at'][:10]}[/]")
    if args.fail_on and any(e["kind"] in args.fail_on for f in result.fences for e in f.events):
        return 1
    return 0


def _mcp(args: argparse.Namespace) -> int:
    from whyfence import mcp_server  # deferred: the MCP stack is slow to import

    mcp_server.serve(args.db.resolve() if args.db else _default_db(args.repo), args.repo.resolve())
    return 0


def main(argv: list[str] | None = None) -> int:
    harden_streams()
    parser = argparse.ArgumentParser(
        prog="whyfence",
        description="Temporal memory for coding agents: why the code is the way it is.",
    )
    parser.add_argument("--version", action="version", version=f"whyfence {__version__}")
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
        query.add_argument("--include-untrusted", action="store_true",
                           help="also show rules and text from authors you haven't trusted (see `whyfence trust`)")
        if name == "why":
            query.add_argument("--limit", type=int, default=10, metavar="N",
                               help="most records to show, strongest first (default: 10)")
        if name == "rules":
            query.add_argument("--include-stale", action="store_true",
                               help="also show rules about code that has since been deleted")
        query.set_defaults(func=func, include_untrusted=False, include_stale=False)

    retiring = commands.add_parser(
        "retire", help="mark a rule or record as no longer true; with no ids, list what's retired",
        description="Retire records that are no longer true, by the ids `whyfence rules`, `why` and `search` "
                    "print. A retirement takes effect in your index at once and is appended to your shared file "
                    "in .whyfence/events/ (unless --local), so it survives rebuilding the index and, once "
                    "committed, applies for teammates who trust you. Retirements by untrusted authors are ignored.")
    retiring.add_argument("ids", nargs="*", metavar="ID", help="event ids, or their first 6+ characters")
    retiring.add_argument("--reason", help="why it no longer holds (shared with the retirement)")
    retiring.add_argument("--local", action="store_true", help="only in this index; don't write to the repository")
    retiring.add_argument("--list", action="store_true", help="list retired records")
    retiring.add_argument("--repo", type=Path, default=Path("."), help="repository (default: .)")
    retiring.add_argument("--db", type=Path, help=f"database path (default: <repo>/{DEFAULT_DB.as_posix()})")
    retiring.set_defaults(func=_retire)

    searching = commands.add_parser(
        "search", help="full-text search over every recorded decision, rule, dead end and correction",
        description="Search what's recorded when there's no single function to ask `why` about.")
    searching.add_argument("text", nargs="+", help="words to look for (matched literally)")
    searching.add_argument("--limit", type=int, default=20, help="most results to show (default: 20, at most 100)")
    searching.add_argument("--repo", type=Path, default=Path("."), help="repository whose index to read (default: .)")
    searching.add_argument("--db", type=Path, help=f"database path (default: <repo>/{DEFAULT_DB.as_posix()})")
    searching.add_argument("--json", action="store_true", help="print the same JSON the MCP tool returns")
    searching.add_argument("--include-untrusted", action="store_true",
                           help="also show rules from authors you haven't trusted (see `whyfence trust`)")
    searching.set_defaults(func=_search)

    trusting = commands.add_parser(
        "trust", help="choose whose commits and shared notes may set rules for your agent",
        description="Rules come from commit messages and .whyfence/events/, which anyone who lands a commit can "
                    "write. Only authors listed here (plus your own user.email and your own agent sessions) may "
                    "set rules. The list lives in git config, never in the repository. With no arguments, "
                    "lists who is trusted.")
    trusting.add_argument("add", nargs="*", metavar="EMAIL", help="author emails to trust; * trusts everyone")
    trusting.add_argument("--remove", nargs="+", default=[], metavar="EMAIL", help="stop trusting these")
    trusting.add_argument("--global", dest="globally", action="store_true",
                          help="save in your global git config (every repository) instead of this one")
    trusting.add_argument("--repo", type=Path, default=Path("."), help="repository (default: .)")
    trusting.set_defaults(func=_trust)

    checker = commands.add_parser(
        "check", help="show what's recorded about the functions a branch or pull request changes",
        description="Show the decisions, rules and dead ends recorded against every function or class that "
                    "the first-parent commits in BASE..HEAD changed. Run `whyfence ingest` first.")
    checker.add_argument("base", help="the branch or commit the change is based on, e.g. main or origin/main")
    checker.add_argument("--repo", type=Path, default=Path("."), help="repository (default: .)")
    checker.add_argument("--db", type=Path, help=f"database path (default: <repo>/{DEFAULT_DB.as_posix()})")
    checker.add_argument("--format", choices=("text", "markdown", "json"), default="text",
                         help="markdown is what the GitHub Action posts on pull requests")
    checker.add_argument("--all", action="store_true",
                         help="also list weak signals, such as old commits whose subject starts with \"Fix\"")
    checker.add_argument("--fail-on", nargs="+", metavar="KIND", choices=sorted(store.EVENT_KINDS),
                         help="exit 1 if a changed symbol has an event of one of these kinds, e.g. constraint")
    checker.set_defaults(func=_check)

    mcp = commands.add_parser("mcp", help="serve the rules/why/history/search tools to MCP clients over stdio")
    mcp.add_argument("--repo", type=Path, default=Path("."),
                     help="repository whose index to serve (default: .)")
    mcp.add_argument("--db", type=Path, help=f"database path (default: <repo>/{DEFAULT_DB.as_posix()})")
    mcp.set_defaults(func=_mcp)

    args = parser.parse_args(argv)
    return args.func(args)

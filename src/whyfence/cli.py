"""Command-line entry point: `whyfence <command>`."""

from __future__ import annotations

import argparse

from whyfence import linter


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
    args = parser.parse_args(argv)
    return args.func(args)

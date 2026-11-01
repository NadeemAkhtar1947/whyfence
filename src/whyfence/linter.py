"""Stale-context linter for agent instruction files.

CLAUDE.md and AGENTS.md tell coding agents how to build, test and run a
project, and they rot: a script is renamed in package.json or a make target is
deleted, and every agent session keeps running a command that no longer exists.
This module pulls runnable commands out of those files and checks each one
against what it would actually run: Makefile targets, package.json scripts,
pyproject entry points, Python modules (`python -m`) and script files.

Only what can be checked statically is judged. A command whose outcome depends
on runtime state (computed target names, pattern rules, workspace filters) is
reported as unverifiable rather than guessed at.
"""

from __future__ import annotations

import argparse
import difflib
import fnmatch
import json
import os
import re
import shlex
import sys
import tomllib
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.style import Style
from rich.table import Table
from rich.text import Text

CONTEXT_FILE_NAMES = ("CLAUDE.md", "AGENTS.md", "CLAUDE.local.md")
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".tox", "dist", "target"}
# Example projects often ship deliberately broken context files (ours does).
DEFAULT_EXCLUDES = ("examples",)


class Status(str, Enum):
    OK = "ok"
    STALE = "stale"
    MISSING_MANIFEST = "missing_manifest"
    UNVERIFIABLE = "unverifiable"


@dataclass(frozen=True)
class Snippet:
    """A piece of shell text found in a context file."""

    text: str
    line: int
    inline: bool


@dataclass(frozen=True)
class Command:
    """One runner invocation (`make test`, `npm run dev`, `python -m app`) to verify."""

    runner: str
    # What `target` names: "task" (make goal / package.json script), "module"
    # (`python -m`), "file" (a script path) or "entrypoint" (`uv run <name>`).
    kind: str
    target: str | None  # None = make's default goal
    cwd: Path | None  # directory it resolves against; None when not statically known
    text: str
    source: Path
    line: int
    makefile: str | None = None  # explicit `make -f`
    note: str | None = None  # parse-time reason it can't be verified
    bin_fallback: bool = False  # runner may resolve a non-script name to a package binary
    optional: bool = False  # `npm run --if-present`


@dataclass(frozen=True)
class Finding:
    command: Command
    status: Status
    reason: str
    suggestion: str | None = None

    @property
    def failed(self) -> bool:
        return self.status in (Status.STALE, Status.MISSING_MANIFEST)


# --------------------------------------------------------------------------- #
# Markdown -> shell snippets
# --------------------------------------------------------------------------- #

SHELL_LANGS = {
    "", "bash", "sh", "shell", "zsh", "fish", "powershell", "pwsh", "ps1", "cmd", "bat",
    "console", "terminal", "shell-session", "shellsession",
}
# Blocks that interleave commands with their output: only prompt lines are commands.
PROMPT_LANGS = {"console", "terminal", "shell-session", "shellsession"}

_FENCE_OPEN_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})\s*([^\s`{]*)")
_PROMPT_RE = re.compile(r"^\s*(?:\$|PS[^>]*>|❯)\s+")
_INLINE_CODE_RE = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)")


def extract_snippets(markdown: str) -> Iterator[Snippet]:
    """Yield shell text from fenced shell blocks and inline code spans."""
    lines = markdown.splitlines()
    i = 0
    while i < len(lines):
        opener = _FENCE_OPEN_RE.match(lines[i])
        if not opener:
            for span in _INLINE_CODE_RE.finditer(lines[i]):
                yield Snippet(span.group(2).strip(), i + 1, inline=True)
            i += 1
            continue

        fence, lang = opener.group(1), opener.group(2).lower()
        close_re = re.compile(rf"^ {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}\s*$")
        i += 1
        pending: list[str] = []
        pending_line = 0
        while i < len(lines) and not close_re.match(lines[i]):
            if lang in SHELL_LANGS:
                line = lines[i]
                if not pending:
                    prompt = _PROMPT_RE.match(line)
                    if prompt:
                        line = line[prompt.end():]
                    elif lang in PROMPT_LANGS:
                        i += 1
                        continue
                    pending_line = i + 1
                if line.rstrip().endswith("\\"):
                    pending.append(line.rstrip()[:-1])
                else:
                    pending.append(line)
                    text = " ".join(part.strip() for part in pending).strip()
                    if text:
                        yield Snippet(text, pending_line, inline=False)
                    pending = []
            i += 1
        if pending:
            yield Snippet(" ".join(p.strip() for p in pending).strip(), pending_line, inline=False)
        i += 1  # closing fence


# --------------------------------------------------------------------------- #
# Shell snippets -> runner commands
# --------------------------------------------------------------------------- #

_SEPARATORS = {"&&", "||", ";", "|", "&", "|&", ";;", "(", ")"}
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_PREFIX_COMMANDS = {"sudo", "time", "env", "nohup", "command"}
_WINDOWS_SEPARATOR_RE = re.compile(r"\\(?=[A-Za-z0-9_.])")


def _segments(text: str) -> list[list[str]]:
    """Split a shell line into simple commands, dropping redirections."""
    # Windows paths (`.\.venv\Scripts\python`) would lose their backslashes to
    # POSIX escaping; a backslash before a path character is a separator.
    text = _WINDOWS_SEPARATOR_RE.sub("/", text)
    lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:  # unbalanced quotes: not a command we can reason about
        return []

    segments: list[list[str]] = []
    current: list[str] = []
    redirected = False
    for token in tokens:
        if token in _SEPARATORS:
            if current:
                segments.append(current)
            current, redirected = [], False
        elif set(token) <= set("<>&"):
            if current and current[-1].isdigit():  # the `2` in `2>&1`
                current.pop()
            redirected = True
        elif not redirected:
            current.append(token)
    if current:
        segments.append(current)
    return segments


def _strip_prefixes(segment: list[str]) -> list[str]:
    i = 0
    while i < len(segment) and (segment[i] in _PREFIX_COMMANDS or _ENV_ASSIGN_RE.match(segment[i])):
        i += 1
    return segment[i:]


def _join(cwd: Path | None, rel: str) -> Path | None:
    """Resolve a relative directory change; None when it can't be known statically."""
    if cwd is None or not rel or rel == "-" or rel[0] in "~$/\\" or "$" in rel or re.match(r"^[A-Za-z]:", rel):
        return None
    return Path(os.path.normpath(cwd / rel))


def _scan(args: list[str], value_opts: set[str]) -> tuple[list[str], dict[str, str]]:
    """Split CLI args into positionals and options, stopping at `--`."""
    positionals: list[str] = []
    opts: dict[str, str] = {}
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            break
        if arg.startswith("-") and len(arg) > 1:
            name, eq, value = arg.partition("=")
            if name in value_opts and not eq:
                i += 1
                value = args[i] if i < len(args) else ""
            opts[name] = value
        else:
            positionals.append(arg)
        i += 1
    return positionals, opts


_MAKE_LONG_WITH_ARG = {
    "--directory", "--file", "--makefile", "--include-dir", "--old-file", "--assume-old",
    "--new-file", "--assume-new", "--what-if",
}


def _is_number(value: str) -> bool:
    return value.replace(".", "", 1).isdigit()


def _parse_make(args: list[str], cwd: Path | None, inline: bool) -> list[dict]:
    makefile: str | None = None
    goals: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg.startswith("--"):
            name, eq, value = arg.partition("=")
            if name in _MAKE_LONG_WITH_ARG:
                if not eq:
                    i += 1
                    value = args[i] if i < len(args) else ""
                if name == "--directory":
                    cwd = _join(cwd, value)
                elif name in ("--file", "--makefile"):
                    makefile = value
            elif name in ("--jobs", "--load-average", "--max-load") and not eq \
                    and i + 1 < len(args) and _is_number(args[i + 1]):
                i += 1
        elif arg.startswith("-") and len(arg) > 1:
            flag = arg[1]
            if flag in "CfIoW":
                value = arg[2:]
                if not value:
                    i += 1
                    value = args[i] if i < len(args) else ""
                if flag == "C":
                    cwd = _join(cwd, value)
                elif flag == "f":
                    makefile = value
            elif flag in "jl" and len(arg) == 2 and i + 1 < len(args) and _is_number(args[i + 1]):
                i += 1
        elif "=" not in arg:  # `VAR=value` is a variable override, not a goal
            goals.append(arg)
        i += 1

    note = "makefile path is computed at runtime" if makefile and "$" in makefile else None
    if not goals:
        # A bare `make` in prose ("we use `make`") is not an instruction to run it.
        return [] if inline else [dict(target=None, cwd=cwd, makefile=makefile, note=note)]
    return [
        dict(target=goal, cwd=cwd, makefile=makefile,
             note=note or ("target name is computed at runtime" if "$" in goal else None))
        for goal in goals
    ]


_NPM_RUN = {"run", "run-script", "rum", "urn"}
_NPM_LIFECYCLE = {"test": "test", "t": "test", "tst": "test", "start": "start", "stop": "stop", "restart": "restart"}
_NPM_WORKSPACE_OPTS = {"-w", "--workspace", "--workspaces", "-ws"}


def _parse_npm(args: list[str], cwd: Path | None, inline: bool) -> list[dict]:
    positionals, opts = _scan(args, {"--prefix", "-w", "--workspace", "--loglevel"})
    if not positionals:
        return []
    sub = positionals[0]
    if sub in _NPM_RUN:
        if len(positionals) < 2:
            return []  # `npm run` alone lists scripts
        target = positionals[1]
    elif sub in _NPM_LIFECYCLE:
        target = _NPM_LIFECYCLE[sub]
    else:
        return []
    if "--prefix" in opts:
        cwd = _join(cwd, opts["--prefix"])
    note = "runs in npm workspaces" if _NPM_WORKSPACE_OPTS & opts.keys() else None
    return [dict(target=target, cwd=cwd, note=note, optional="--if-present" in opts)]


_YARN_BUILTINS = {
    "add", "audit", "autoclean", "bin", "cache", "check", "config", "constraints", "create", "dedupe",
    "dlx", "exec", "explain", "generate-lock-entry", "global", "help", "import", "info", "init",
    "install", "link", "licenses", "list", "login", "logout", "node", "npm", "outdated", "owner",
    "pack", "patch", "patch-commit", "plugin", "policies", "publish", "rebuild", "remove", "search",
    "set", "stage", "tag", "team", "unlink", "unplug", "up", "upgrade", "upgrade-interactive",
    "version", "versions", "why",
}


def _parse_yarn(args: list[str], cwd: Path | None, inline: bool) -> list[dict]:
    positionals, opts = _scan(args, {"--cwd"})
    if not positionals:
        return []  # bare `yarn` installs
    if "--cwd" in opts:
        cwd = _join(cwd, opts["--cwd"])
    sub = positionals[0]
    if sub in ("workspace", "workspaces"):
        target = positionals[2] if sub == "workspace" and len(positionals) > 2 else None
        return [dict(target=target, cwd=cwd, note="runs inside a yarn workspace")] if target else []
    if sub == "run":
        if len(positionals) < 2:
            return []
        return [dict(target=positionals[1], cwd=cwd, bin_fallback=True)]
    if sub in _YARN_BUILTINS:
        return []
    return [dict(target=sub, cwd=cwd, bin_fallback=True)]


_PNPM_BUILTINS = {
    "add", "i", "install", "install-test", "it", "ln", "link", "list", "ls", "la", "ll", "outdated",
    "pack", "prune", "publish", "rb", "rebuild", "remove", "rm", "uninstall", "un", "unlink", "up",
    "update", "upgrade", "why", "audit", "exec", "dlx", "create", "init", "env", "setup", "store",
    "server", "root", "bin", "config", "c", "get", "set", "doctor", "deploy", "fetch", "import",
    "patch", "patch-commit", "patch-remove", "licenses", "help", "approve-builds", "self-update",
    "sbom", "cat-file", "cat-index", "find-hash",
}
_PNPM_RECURSIVE_OPTS = {"-r", "--recursive", "--filter", "-F"}


def _parse_pnpm(args: list[str], cwd: Path | None, inline: bool) -> list[dict]:
    positionals, opts = _scan(args, {"-C", "--dir", "--filter", "-F", "--workspace-dir"})
    if not positionals:
        return []
    for dir_opt in ("-C", "--dir"):
        if dir_opt in opts:
            cwd = _join(cwd, opts[dir_opt])
    note = "runs across workspace packages" if _PNPM_RECURSIVE_OPTS & opts.keys() else None
    sub = positionals[0]
    if sub in ("run", "run-script"):
        if len(positionals) < 2:
            return []
        return [dict(target=positionals[1], cwd=cwd, note=note)]
    if sub in ("test", "t", "tst", "start"):
        return [dict(target="start" if sub == "start" else "test", cwd=cwd, note=note)]
    if sub in _PNPM_BUILTINS:
        return []
    return [dict(target=sub, cwd=cwd, note=note, bin_fallback=True)]


def _parse_bun(args: list[str], cwd: Path | None, inline: bool) -> list[dict]:
    positionals, opts = _scan(args, {"--cwd"})
    if len(positionals) < 2 or positionals[0] != "run":
        return []  # `bun test`, `bun install`, ... are built-ins
    target = positionals[1]
    if "/" in target or "\\" in target or Path(target).suffix in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
        return []  # `bun run src/index.ts` runs a file, not a script
    if "--cwd" in opts:
        cwd = _join(cwd, opts["--cwd"])
    return [dict(target=target, cwd=cwd, bin_fallback=True)]


_PYTHON_RE = re.compile(r"^(?:python(?:\d+(?:\.\d+)?)?|py)(?:\.exe)?$", re.IGNORECASE)
_SCRIPT_SUFFIXES = {".py", ".sh", ".bash", ".zsh", ".js", ".mjs", ".cjs", ".ts", ".rb", ".pl"}
_FILE_INTERPRETERS = {"bash", "sh", "zsh", "node", "ruby", "perl"}
# Paths into installed environments depend on local setup, not on the repo.
_ENVIRONMENT_DIRS = {"node_modules", ".venv", "venv", "env"}


def _file_command(path: str, cwd: Path | None, runner: str) -> dict:
    fields = dict(kind="file", target=path, cwd=cwd, runner=runner)
    if any(ch in path for ch in "$*?[") or path.startswith("~"):
        fields["note"] = "path is computed at runtime"
    elif Path(path).is_absolute() or re.match(r"^[A-Za-z]:", path):
        fields["note"] = "absolute paths depend on the machine"
    return fields


def _parse_python(args: list[str], cwd: Path | None, inline: bool) -> list[dict]:
    """`python [opts] -m module` or `python [opts] script.py`."""
    i = 0
    while i < len(args):
        arg = args[i]
        if arg.startswith("-m"):
            module = arg[2:] or (args[i + 1] if i + 1 < len(args) else "")
            return [dict(kind="module", target=module, cwd=cwd, runner="python")] if module else []
        if arg.startswith("-c") or arg == "-":
            return []  # inline code or stdin
        if arg in ("-X", "-W"):
            i += 2
            continue
        if arg.startswith("-"):
            i += 1
            continue
        return [_file_command(arg, cwd, "python")]
    return []  # bare `python` starts a REPL


def _parse_interpreter(args: list[str], cwd: Path | None, inline: bool, runner: str) -> list[dict]:
    """`bash scripts/x.sh`, `node scripts/x.js`: the first positional is the script."""
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("-c", "-e", "-p", "--eval", "--print"):
            return []
        if arg in ("-r", "--require", "--import", "--loader", "-o", "-O"):
            i += 2
            continue
        if arg.startswith("-"):
            i += 1
            continue
        return [_file_command(arg, cwd, runner)]
    return []


_UV_VALUE_OPTS = {
    "--directory", "--project", "--package", "--with", "--with-requirements", "--with-editable",
    "--python", "-p", "--extra", "--group", "--only-group", "--no-group", "--env-file", "--index",
    "--default-index", "--index-url", "--extra-index-url", "--find-links", "-f", "--cache-dir",
    "--config-file", "--color", "--prerelease", "--resolution", "--exclude-newer", "--link-mode",
}
_POETRY_VALUE_OPTS = {"-C", "--directory", "-P", "--project"}


def _parse_project_runner(args: list[str], cwd: Path | None, inline: bool, runner: str) -> list[dict]:
    """`uv run <cmd>` / `poetry run <cmd>`, with options before and after `run`."""
    value_opts = _UV_VALUE_OPTS if runner == "uv" else _POETRY_VALUE_OPTS
    seen_run = module = False
    note: str | None = None
    i = 0
    while i < len(args):
        arg = args[i]
        if arg.startswith("-"):
            name, eq, value = arg.partition("=")
            if name in value_opts and not eq:
                i += 1
                value = args[i] if i < len(args) else ""
            if name in ("--directory", "--project", "-C", "-P"):
                cwd = _join(cwd, value)
            elif name == "--package":
                note = "runs a workspace member"
            elif name in ("-m", "--module") and runner == "uv":
                module = True
            i += 1
            continue
        if not seen_run:
            if arg != "run":
                return []  # `uv sync`, `poetry install`, ...
            seen_run = True
            i += 1
            continue

        rest = args[i + 1:]
        if module:
            found = [dict(kind="module", target=arg, cwd=cwd)]
        elif _PYTHON_RE.match(Path(arg).name):
            found = _parse_python(rest, cwd, inline)
        elif "/" in arg or Path(arg).suffix in _SCRIPT_SUFFIXES:
            found = [_file_command(arg, cwd, runner)]
        elif arg in _RUNNER_PARSERS or arg in _FILE_INTERPRETERS:
            return []  # `uv run make test` is checked as `make test` elsewhere
        else:
            found = [dict(kind="entrypoint", target=arg, cwd=cwd)]
        for fields in found:
            fields["runner"] = runner
            if note and not fields.get("note"):
                fields["note"] = note
        return found
    return []


_RUNNER_PARSERS = {
    "make": _parse_make,
    "npm": _parse_npm,
    "yarn": _parse_yarn,
    "pnpm": _parse_pnpm,
    "bun": _parse_bun,
}


def _parser_for(head: str):
    if head in _RUNNER_PARSERS:
        return _RUNNER_PARSERS[head]
    if head in ("uv", "poetry"):
        return lambda args, cwd, inline: _parse_project_runner(args, cwd, inline, head)
    if _PYTHON_RE.match(Path(head).name):  # also `.venv/Scripts/python`
        return _parse_python
    if head in _FILE_INTERPRETERS:
        return lambda args, cwd, inline: _parse_interpreter(args, cwd, inline, head)
    path = Path(head)
    is_script = head.startswith(("./", "../")) or ("/" in head and path.suffix in _SCRIPT_SUFFIXES)
    if is_script and not _ENVIRONMENT_DIRS & set(path.parts):
        return lambda args, cwd, inline: [_file_command(head, cwd, "exec")]
    return None


def parse_commands(snippet: Snippet, root: Path, source: Path) -> Iterator[Command]:
    """Turn one shell snippet into the runner commands it invokes, tracking `cd`."""
    cwd: Path | None = root
    for segment in _segments(snippet.text):
        segment = _strip_prefixes(segment)
        if not segment:
            continue
        head = segment[0]
        if head in ("cd", "pushd"):
            cwd = _join(cwd, segment[1]) if len(segment) > 1 else None
            continue
        if head == "popd":
            cwd = None
            continue
        parser = _parser_for(head)
        if parser is None:
            continue
        for fields in parser(segment[1:], cwd, snippet.inline):
            fields.setdefault("runner", head)
            fields.setdefault("kind", "task")
            yield Command(text=" ".join(segment), source=source, line=snippet.line, **fields)


# --------------------------------------------------------------------------- #
# Manifests
# --------------------------------------------------------------------------- #

MAKEFILE_NAMES = ("GNUmakefile", "makefile", "Makefile")

# `targets...:` or `targets...::` at line start; excludes `:=`, `::=` and `=` assignments.
_RULE_RE = re.compile(r"^([^\s:=#][^:=#]*?)\s*::?(?![:=])")
_INCLUDE_RE = re.compile(r"^-?s?include\s+(.+)$")
_SPECIAL_TARGET_RE = re.compile(r"^\.[A-Z_]+$")


@dataclass(frozen=True)
class MakefileInfo:
    targets: frozenset[str]
    patterns: tuple[str, ...]
    dynamic: bool  # some target or include name is computed at runtime


def _logical_lines(text: str) -> Iterator[str]:
    buffer = ""
    for raw in text.splitlines():
        if raw.endswith("\\"):
            buffer += raw[:-1] + " "
            continue
        yield buffer + raw
        buffer = ""
    if buffer:
        yield buffer


def _read_makefile(path: Path, seen: set[Path], targets: set[str], patterns: list[str]) -> bool:
    """Collect rule targets from `path` and its includes; returns True if any are dynamic."""
    if path in seen or not path.is_file():
        return False
    seen.add(path)
    dynamic = False
    in_define = False
    for line in _logical_lines(path.read_text(encoding="utf-8", errors="replace")):
        stripped = line.strip()
        if in_define:
            in_define = not stripped.startswith("endef")
            continue
        if line.startswith("\t"):  # recipe line
            continue
        if stripped.startswith("define "):
            in_define = True
            continue
        line = line.split("#", 1)[0]
        include = _INCLUDE_RE.match(line.strip())
        if include:
            for name in include.group(1).split():
                if "$" in name:
                    dynamic = True
                else:
                    dynamic |= _read_makefile(path.parent / name, seen, targets, patterns)
            continue
        rule = _RULE_RE.match(line)
        if not rule:
            continue
        for target in rule.group(1).split():
            if "$" in target:
                dynamic = True
            elif "%" in target:
                patterns.append(target)
            elif not _SPECIAL_TARGET_RE.match(target):
                targets.add(target)
    return dynamic


@lru_cache(maxsize=256)
def _load_makefile_at(path: Path, mtime_ns: int) -> MakefileInfo:
    targets: set[str] = set()
    patterns: list[str] = []
    dynamic = _read_makefile(path, set(), targets, patterns)
    return MakefileInfo(frozenset(targets), tuple(patterns), dynamic)


def load_makefile(path: Path) -> MakefileInfo:
    return _load_makefile_at(path, path.stat().st_mtime_ns)


def find_makefile(cwd: Path, explicit: str | None = None) -> Path | None:
    if explicit:
        path = cwd / explicit
        return path if path.is_file() else None
    # Match exact entry names: on case-insensitive filesystems `makefile` would
    # otherwise match `Makefile` and report the wrong file.
    try:
        entries = set(os.listdir(cwd))
    except OSError:
        return None
    for name in MAKEFILE_NAMES:
        if name in entries and (cwd / name).is_file():
            return cwd / name
    return None


@dataclass(frozen=True)
class PackageInfo:
    scripts: frozenset[str]
    dependencies: frozenset[str]


@lru_cache(maxsize=256)
def _load_package_at(path: Path, mtime_ns: int) -> PackageInfo | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    scripts = data.get("scripts") or {}
    deps: set[str] = set()
    for key in ("dependencies", "devDependencies", "optionalDependencies"):
        if isinstance(data.get(key), dict):
            deps.update(data[key])
    return PackageInfo(frozenset(scripts) if isinstance(scripts, dict) else frozenset(), frozenset(deps))


def load_package(path: Path) -> PackageInfo | None:
    return _load_package_at(path, path.stat().st_mtime_ns)


_REQUIREMENT_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _normalize(name: str) -> str:
    """PEP 503 name normalization, with `_` so it also matches module names."""
    return re.sub(r"[-_.]+", "_", name).lower()


def _requirement_names(specs: Iterable) -> set[str]:
    names = set()
    for spec in specs:
        match = _REQUIREMENT_NAME_RE.match(spec) if isinstance(spec, str) else None
        if match:
            names.add(_normalize(match.group(1)))
    return names


@dataclass(frozen=True)
class PyProjectInfo:
    scripts: frozenset[str]
    dependencies: frozenset[str]  # normalized distribution names


@lru_cache(maxsize=256)
def _load_pyproject_at(path: Path, mtime_ns: int) -> PyProjectInfo | None:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    project = data.get("project", {})
    tool = data.get("tool", {})
    poetry = tool.get("poetry", {})

    scripts = set(project.get("scripts", {})) | set(project.get("gui-scripts", {})) | set(poetry.get("scripts", {}))
    deps = _requirement_names(project.get("dependencies", []))
    for group in project.get("optional-dependencies", {}).values():
        deps |= _requirement_names(group)
    for group in data.get("dependency-groups", {}).values():  # PEP 735
        deps |= _requirement_names(group)
    deps |= _requirement_names(tool.get("uv", {}).get("dev-dependencies", []))
    poetry_tables = [poetry.get("dependencies", {}), poetry.get("dev-dependencies", {})]
    poetry_tables += [g.get("dependencies", {}) for g in poetry.get("group", {}).values()]
    for table in poetry_tables:
        deps |= {_normalize(name) for name in table if name.lower() != "python"}
    return PyProjectInfo(frozenset(scripts), frozenset(deps))


@lru_cache(maxsize=256)
def _load_requirements_at(path: Path, mtime_ns: int) -> frozenset[str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return frozenset()
    return frozenset(_requirement_names(line for line in lines if not line.lstrip().startswith(("#", "-"))))


@dataclass(frozen=True)
class PythonEnv:
    """What a Python command in some directory can see: its project and its .venv."""

    pyproject: Path | None
    scripts: frozenset[str]
    dependencies: frozenset[str]
    site_packages: tuple[Path, ...]
    bin_dirs: tuple[Path, ...]


def python_env(cwd: Path, root: Path) -> PythonEnv:
    # uv and poetry find the project by walking up from the working directory.
    pyproject = None
    for directory in (cwd, *cwd.parents):
        if (directory / "pyproject.toml").is_file():
            pyproject = directory / "pyproject.toml"
            break
        if directory == root:
            break
    project_dir = pyproject.parent if pyproject else cwd

    scripts: frozenset[str] = frozenset()
    deps: set[str] = set()
    if pyproject:
        info = _load_pyproject_at(pyproject, pyproject.stat().st_mtime_ns)
        if info:
            scripts, deps = info.scripts, set(info.dependencies)
    for directory in {cwd, project_dir}:
        for req in sorted(directory.glob("requirements*.txt")):
            deps |= _load_requirements_at(req, req.stat().st_mtime_ns)

    site_packages: list[Path] = []
    bin_dirs: list[Path] = []
    for directory in dict.fromkeys((cwd, project_dir, root)):
        venv = directory / ".venv"
        if not venv.is_dir():
            continue
        site_packages += [venv / "Lib" / "site-packages", *sorted(venv.glob("lib/python*/site-packages"))]
        bin_dirs += [venv / "Scripts", venv / "bin"]
    return PythonEnv(
        pyproject, scripts, frozenset(deps),
        tuple(p for p in site_packages if p.is_dir()), tuple(p for p in bin_dirs if p.is_dir()),
    )


def _package_manager(cwd: Path) -> str:
    for lockfile, manager in (("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"),
                              ("bun.lock", "bun"), ("bun.lockb", "bun")):
        if (cwd / lockfile).exists():
            return manager
    return "npm"


# --------------------------------------------------------------------------- #
# Verification
# --------------------------------------------------------------------------- #

_DIR_FLAGS = {"make": "-C", "npm": "--prefix", "yarn": "--cwd", "pnpm": "-C", "bun": "--cwd"}


def _display(path: Path, root: Path) -> str:
    try:
        rel = os.path.relpath(path, root)
    except ValueError:  # different drive on Windows
        return Path(path).as_posix()
    if rel == ".." or rel.startswith(".." + os.sep):
        return Path(path).as_posix()  # outside `root`: an absolute path reads better than ../../..
    return "./" if rel == "." else rel.replace(os.sep, "/")


def _canonical(runner: str, target: str, cwd: Path, root: Path) -> str:
    parts = [runner]
    rel = _display(cwd, root)
    if rel != "./":
        parts += [_DIR_FLAGS[runner], rel]
    parts += [target] if runner == "make" else ["run", target]
    return " ".join(parts)


def _closest(name: str, candidates: Iterable[str]) -> str | None:
    matches = difflib.get_close_matches(name, sorted(candidates), n=1, cutoff=0.6)
    return matches[0] if matches else None


def _subdirs(root: Path, depth: int = 2) -> Iterator[Path]:
    if depth == 0:
        return
    try:
        children = sorted(p for p in root.iterdir() if p.is_dir() and p.name not in SKIP_DIRS)
    except OSError:
        return
    for child in children:
        yield child
        yield from _subdirs(child, depth - 1)


def _moved_to(target: str, cwd: Path, root: Path) -> str | None:
    """Find the same target in a nearby Makefile or package.json (code moved into a subdirectory)."""
    for directory in _subdirs(cwd):
        makefile = find_makefile(directory)
        if makefile and target in load_makefile(makefile).targets:
            return _canonical("make", target, directory, root)
        package = directory / "package.json"
        info = load_package(package) if package.is_file() else None
        if info and target in info.scripts:
            return _canonical(_package_manager(directory), target, directory, root)
    return None


def _check_make(cmd: Command, root: Path) -> Finding:
    assert cmd.cwd is not None
    makefile = find_makefile(cmd.cwd, cmd.makefile)
    where = _display(cmd.cwd, root)
    if makefile is None:
        name = cmd.makefile or "Makefile"
        suggestion = None
        package = cmd.cwd / "package.json"
        info = load_package(package) if cmd.target and package.is_file() else None
        if info and cmd.target in info.scripts:
            suggestion = _canonical(_package_manager(cmd.cwd), cmd.target, cmd.cwd, root)
        elif cmd.target:
            suggestion = _moved_to(cmd.target, cmd.cwd, root)
        return Finding(cmd, Status.MISSING_MANIFEST, f"no {name} in {where}", suggestion)

    info = load_makefile(makefile)
    if cmd.target is None:
        if info.targets or info.patterns or info.dynamic:
            return Finding(cmd, Status.OK, "runs the default goal")
        return Finding(cmd, Status.STALE, f"{_display(makefile, root)} defines no targets")
    if cmd.target in info.targets:
        return Finding(cmd, Status.OK, f"target in {_display(makefile, root)}")
    pattern = next((p for p in info.patterns if fnmatch.fnmatchcase(cmd.target, p.replace("%", "*"))), None)
    if pattern:
        return Finding(cmd, Status.UNVERIFIABLE, f"only matched by pattern rule `{pattern}`")
    if info.dynamic:
        return Finding(cmd, Status.UNVERIFIABLE, f"{_display(makefile, root)} has computed target names")

    reason = f"no `{cmd.target}` target in {_display(makefile, root)}"
    if (cmd.cwd / cmd.target).exists():
        reason += " (a file by that name exists, so make silently does nothing)"
    match = _closest(cmd.target, info.targets)
    suggestion = _canonical("make", match, cmd.cwd, root) if match else _moved_to(cmd.target, cmd.cwd, root)
    return Finding(cmd, Status.STALE, reason, suggestion)


def _check_script(cmd: Command, root: Path) -> Finding:
    assert cmd.cwd is not None and cmd.target is not None
    package = cmd.cwd / "package.json"
    if not package.is_file():
        suggestion = None
        makefile = find_makefile(cmd.cwd)
        if makefile and cmd.target in load_makefile(makefile).targets:
            suggestion = _canonical("make", cmd.target, cmd.cwd, root)
        else:
            suggestion = _moved_to(cmd.target, cmd.cwd, root)
        return Finding(cmd, Status.MISSING_MANIFEST, f"no package.json in {_display(cmd.cwd, root)}", suggestion)

    info = load_package(package)
    if info is None:
        return Finding(cmd, Status.UNVERIFIABLE, f"{_display(package, root)} is not valid JSON")
    if cmd.target in info.scripts:
        return Finding(cmd, Status.OK, f"script in {_display(package, root)}")
    if cmd.optional:
        return Finding(cmd, Status.OK, "missing, but --if-present makes it a no-op")
    if cmd.runner == "npm" and cmd.target == "start" and (cmd.cwd / "server.js").is_file():
        return Finding(cmd, Status.OK, "falls back to `node server.js`")
    if cmd.bin_fallback:
        bin_dir = cmd.cwd / "node_modules" / ".bin"
        if any((bin_dir / f"{cmd.target}{ext}").exists() for ext in ("", ".cmd")):
            return Finding(cmd, Status.OK, "resolves to an installed package binary")
        if cmd.target in info.dependencies:
            return Finding(cmd, Status.OK, f"runs the `{cmd.target}` package binary")

    match = _closest(cmd.target, info.scripts)
    suggestion = _canonical(cmd.runner, match, cmd.cwd, root) if match else None
    if suggestion is None:
        makefile = find_makefile(cmd.cwd)
        if makefile and cmd.target in load_makefile(makefile).targets:
            suggestion = _canonical("make", cmd.target, cmd.cwd, root)
        else:
            suggestion = _moved_to(cmd.target, cmd.cwd, root)
    if cmd.bin_fallback and suggestion is None:
        return Finding(cmd, Status.UNVERIFIABLE,
                       f"not a script; {cmd.runner} may resolve it as a package binary")
    return Finding(cmd, Status.STALE, f"no `{cmd.target}` script in {_display(package, root)}", suggestion)


def _replace_token(text: str, old: str, new: str) -> str:
    return " ".join(new if token == old else token for token in text.split(" "))


def _local_module(base: Path, parts: list[str]) -> str | None:
    """'ok' if `python -m` can run it from `base`, 'package' if it's a package it can't run."""
    path = base.joinpath(*parts)
    if path.with_suffix(".py").is_file():
        return "ok"
    if path.is_dir():
        if (path / "__main__.py").is_file():
            return "ok"
        if (path / "__init__.py").is_file() or any(path.glob("*.py")):
            return "package"
    return None


def _modules_in(package_dir: Path, prefix: str) -> list[str]:
    names = []
    for path in package_dir.rglob("*.py"):
        if SKIP_DIRS & set(path.relative_to(package_dir).parts):
            continue
        rel = path.relative_to(package_dir).with_suffix("")
        parts = [p for p in rel.parts if p != "__init__"]
        if parts and parts[-1] == "__main__":
            parts = parts[:-1]
        names.append(".".join([prefix, *parts]))
    return names


def _check_module(cmd: Command, root: Path) -> Finding:
    assert cmd.cwd is not None and cmd.target is not None
    module = cmd.target
    parts = module.split(".")
    if not all(part.isidentifier() for part in parts):
        return Finding(cmd, Status.UNVERIFIABLE, f"`{module}` is not a module name")

    # `python -m` imports from the working directory; src/ layouts work once installed.
    bases = [cmd.cwd, cmd.cwd / "src"]
    for base in bases:
        found = _local_module(base, parts)
        if found == "ok":
            return Finding(cmd, Status.OK, f"module in {_display(base, root)}")
        if found == "package":
            return Finding(cmd, Status.STALE,
                           f"`{module}` is a package without __main__.py, so `python -m` can't run it")
    for base in bases:
        top = base / parts[0]
        if len(parts) > 1 and (top / "__init__.py").is_file():
            match = _closest(module, _modules_in(top, parts[0]))
            return Finding(cmd, Status.STALE, f"no module `{module}` in {_display(top, root)}",
                           _replace_token(cmd.text, module, match) if match else None)

    if parts[0] in sys.stdlib_module_names or parts[0] == "pip":
        return Finding(cmd, Status.OK, "standard library")
    env = python_env(cmd.cwd, root)
    if _normalize(parts[0]) in env.dependencies:
        return Finding(cmd, Status.OK, f"provided by dependency `{parts[0]}`")
    if any((sp / parts[0]).is_dir() or (sp / f"{parts[0]}.py").is_file() for sp in env.site_packages):
        return Finding(cmd, Status.OK, "installed in .venv")
    # It may still be installed globally, so absence alone isn't proof of staleness.
    return Finding(cmd, Status.UNVERIFIABLE,
                   f"`{parts[0]}` is not a local module, the standard library, or a declared dependency")


def _check_entrypoint(cmd: Command, root: Path) -> Finding:
    assert cmd.cwd is not None and cmd.target is not None
    name = cmd.target
    env = python_env(cmd.cwd, root)
    if env.pyproject is None:
        return Finding(cmd, Status.UNVERIFIABLE, f"no pyproject.toml, so `{cmd.runner} run` looks it up on PATH")
    where = _display(env.pyproject, root)
    if name in env.scripts:
        return Finding(cmd, Status.OK, f"script in {where}")
    if _normalize(name) in env.dependencies:
        return Finding(cmd, Status.OK, f"provided by dependency `{name}`")
    if any((d / name).exists() or (d / f"{name}.exe").exists() for d in env.bin_dirs):
        return Finding(cmd, Status.OK, "installed in .venv")
    match = _closest(name, env.scripts)
    if match:
        return Finding(cmd, Status.STALE, f"no `{name}` script in {where}", _replace_token(cmd.text, name, match))
    return Finding(cmd, Status.UNVERIFIABLE,
                   f"not a project script or declared dependency; `{cmd.runner} run` may find it on PATH")


def _find_moved_file(name: str, root: Path) -> Path | None:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and d not in _ENVIRONMENT_DIRS)
        if name in filenames:
            return Path(dirpath) / name
    return None


def _check_file(cmd: Command, root: Path) -> Finding:
    assert cmd.cwd is not None and cmd.target is not None
    path = Path(os.path.normpath(cmd.cwd / cmd.target))
    if path.is_file():
        return Finding(cmd, Status.OK, "file exists")
    if path.is_dir():
        if cmd.runner in ("python", "uv", "poetry") and (path / "__main__.py").is_file():
            return Finding(cmd, Status.OK, "directory with __main__.py")
        return Finding(cmd, Status.STALE, f"`{cmd.target}` is a directory, not a script")
    moved = _find_moved_file(path.name, root)
    suggestion = None
    if moved:
        new = os.path.relpath(moved, cmd.cwd).replace(os.sep, "/")
        if cmd.target.startswith("./") and not new.startswith("."):
            new = f"./{new}"
        suggestion = _replace_token(cmd.text, cmd.target, new)
    return Finding(cmd, Status.STALE, f"no file `{cmd.target}`", suggestion)


_CHECKS = {"module": _check_module, "entrypoint": _check_entrypoint, "file": _check_file}


def check(cmd: Command, root: Path) -> Finding:
    if cmd.note:
        return Finding(cmd, Status.UNVERIFIABLE, cmd.note)
    if cmd.cwd is None:
        return Finding(cmd, Status.UNVERIFIABLE, "runs in a directory that can't be resolved statically")
    if cmd.kind in _CHECKS:
        return _CHECKS[cmd.kind](cmd, root)
    return _check_make(cmd, root) if cmd.runner == "make" else _check_script(cmd, root)


# --------------------------------------------------------------------------- #
# Files
# --------------------------------------------------------------------------- #


def project_root_for(context_file: Path) -> Path:
    """Commands in a context file run from its directory (`.claude/CLAUDE.md` -> project root)."""
    parent = context_file.parent
    return parent.parent if parent.name == ".claude" else parent


def _excluded(rel: str, patterns: Sequence[str]) -> bool:
    """Match a scan-relative POSIX path against patterns, by full path or by name."""
    name = rel.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatchcase(rel, p) or fnmatch.fnmatchcase(name, p) for p in patterns)


def discover(root: Path, exclude: Sequence[str] = DEFAULT_EXCLUDES) -> list[Path]:
    """Find agent context files under `root`, including nested AGENTS.md files.

    `exclude` patterns are matched relative to `root`, so scanning inside an
    excluded directory directly (`whyfence lint examples/demo`) still works.
    """
    patterns = [p.strip("/") for p in exclude if p.strip("/")]
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        prefix = "" if rel_dir == "." else f"{rel_dir}/"
        dirnames[:] = sorted(
            d for d in dirnames if d not in SKIP_DIRS and not _excluded(prefix + d, patterns)
        )
        found.extend(
            Path(dirpath) / name for name in CONTEXT_FILE_NAMES
            if name in filenames and not _excluded(prefix + name, patterns)
        )
    return found


def lint_file(path: Path, root: Path | None = None) -> list[Finding]:
    root = root or project_root_for(path)
    findings: list[Finding] = []
    seen: set[tuple[int, str, str | None]] = set()
    for snippet in extract_snippets(path.read_text(encoding="utf-8", errors="replace")):
        for cmd in parse_commands(snippet, root, path):
            key = (cmd.line, cmd.text, cmd.target)
            if key not in seen:
                seen.add(key)
                findings.append(check(cmd, root))
    return findings


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

_STYLES = {
    Status.OK: ("✓", "OK", "green"),
    Status.STALE: ("✗", "x", "bold red"),
    Status.MISSING_MANIFEST: ("✗", "x", "bold red"),
    Status.UNVERIFIABLE: ("?", "?", "yellow"),
}


def _render(results: dict[Path, list[Finding]], console: Console, base: Path, verbose: bool) -> None:
    fancy = (console.encoding or "").lower().startswith("utf")
    counts = {status: 0 for status in Status}
    for findings in results.values():
        for finding in findings:
            counts[finding.status] += 1
    total = sum(counts.values())
    failed = counts[Status.STALE] + counts[Status.MISSING_MANIFEST]

    console.print()
    for path, findings in results.items():
        shown = [f for f in findings if verbose or f.status is not Status.OK]
        bad = sum(f.failed for f in findings)
        header = Text.assemble(
            (_display(path, base), "bold"),
            (f"  {len(findings)} command{'s' * (len(findings) != 1)}", "grey50"),
        )
        if bad:
            header.append(f" · {bad} stale", "red")
        console.print(header)
        if not shown:
            console.print("  [green]all commands resolve[/]" if findings else "  [grey50]no commands found[/]")
            console.print()
            continue

        table = Table(box=None, show_header=False, padding=(0, 2, 0, 0), pad_edge=False)
        table.add_column(width=1, no_wrap=True)
        table.add_column(justify="right", no_wrap=True)
        table.add_column(style="bold", overflow="fold", max_width=40)
        table.add_column(overflow="fold", min_width=30)
        link = Style(color="grey50", link=path.resolve().as_uri())
        for finding in shown:
            icon, plain_icon, style = _STYLES[finding.status]
            problem = Text(finding.reason, style="red" if finding.failed else style if
                           finding.status is Status.UNVERIFIABLE else "grey50")
            if finding.suggestion:
                problem.append(f"\n{'→' if fancy else '->'} did you mean ", style="cyan")
                problem.append(finding.suggestion, style="bold cyan")
                problem.append("?", style="cyan")
            table.add_row(
                Text(icon if fancy else plain_icon, style=style),
                Text(f"L{finding.command.line}", style=link),
                Text(finding.command.text),
                problem,
            )
        console.print(table, style="", soft_wrap=False)
        console.print()

    files = f"{len(results)} file{'s' * (len(results) != 1)}"
    if failed:
        icon = "✗" if fancy else "x"
        summary = Text.assemble(
            (f"{icon} {failed} stale", "bold red"), "   ",
            (f"? {counts[Status.UNVERIFIABLE]} unverifiable", "yellow"), "   ",
            (f"{'✓' if fancy else 'OK'} {counts[Status.OK]} verified", "green"),
            (f"\n{total} commands across {files}. Agents reading these files will run "
             "commands that no longer exist.", "grey50"),
        )
        console.print(Panel(summary, border_style="red", expand=False))
    else:
        unverifiable = counts[Status.UNVERIFIABLE]
        extra = f" ({unverifiable} unverifiable)" if unverifiable else ""
        resolved = "The 1 command" if total == 1 else f"All {total} commands"
        console.print(Panel(
            Text(f"{'✓' if fancy else 'OK'} {resolved} across {files} resolve{'s' * (total == 1)}{extra}.",
                 style="green"),
            border_style="green", expand=False,
        ))


def _to_json(results: dict[Path, list[Finding]], base: Path) -> str:
    return json.dumps({
        "files": [
            {
                "path": _display(path, base),
                "findings": [
                    {
                        "line": f.command.line,
                        "command": f.command.text,
                        "runner": f.command.runner,
                        "kind": f.command.kind,
                        "target": f.command.target,
                        "status": f.status.value,
                        "reason": f.reason,
                        "suggestion": f.suggestion,
                    }
                    for f in findings
                ],
            }
            for path, findings in results.items()
        ],
    }, indent=2)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("paths", nargs="*", type=Path, default=[Path(".")],
                        help="context files or directories to scan (default: .)")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument("--strict", action="store_true", help="also fail on unverifiable commands")
    parser.add_argument("-v", "--verbose", action="store_true", help="show verified commands too")
    parser.add_argument("--exclude", action="append", default=[], metavar="PATTERN",
                        help="skip matching paths when scanning directories (glob on the relative path "
                             "or name; repeatable)")
    parser.add_argument("--no-default-excludes", action="store_true",
                        help=f"don't skip {', '.join(f'{d}/' for d in DEFAULT_EXCLUDES)} by default")
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    console = Console(highlight=False)
    base = Path.cwd()
    exclude = [*([] if args.no_default_excludes else DEFAULT_EXCLUDES), *args.exclude]
    files: list[Path] = []
    for path in args.paths:
        if path.is_dir():
            files.extend(discover(path, exclude))
        elif path.is_file():
            files.append(path)
        else:
            console.print(f"[red]error:[/] {escape(str(path))} does not exist")
            return 2

    if not files:
        if args.json:
            print(json.dumps({"files": []}))
        else:
            console.print(f"[grey50]No {', '.join(CONTEXT_FILE_NAMES)} files found.[/]")
        return 0

    results = {path: lint_file(path.resolve()) for path in files}
    if args.json:
        print(_to_json(results, base))
    else:
        _render(results, console, base, args.verbose)

    findings = [f for fs in results.values() for f in fs]
    if any(f.failed for f in findings) or (args.strict and any(f.status is Status.UNVERIFIABLE for f in findings)):
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="whyfence-lint", description=__doc__.splitlines()[0])
    add_arguments(parser)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

import json
from pathlib import Path

import pytest

from whyfence import linter
from whyfence.linter import Status, extract_snippets, lint_file


def write(root: Path, files: dict[str, str]) -> None:
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def package(scripts: dict[str, str], **extra) -> str:
    return json.dumps({"name": "demo", "scripts": scripts, **extra})


def results(root: Path, doc: str = "CLAUDE.md") -> list[tuple[str, Status]]:
    return [(f.command.text, f.status) for f in lint_file(root / doc)]


def test_renamed_npm_script_is_stale_with_suggestion(tmp_path):
    write(tmp_path, {
        "package.json": package({"test:unit": "vitest", "dev": "vite"}),
        "CLAUDE.md": "```bash\nnpm run test-unit\nnpm run dev\n```\n",
    })
    stale, ok = lint_file(tmp_path / "CLAUDE.md")
    assert stale.status is Status.STALE
    assert stale.suggestion == "npm run test:unit"
    assert ok.status is Status.OK


def test_npm_lifecycle_aliases(tmp_path):
    write(tmp_path, {
        "package.json": package({"test": "jest"}),
        "CLAUDE.md": "```sh\nnpm test\nnpm t\nnpm start\n```",
    })
    assert results(tmp_path) == [
        ("npm test", Status.OK), ("npm t", Status.OK), ("npm start", Status.STALE),
    ]


def test_make_targets_and_includes(tmp_path):
    write(tmp_path, {
        "Makefile": (
            "include mk/dev.mk\n"
            ".PHONY: test lint\n"
            "VERSION := 1.0\n"
            "test lint: deps\n\t@pytest\n"
            "deps::\n\tpip install .\n"
        ),
        "mk/dev.mk": "serve:\n\tpython -m http.server\n",
        "AGENTS.md": "Run `make test` and `make serve`, never `make deploy`.\n",
    })
    assert results(tmp_path, "AGENTS.md") == [
        ("make test", Status.OK), ("make serve", Status.OK), ("make deploy", Status.STALE),
    ]


def test_assignments_and_special_targets_are_not_targets(tmp_path):
    write(tmp_path, {
        "Makefile": "X ::= a\nY := b\n.PHONY: ghost\nreal:\n\techo\n",
        "CLAUDE.md": "```\nmake X\nmake ghost\nmake real\n```",
    })
    assert [s for _, s in results(tmp_path)] == [Status.STALE, Status.STALE, Status.OK]


def test_missing_target_that_is_a_file_explains_silent_noop(tmp_path):
    write(tmp_path, {"Makefile": "all:\n\techo\n", "build/x": "", "CLAUDE.md": "`make build`"})
    (finding,) = lint_file(tmp_path / "CLAUDE.md")
    assert finding.status is Status.STALE
    assert "silently does nothing" in finding.reason


def test_missing_makefile_suggests_package_script(tmp_path):
    write(tmp_path, {
        "package.json": package({"lint": "eslint ."}),
        "pnpm-lock.yaml": "",
        "CLAUDE.md": "`make lint`",
    })
    (finding,) = lint_file(tmp_path / "CLAUDE.md")
    assert finding.status is Status.MISSING_MANIFEST
    assert finding.suggestion == "pnpm run lint"


def test_cd_and_directory_flags_change_where_commands_resolve(tmp_path):
    write(tmp_path, {
        "web/package.json": package({"dev": "vite"}),
        "api/Makefile": "run:\n\tuvicorn app:app\n",
        "CLAUDE.md": (
            "```bash\n"
            "cd web && npm run dev\n"
            "npm --prefix web run dev\n"
            "make -C api run\n"
            "npm run dev\n"
            "cd $APP_DIR && npm run dev\n"
            "```\n"
        ),
    })
    assert results(tmp_path) == [
        ("npm run dev", Status.OK),
        ("npm --prefix web run dev", Status.OK),
        ("make -C api run", Status.OK),
        ("npm run dev", Status.MISSING_MANIFEST),
        ("npm run dev", Status.UNVERIFIABLE),
    ]


def test_suggestion_keeps_the_directory(tmp_path):
    write(tmp_path, {"api/Makefile": "test:\n\tpytest\n", "CLAUDE.md": "`make -C api tests`"})
    (finding,) = lint_file(tmp_path / "CLAUDE.md")
    assert finding.suggestion == "make -C api test"


def test_target_moved_into_subdirectory_is_suggested(tmp_path):
    write(tmp_path, {
        "services/api/Makefile": "migrate:\n\talembic upgrade head\n",
        "web/package.json": package({"storybook": "storybook dev"}),
        "yarn.lock": "",
        "CLAUDE.md": "`make migrate` `yarn run storybook`",
    })
    migrate, storybook = lint_file(tmp_path / "CLAUDE.md")
    assert migrate.suggestion == "make -C services/api migrate"
    assert storybook.status is Status.MISSING_MANIFEST
    assert storybook.suggestion == "npm --prefix web run storybook"


def test_makefile_name_reported_with_real_case(tmp_path):
    write(tmp_path, {"Makefile": "all:\n\techo\n", "CLAUDE.md": "`make nope`"})
    (finding,) = lint_file(tmp_path / "CLAUDE.md")
    assert "in Makefile" in finding.reason


def test_console_blocks_only_read_prompt_lines(tmp_path):
    write(tmp_path, {
        "package.json": package({"build": "tsc"}),
        "CLAUDE.md": "```console\n$ npm run build\nnpm run fake (this is output)\n```\n",
    })
    assert results(tmp_path) == [("npm run build", Status.OK)]


def test_non_shell_fences_and_prose_mentions_are_ignored(tmp_path):
    write(tmp_path, {
        "CLAUDE.md": "We don't use `make`.\n```json\n{\"cmd\": \"npm run nope\"}\n```\n",
    })
    assert results(tmp_path) == []


def test_line_continuations_redirects_and_env_prefixes(tmp_path):
    write(tmp_path, {
        "Makefile": "test:\n\tpytest\n",
        "CLAUDE.md": "```bash\nCI=1 make \\\n  test > out.log 2>&1\n```\n",
    })
    (finding,) = lint_file(tmp_path / "CLAUDE.md")
    assert finding.command.text == "make test"
    assert finding.command.line == 2
    assert finding.status is Status.OK


def test_yarn_and_pnpm_builtins_and_binaries(tmp_path):
    write(tmp_path, {
        "package.json": package({"lint": "eslint ."}, devDependencies={"eslint": "^9"}),
        "CLAUDE.md": (
            "```bash\n"
            "yarn install\n"
            "yarn add left-pad\n"
            "yarn lint\n"
            "yarn eslint\n"
            "yarn tsc\n"
            "yarn lnt\n"
            "pnpm -r build\n"
            "npm run --if-present typecheck\n"
            "```\n"
        ),
    })
    assert results(tmp_path) == [
        ("yarn lint", Status.OK),
        ("yarn eslint", Status.OK),
        ("yarn tsc", Status.UNVERIFIABLE),
        ("yarn lnt", Status.STALE),
        ("pnpm -r build", Status.UNVERIFIABLE),
        ("npm run --if-present typecheck", Status.OK),
    ]


def test_pattern_rules_and_dynamic_targets_are_unverifiable(tmp_path):
    write(tmp_path, {
        "Makefile": "test-%:\n\tpytest -k $*\n",
        "CLAUDE.md": "`make test-api` `make deploy`",
    })
    assert results(tmp_path) == [("make test-api", Status.UNVERIFIABLE), ("make deploy", Status.STALE)]


def test_dot_claude_file_resolves_against_project_root(tmp_path):
    write(tmp_path, {"Makefile": "test:\n\tpytest\n", ".claude/CLAUDE.md": "`make test`"})
    assert results(tmp_path, ".claude/CLAUDE.md") == [("make test", Status.OK)]


def test_fence_with_longer_closer_and_tildes():
    snippets = list(extract_snippets("~~~sh\nmake a\n~~~~\n````bash\nmake b\n```\nmake c\n````\n"))
    assert [s.text for s in snippets] == ["make a", "make b", "```", "make c"]


@pytest.mark.parametrize("stale, code", [(False, 0), (True, 1)])
def test_cli_exit_code_and_json(tmp_path, capsys, monkeypatch, stale, code):
    write(tmp_path, {
        "package.json": package({"dev": "vite"}),
        "CLAUDE.md": "`npm run dev`" + (" `npm run gone`" if stale else ""),
    })
    monkeypatch.chdir(tmp_path)
    assert linter.main(["--json"]) == code
    report = json.loads(capsys.readouterr().out)
    statuses = [f["status"] for f in report["files"][0]["findings"]]
    assert statuses == (["ok", "stale"] if stale else ["ok"])


def test_cli_strict_fails_on_unverifiable(tmp_path, monkeypatch):
    write(tmp_path, {"Makefile": "%:\n\techo\n", "CLAUDE.md": "`make anything`"})
    monkeypatch.chdir(tmp_path)
    assert linter.main([]) == 0
    assert linter.main(["--strict"]) == 1


def test_discover_skips_vendored_dirs(tmp_path):
    write(tmp_path, {
        "AGENTS.md": "",
        "packages/web/AGENTS.md": "",
        "node_modules/pkg/CLAUDE.md": "",
    })
    found = {p.relative_to(tmp_path).as_posix() for p in linter.discover(tmp_path)}
    assert found == {"AGENTS.md", "packages/web/AGENTS.md"}

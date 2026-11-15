"""Fixes from the 0.3.0 review: edit linking, side-branch commits, stale and retired rules."""

import json
from contextlib import closing

from test_symbols import Repo, needs_git, repo  # noqa: F401  (fixture)

from whyfence import queries, store, team
from whyfence.cli import main
from whyfence.ingest import events, git, symbols
from whyfence.trust import Trust

pytestmark = needs_git
ADA = Trust(frozenset({"ada@example.com"}))


def ingest(target: Repo, conn) -> None:
    git.ingest_git(conn, target.path)
    symbols.ingest_symbols(conn, target.path)
    events.ingest_events(conn, target.path, transcripts=False)
    team.import_shared(conn, target.path)


def ids_of(conn, *names: str) -> set[str]:
    return {store.find_symbols(conn, name)[0].id for name in names}


# --------------------------------------------------------------------------- #
# Issue 5: an edit whose text appears more than once links to nothing
# --------------------------------------------------------------------------- #

TWINS = (
    "def load(path):\n    if not path:\n        return None\n    return read(path)\n\n\n"
    "def save(path):\n    if not path:\n        return None\n    return write(path)\n"
)


def test_repeated_edit_text_is_not_guessed(repo):  # noqa: F811
    repo.commit("add", {"io.py": TWINS})
    with closing(store.connect()) as conn:
        ingest(repo, conn)
        with git.BlobReader(repo.path) as blobs:
            linker = events._Linker(conn, git.repo_root(repo.path), blobs)
            assert linker.symbols_for_edit("io.py", "    if not path:\n        return None\n") == set()
            assert linker.symbols_for_edit("io.py", "    return write(path)\n") == ids_of(conn, "save")
            assert linker.symbols_for_edit("io.py", "    return gone(path)\n") == set()


# --------------------------------------------------------------------------- #
# Issue 4: commits on a merged branch link to the symbols they changed
# --------------------------------------------------------------------------- #

APP = (
    "def parse(text):\n    if not text:\n        return []\n    return text.split(',')\n\n\n"
    "def render(items):\n    return ', '.join(items)\n"
)


def test_side_branch_commit_links_through_a_real_merge(repo):  # noqa: F811
    repo.commit("Add parser", {"app/csv.py": APP})
    repo.git("checkout", "-q", "-b", "feature")
    repo.commit("fix: parse crashed on None\n\nWhy: callers pass None for missing fields.",
                {"app/csv.py": APP.replace("if not text:", "if text is None or not text:")})
    repo.commit("Tidy render", {"app/csv.py": APP.replace("if not text:", "if text is None or not text:")
                                .replace("', '.join(items)", "', '.join(map(str, items))")})
    repo.git("checkout", "-q", "main")
    repo.commit("Unrelated work on main", {"README.md": "csv tools\n"})
    repo.git("merge", "-q", "--no-ff", "-m", "Merge branch 'feature'", "feature")
    assert len(repo.git("rev-list", "--merges", "HEAD").split()) == 1  # a real merge, not a squash
    with closing(store.connect()) as conn:
        ingest(repo, conn)
        why_parse = queries.lookup_why(conn, "parse", ADA)
        why_render = queries.lookup_why(conn, "render", ADA)
    assert [e["summary"] for e in why_parse["events"]] == ["fix: parse crashed on None"]
    assert why_parse["events"][0]["detail"] == "Why: callers pass None for missing fields."
    assert why_render["events"] == []  # the fix touched parse only


def test_side_branch_name_reused_over_time_is_settled_by_the_merge(repo):  # noqa: F811
    old = "def handler(event):\n    log(event)\n    return old_path(event)\n"
    repo.commit("Add handler", {"app/h.py": old})
    repo.commit("Remove handler", {"app/h.py": "def other():\n    return 1\n"})
    new = "def other():\n    return 1\n\n\ndef handler(event):\n    return new_path(event)\n"
    repo.commit("Bring handler back, rewritten", {"app/h.py": new})
    repo.git("checkout", "-q", "-b", "feature")
    repo.commit("fix: handler dropped events\n\nWhy: retries arrive twice.",
                {"app/h.py": new.replace("new_path(event)", "new_path(event, dedupe=True)")})
    repo.git("checkout", "-q", "main")
    repo.git("merge", "-q", "--no-ff", "-m", "Merge feature", "feature")
    with closing(store.connect()) as conn:
        ingest(repo, conn)
        # Two symbols have been called app.h.handler; the fix is about the one alive at the merge.
        assert len(store.versions_named(conn, "app/h.py", "app.h.handler")) == 2
        current = next(s for s in store.find_symbols(conn, "handler") if store.is_current(conn, s.id))
        linked = {sid for e in store.events_between(conn) if e.summary.startswith("fix: handler")
                  for sid in store.linked_symbol_ids(conn, e.id)}
    assert linked == {current.id}


# --------------------------------------------------------------------------- #
# Issue 3: stale and retired rules
# --------------------------------------------------------------------------- #

OPS = ("def deploy(target):\n    build(target)\n    return push(target)\n\n\n"
       "def rollback(target):\n    return undo(target)\n")


def with_rules(target: Repo) -> Repo:
    target.git("config", "user.email", "ada@example.com")
    target.git("config", "user.name", "Ada")
    target.commit("Add ops\n\nNever call deploy() from a request handler.", {"app/ops.py": OPS})
    # In a code file: since 0.4.1, a commit that only touches docs yields no events.
    target.commit("Add the release script\n\nAlways run the release script before pushing.",
                  {"scripts/release.py": "print('tag first')\n"})
    return target


def rule_texts(conn, **kwargs) -> list[str]:
    return [r["rule"] for r in queries.lookup_rules(conn, ADA, **kwargs)["rules"]]


def test_rule_about_deleted_code_is_stale(repo):  # noqa: F811
    with_rules(repo)
    repo.commit("Remove deploy", {"app/ops.py": "def rollback(target):\n    return undo(target)\n"})
    with closing(store.connect()) as conn:
        ingest(repo, conn)
        answer = queries.lookup_rules(conn, ADA)
        # The release rule names no function, so it never goes stale.
        assert [r["rule"] for r in answer["rules"]] == ["Always run the release script before pushing."]
        assert answer["stale_hidden"] == 1 and "deleted" in answer["message"]
        shown = queries.lookup_rules(conn, ADA, include_stale=True)["rules"]
        assert {r["rule"]: r["stale"] for r in shown} == {"Never call deploy() from a request handler.": True,
                                                         "Always run the release script before pushing.": False}


def test_retire_by_id_writes_the_shared_file_and_survives_a_rebuild(repo, capsys):  # noqa: F811
    with_rules(repo)
    path = str(repo.path)
    assert main(["ingest", path, "--no-transcripts"]) == 0
    with closing(store.connect(repo.path / ".whyfence/index.db")) as conn:
        rule = next(r for r in queries.lookup_rules(conn, ADA)["rules"] if "deploy()" in r["rule"])
    capsys.readouterr()
    assert main(["retire", rule["id"][:8], "--reason", "deploys moved to CI", "--repo", path]) == 0
    assert "Retired" in capsys.readouterr().out
    lines = (repo.path / ".whyfence/events/ada.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[-1])["retires"] == rule["id"]
    assert json.loads(lines[-1])["reason"] == "deploys moved to CI"

    with closing(store.connect(repo.path / ".whyfence/index.db")) as conn:
        assert rule_texts(conn) == ["Always run the release script before pushing."]
        assert queries.lookup_rules(conn, ADA)["retired_hidden"] == 1
        assert queries.lookup_why(conn, "deploy", ADA)["events"] == []
        assert queries.lookup_search(conn, "request handler", ADA)["events"] == []
    assert main(["retire", rule["id"][:8], "--repo", path]) == 0
    assert "Already retired" in capsys.readouterr().out
    assert main(["retire", "--list", "--repo", path]) == 0
    listed = capsys.readouterr().out
    assert "request handler" in listed and "deploys moved to CI" in listed

    (repo.path / ".whyfence/index.db").unlink()  # rebuild from scratch: the shared file brings it back
    for suffix in ("-wal", "-shm"):
        (repo.path / f".whyfence/index.db{suffix}").unlink(missing_ok=True)
    with closing(store.connect()) as conn:
        ingest(repo, conn)
        assert rule_texts(conn) == ["Always run the release script before pushing."]


def test_retire_rejects_unknown_short_and_local_stays_local(repo, capsys):  # noqa: F811
    with_rules(repo)
    path = str(repo.path)
    assert main(["ingest", path, "--no-transcripts"]) == 0
    assert main(["retire", "abc", "--repo", path]) == 2
    assert main(["retire", "ffffffffff", "--repo", path]) == 2
    with closing(store.connect(repo.path / ".whyfence/index.db")) as conn:
        rule = queries.lookup_rules(conn, ADA)["rules"][0]
    assert main(["retire", rule["id"], "--local", "--repo", path]) == 0
    assert not (repo.path / ".whyfence/events").exists()
    with closing(store.connect(repo.path / ".whyfence/index.db")) as conn:
        assert queries.lookup_rules(conn, ADA)["retired_hidden"] == 1


def test_only_trusted_retirements_count(repo):  # noqa: F811
    with_rules(repo)
    with closing(store.connect()) as conn:
        ingest(repo, conn)
        rule = next(r for r in queries.lookup_rules(conn, ADA)["rules"] if "deploy()" in r["rule"])
    record = {"v": 1, "id": "e" * 32, "retires": rule["id"], "reason": "obsolete",
              "retired_at": "2026-03-01T00:00:00Z"}
    repo.write({".whyfence/events/ada.jsonl": json.dumps(record) + "\n"})
    repo.git("add", "-A")
    repo.git("-c", "user.name=Mallory", "-c", "user.email=mallory@evil.example", "commit", "-q",
             "--author=Mallory <mallory@evil.example>", "-m", "Clean up rules")
    with closing(store.connect()) as conn:
        ingest(repo, conn)
        assert "Never call deploy() from a request handler." in rule_texts(conn)  # Mallory can't silence it
        everyone = Trust(everyone=True)
        assert "Never call deploy() from a request handler." not in [
            r["rule"] for r in queries.lookup_rules(conn, everyone)["rules"]]


def test_check_honours_retirements_from_the_base_only(repo):  # noqa: F811
    from whyfence import check

    with_rules(repo)
    repo.commit("fix: deploy pushed twice\n\nWhy: the CLI retried on its own.",
                {"app/ops.py": OPS.replace("build(target)", "build(target)  # once")})
    with closing(store.connect()) as conn:
        ingest(repo, conn)
        fix = next(e for e in store.events_between(conn) if e.summary.startswith("fix: deploy"))
    retirement = {"v": 1, "id": "f" * 32, "retires": fix.id, "reason": "CLI no longer retries",
                  "retired_at": "2026-03-01T00:00:00Z"}

    repo.git("checkout", "-q", "-b", "pr")  # a PR that retires the fence it trips
    repo.commit("Edit deploy and retire its history", {
        "app/ops.py": OPS.replace("build(target)", "build(target, fast=True)"),
        ".whyfence/events/ada.jsonl": json.dumps(retirement) + "\n"})
    with closing(store.connect()) as conn:
        ingest(repo, conn)
        summaries = [e["summary"] for f in check.check(conn, repo.path, "main").fences for e in f.events]
    assert "fix: deploy pushed twice" in summaries

    repo.git("checkout", "-q", "main")  # the same retirement, already on main
    repo.commit("Retire stale history", {".whyfence/events/ada.jsonl": json.dumps(retirement) + "\n"})
    repo.git("checkout", "-q", "-b", "pr2")
    repo.commit("Edit deploy", {"app/ops.py": OPS.replace("build(target)", "build(target, fast=True)")})
    with closing(store.connect()) as conn:
        ingest(repo, conn)
        summaries = [e["summary"] for f in check.check(conn, repo.path, "main").fences for e in f.events]
    assert "fix: deploy pushed twice" not in summaries

"""Draw the fresh blind sample for 0.4.1: 20 events per kind, none labelled before.

    uv run python eval/sample_fresh.py flask=f.db requests=r.db click=c.db gin=g.db django=d.db > out.csv

Five per kind from each of flask, requests, click and gin (a Go project); a repository
with fewer gives its share to the others, and django fills what is still missing (it is
needed for rules: the other four have few commit-mined rules, and earlier samples used
most of them). Rows already in eval/labels.jsonl or eval/blind-80-*.csv are left out.
The CSV has the same columns as the 80-row file, with `my_label` empty.
"""

from __future__ import annotations

import csv
import json
import random
import sqlite3
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
SEED = 20261118
PER_KIND = 20
BASE = ("flask", "requests", "click", "gin")
FALLBACK = "django"
KINDS = ("incident", "decision", "revert", "constraint")
GITHUB = {"flask": "pallets/flask", "requests": "psf/requests", "click": "pallets/click",
          "gin": "gin-gonic/gin", "django": "django/django"}


def labelled() -> set[tuple[str, str, str]]:
    seen = {(r["repo"], r["kind"], r["sha"][:12])
            for r in map(json.loads, (HERE / "labels.jsonl").read_text(encoding="utf-8").splitlines())}
    for row in csv.DictReader((HERE / "blind-80-reviewer.csv").open(encoding="utf-8-sig")):
        seen.add((row["repo"], row["kind"], row["sha"][:12]))
    return seen


def pool(db: str, repo: str, kind: str, seen: set) -> list[tuple[str, str]]:
    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT commit_sha, summary FROM events WHERE kind = ? AND commit_sha IS NOT NULL "
                        "ORDER BY commit_sha", (kind,)).fetchall()
    conn.close()
    return [(sha, summary) for sha, summary in rows if (repo, kind, sha[:12]) not in seen]


def draw(dbs: dict[str, str]) -> list[dict]:
    seen = labelled()
    rng = random.Random(SEED)
    chosen = []
    for kind in KINDS:
        pools = {repo: pool(dbs[repo], repo, kind, seen) for repo in (*BASE, FALLBACK)}
        for repo in pools:
            rng.shuffle(pools[repo])
        picked: list[tuple[str, str, str]] = []
        quota = PER_KIND // len(BASE)
        for repo in BASE:  # five each, as far as each has them
            picked += [(repo, sha, summary) for sha, summary in pools[repo][:quota]]
            pools[repo] = pools[repo][quota:]
        leftovers = [(repo, sha, summary) for repo in BASE for sha, summary in pools[repo]]
        rng.shuffle(leftovers)
        picked += leftovers[:PER_KIND - len(picked)]
        picked += [(FALLBACK, sha, summary) for sha, summary in pools[FALLBACK][:PER_KIND - len(picked)]]
        chosen += [{"repo": repo, "kind": kind, "sha": sha, "summary": summary} for repo, sha, summary in picked]
    order = list(range(len(chosen)))
    rng.shuffle(order)  # mixed, so kinds don't come in blocks
    return [chosen[i] for i in order]


def main() -> int:
    specs = dict(spec.split("=", 1) for spec in sys.argv[1:] if "=" in spec)
    clones = Path(sys.argv[sys.argv.index("--clones") + 1]) if "--clones" in sys.argv else None
    rows = draw(specs)
    writer = csv.writer(sys.stdout)
    writer.writerow(["n", "repo", "kind", "sha", "commit_url", "whyfence_summary", "full_commit_message", "my_label"])
    for n, row in enumerate(rows, start=1):
        message = ""
        if clones is not None:
            message = subprocess.run(["git", "-C", str(clones / row["repo"]), "show", "-s", "--format=%B", row["sha"]],
                                     capture_output=True, text=True, encoding="utf-8", errors="replace").stdout.strip()
        writer.writerow([n, row["repo"], row["kind"], row["sha"][:12],
                         f"https://github.com/{GITHUB[row['repo']]}/commit/{row['sha']}", row["summary"], message, ""])
    return 0


if __name__ == "__main__":
    sys.exit(main())

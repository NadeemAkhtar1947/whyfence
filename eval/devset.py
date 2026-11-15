"""Before/after on the 80-row development set, from both labellers' labels.

    uv run python eval/devset.py flask=flask.db requests=requests.db click=click.db django=django.db

The 80 rows of eval/blind-80-*.csv were drawn from indexes built by whyfence 0.4.0, so
"before" is every row. "After" keeps the rows whose event a newer whyfence still mines
from the given indexes. The 0.4.1 filters were designed from these rows' rejections, so
this is a development-set number, not an unbiased measure: see eval/README.md.
"""

from __future__ import annotations

import csv
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).parent
KINDS = ("incident", "decision", "revert", "constraint")


def load() -> list[dict]:
    claude = {r["n"]: r["claude_label"] == "correct"
              for r in csv.DictReader((HERE / "blind-80-claude.csv").open(encoding="utf-8"))}
    rows = []
    for row in csv.DictReader((HERE / "blind-80-reviewer.csv").open(encoding="utf-8-sig")):
        rows.append({"n": int(row["n"]), "repo": row["repo"], "kind": row["kind"], "sha": row["sha"],
                     "summary": row["whyfence_summary"], "claude": claude[row["n"]],
                     "reviewer": row["reviewer_label"].strip().lower() == "y"})
    return rows


def _norm(text: str) -> str:
    return " ".join(text.split())


def kept(rows: list[dict], dbs: dict[str, str]) -> tuple[set[int], dict[int, str]]:
    """Rows whose event is still mined with the same text, and rows whose text changed.

    A changed text is a different claim: its label no longer applies, so it counts as
    neither kept nor dropped.
    """
    alive: set[int] = set()
    changed: dict[int, str] = {}
    for repo, db in dbs.items():
        conn = sqlite3.connect(db)
        events: dict[tuple[str, str], list[str]] = defaultdict(list)
        for kind, sha, summary in conn.execute(
                "SELECT kind, commit_sha, summary FROM events WHERE commit_sha IS NOT NULL"):
            events[(kind, sha[:12])].append(_norm(summary))
        conn.close()
        for r in rows:
            if r["repo"] != repo or (r["kind"], r["sha"]) not in events:
                continue
            now = events[(r["kind"], r["sha"])]
            if _norm(r["summary"]) in now:
                alive.add(r["n"])
            else:
                changed[r["n"]] = now[0]
    return alive, changed


def main() -> int:
    dbs = dict(spec.split("=", 1) for spec in sys.argv[1:])
    rows = load()
    alive, changed = kept(rows, dbs)
    print("Development set (80 rows the 0.4.1 filters were designed from; not an unbiased measure)\n")
    print("| kind | labeller | before | after | rows dropped | of which labelled correct |")
    print("|---|---|---|---|---|---|")
    for kind in KINDS:
        group = [r for r in rows if r["kind"] == kind]
        after = [r for r in group if r["n"] in alive]
        dropped = [r for r in group if r["n"] not in alive and r["n"] not in changed]
        for who in ("claude", "reviewer"):
            right_before = sum(r[who] for r in group)
            right_after = sum(r[who] for r in after)
            pct_after = f"{right_after}/{len(after)} = {right_after / len(after):.0%}" if after else "-"
            label = "rule" if kind == "constraint" else kind
            print(f"| {label} | {who} | {right_before}/{len(group)} = {right_before / len(group):.0%} | "
                  f"{pct_after} | {len(dropped)} | {sum(r[who] for r in dropped)} |")
    if changed:
        print("\nRows whose event text changed (a new, unlabelled claim; excluded from 'after'):")
        for r in sorted((r for r in rows if r["n"] in changed), key=lambda r: r["n"]):
            print(f"  #{r['n']:<3} {r['kind']:<10} was: {_norm(r['summary'])[:70]}")
            print(f"  {'':<14} now: {changed[r['n']][:70]}")
    print("\nDropped rows:")
    for r in sorted((r for r in rows if r["n"] not in alive and r["n"] not in changed), key=lambda r: r["n"]):
        marks = f"Claude {'✓' if r['claude'] else '✗'}, reviewer {'✓' if r['reviewer'] else '✗'}"
        print(f"  #{r['n']:<3} {r['kind']:<10} {marks}  {' '.join(r['summary'].split())[:80]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Precision of commit-mined events, from the hand labels in eval/labels.jsonl.

    uv run python eval/precision.py                     # the table in eval/README.md
    uv run python eval/precision.py --check flask=x.db  # also confirm an index still yields the sample

Each label says whether a sampled event is what its kind claims (see eval/README.md
for the criteria). Precision only: nothing here measures what whyfence misses.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).parent
KINDS = ("incident", "decision", "revert", "constraint")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--labels", type=Path, default=HERE / "labels.jsonl")
    parser.add_argument("--check", nargs="*", default=[], metavar="REPO=DB",
                        help="whyfence indexes to re-draw the sample from (see sample.py)")
    args = parser.parse_args()
    rows = [json.loads(line) for line in args.labels.read_text(encoding="utf-8").splitlines() if line.strip()]
    repos = sorted({r["repo"] for r in rows})
    tally: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    for row in rows:
        for key in ((row["kind"], row["repo"]), (row["kind"], "all"), ("all", row["repo"]), ("all", "all")):
            tally[key][0] += bool(row["correct"])
            tally[key][1] += 1

    def cell(kind: str, repo: str) -> str:
        right, total = tally.get((kind, repo), (0, 0))
        return f"{right}/{total}" if total else "-"

    print(f"| kind | {' | '.join(repos)} | **all** |")
    print("|---|" + "---|" * (len(repos) + 1))
    for kind in (*KINDS, "all"):
        right, total = tally[(kind, "all")]
        label = "rule (constraint)" if kind == "constraint" else kind
        print(f"| {label} | {' | '.join(cell(kind, r) for r in repos)} | **{right}/{total} = {right / total:.0%}** |")

    if args.check:
        from sample import draw  # same folder

        drawn = {(r["repo"], r["kind"], r["sha"]) for r in draw(args.check)}
        checked = {spec.split("=")[0] for spec in args.check}
        labelled = {(r["repo"], r["kind"], r["sha"]) for r in rows if r["repo"] in checked}
        if drawn != labelled:
            print(f"\nthe index yields a different sample: {len(drawn - labelled)} new, "
                  f"{len(labelled - drawn)} gone; the labels describe an older classifier", file=sys.stderr)
            return 1
        print("\nthe index yields exactly the labelled sample")
    return 0


if __name__ == "__main__":
    sys.exit(main())

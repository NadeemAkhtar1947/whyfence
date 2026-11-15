"""Compare two labellers on the same sample: precision per kind for each, agreement, Cohen's kappa.

    uv run python eval/compare.py fresh-80-claude.csv fresh-80-reviewer.csv [--disagreements out.csv]

The first file has `claude_label` (correct/incorrect) and `claude_note`; the second has
`reviewer_label` (y/n), `reviewer_borderline` and `reviewer_reason`. Precision is per kind
only: the samples are stratified, so no overall number is computed.
"""

from __future__ import annotations

import csv
import sys
from collections import Counter, defaultdict
from pathlib import Path

KINDS = ("incident", "decision", "revert", "constraint")


def kappa(pairs: list[tuple[bool, bool]]) -> float:
    total = len(pairs)
    observed = sum(a == b for a, b in pairs) / total
    pa, pb = sum(a for a, _ in pairs) / total, sum(b for _, b in pairs) / total
    expected = pa * pb + (1 - pa) * (1 - pb)
    return (observed - expected) / (1 - expected) if expected < 1 else 1.0


def repos(rows: list[dict]) -> str:
    counts = Counter(r["repo"] for r in rows)
    return ", ".join(f"{repo} {n}" for repo, n in counts.most_common())


def main() -> int:
    mine_path, theirs_path = Path(sys.argv[1]), Path(sys.argv[2])
    out = Path(sys.argv[sys.argv.index("--disagreements") + 1]) if "--disagreements" in sys.argv else None
    mine = {r["n"]: r for r in csv.DictReader(mine_path.open(encoding="utf-8-sig"))}
    theirs = list(csv.DictReader(theirs_path.open(encoding="utf-8-sig")))
    by_kind: dict[str, list[tuple[dict, bool, bool]]] = defaultdict(list)
    for row in theirs:
        c = mine[row["n"]]["claude_label"] == "correct"
        r = row["reviewer_label"].strip().lower() == "y"
        by_kind[row["kind"]].append((row, c, r))

    print("| kind | rows (repositories) | Claude | reviewer | agreement | Cohen's κ |")
    print("|---|---|---|---|---|---|")
    for kind in KINDS:
        group = by_kind[kind]
        n = len(group)
        c = sum(x[1] for x in group)
        r = sum(x[2] for x in group)
        agree = sum(x[1] == x[2] for x in group)
        rows = [x[0] for x in group]
        label = "rule" if kind == "constraint" else kind
        print(f"| {label} | {n} ({repos(rows)}) | {c}/{n} = {c / n:.0%} | {r}/{n} = {r / n:.0%} | "
              f"{agree}/{n} | {kappa([(x[1], x[2]) for x in group]):.2f} |")

    disagreements = sorted(((int(row["n"]), row, c, r) for kind in KINDS for row, c, r in by_kind[kind] if c != r),
                           key=lambda d: d[0])
    borderline = sum(1 for _, row, _, _ in disagreements if row.get("reviewer_borderline", "").strip())
    claude_borderline = sum(1 for n, _, _, _ in disagreements if mine[str(n)].get("claude_borderline", "").strip())
    print(f"\n{len(disagreements)} disagreements ({borderline} marked borderline by the reviewer, "
          f"{claude_borderline} by Claude):")
    for n, row, c, r in disagreements:
        note = mine[str(n)].get("claude_note", "")
        rb = " (reviewer: borderline)" if row.get("reviewer_borderline", "").strip() else ""
        cb = " (Claude: borderline)" if mine[str(n)].get("claude_borderline", "").strip() else ""
        print(f"\n#{n} {row['repo']} {row['kind']} {row['sha']}: {' '.join(row['whyfence_summary'].split())[:105]}")
        print(f"   Claude:   {'correct' if c else 'incorrect'}{': ' + note if note else ''}{cb}")
        print(f"   reviewer: {'correct' if r else 'incorrect'}: {row['reviewer_reason'].strip()}{rb}")

    if out is not None:
        with out.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["n", "repo", "kind", "sha", "commit_url", "whyfence_summary", "claude_label",
                             "claude_borderline", "claude_note", "reviewer_label", "reviewer_borderline",
                             "reviewer_reason"])
            for n, row, c, r in disagreements:
                m = mine[str(n)]
                writer.writerow([n, row["repo"], row["kind"], row["sha"], row["commit_url"], row["whyfence_summary"],
                                 "correct" if c else "incorrect", m.get("claude_borderline", ""),
                                 m.get("claude_note", ""), "correct" if r else "incorrect",
                                 row.get("reviewer_borderline", ""), row["reviewer_reason"]])
    return 0


if __name__ == "__main__":
    sys.exit(main())

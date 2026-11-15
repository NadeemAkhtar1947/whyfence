"""Draw the precision sample from whyfence indexes: up to 10 commit events per kind per repo.

    uv run python eval/sample.py flask=flask.db requests=requests.db click=click.db > sample.jsonl

Each (repo, kind) pair has its own fixed random seed, so the same index always
yields the same sample. Build each index with `whyfence ingest REPO --db X.db
--no-transcripts` at the commits listed in eval/README.md.
"""

from __future__ import annotations

import json
import random
import sqlite3
import sys

KINDS = ("incident", "decision", "revert", "constraint")
PER_KIND = 10


def draw(specs: list[str]) -> list[dict]:
    sample = []
    for spec in specs:
        repo, db = spec.split("=", 1)
        conn = sqlite3.connect(db)
        for kind in KINDS:
            rows = conn.execute("""SELECT commit_sha, summary, body FROM events
                                   WHERE kind = ? AND source_id LIKE 'commit:%' ORDER BY commit_sha""",
                                (kind,)).fetchall()
            for sha, summary, body in random.Random(f"{repo}:{kind}").sample(rows, min(PER_KIND, len(rows))):
                sample.append({"repo": repo, "kind": kind, "sha": sha, "summary": summary, "body": body[:400]})
        conn.close()
    return sample


if __name__ == "__main__":
    for record in draw(sys.argv[1:]):
        print(json.dumps(record, ensure_ascii=False))

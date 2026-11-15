"""Time whyfence on a real repository: each ingest stage, then the queries agents make.

    uv run python scripts/bench.py PATH/TO/REPO              # stages + queries
    uv run python scripts/bench.py PATH/TO/REPO --profile    # + where the symbols stage spends its time
    uv run python scripts/bench.py PATH/TO/REPO --keep x.db  # keep the index for poking at

It builds a fresh index in a temporary folder (agent sessions are not read) and
never writes to the repository. "symbols_changed_in" is timed twice, with and
without the version indexes added in schema v4, to show what they buy.
"""

from __future__ import annotations

import argparse
import cProfile
import io
import pstats
import random
import shutil
import sqlite3
import statistics
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path

from whyfence import queries, store
from whyfence.ingest import events, git, symbols
from whyfence.trust import Trust


def timed(label: str, func, *args, **kwargs):
    start = time.perf_counter()
    result = func(*args, **kwargs)
    print(f"  {label:<34} {time.perf_counter() - start:8.2f} s")
    return result


def per_call(func, inputs: list, repeat: int = 1) -> float:
    """Median milliseconds per call."""
    samples = []
    for value in inputs:
        start = time.perf_counter()
        for _ in range(repeat):
            func(value)
        samples.append((time.perf_counter() - start) * 1000 / repeat)
    return statistics.median(samples) if samples else 0.0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("repo", type=Path)
    parser.add_argument("--profile", action="store_true", help="profile the symbols stage")
    parser.add_argument("--keep", type=Path, metavar="DB", help="copy the index here when done")
    args = parser.parse_args()
    root = git.repo_root(args.repo)
    folder = Path(tempfile.mkdtemp(prefix="whyfence-bench-"))
    db = folder / "index.db"
    try:
        print(f"{root.name}: {git.count_commits(root)} commits "
              f"({git.count_commits(root, first_parent=True)} first-parent)")
        print("ingest")
        with closing(store.connect(db)) as conn:
            timed("read commits", git.ingest_git, conn, root)
            if args.profile:
                profiler = cProfile.Profile()
                profiler.enable()
            timed("index functions and classes", symbols.ingest_symbols, conn, root)
            if args.profile:
                profiler.disable()
            timed("mine commit messages", events.ingest_events, conn, root, transcripts=False)
            counts = {table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                      for table in ("symbols", "symbol_versions", "events", "event_links")}
            print("  " + ", ".join(f"{n} {t}" for t, n in counts.items()))

            print("queries (median per call)")
            shas = [row[0] for row in conn.execute("SELECT sha FROM commits")]
            sample = random.Random(0).sample(shas, min(200, len(shas)))
            with_index = per_call(lambda sha: store.symbols_changed_in(conn, sha), sample)
            print(f"  {'symbols_changed_in':<34} {with_index:8.3f} ms")
            trust = Trust(everyone=True)
            busiest = conn.execute("""SELECT s.qualified_name FROM event_links l JOIN symbols s ON s.id = l.symbol_id
                                      GROUP BY l.symbol_id ORDER BY count(*) DESC LIMIT 5""").fetchall()
            names = [row[0] for row in busiest]
            print(f"  {'why (5 most-discussed symbols)':<34} "
                  f"{per_call(lambda n: queries.lookup_why(conn, n, trust), names):8.3f} ms")
            print(f"  {'rules':<34} {per_call(lambda _: queries.lookup_rules(conn, trust), [0], 5):8.3f} ms")
            print(f"  {'search':<34} "
                  f"{per_call(lambda w: queries.lookup_search(conn, w, trust), ['fix', 'error', 'test']):8.3f} ms")

        # The same lookup on a copy without the v4 indexes, to show what they buy.
        bare = folder / "bare.db"
        shutil.copy(db, bare)
        raw = sqlite3.connect(bare)
        raw.executescript("DROP INDEX symbol_versions_from; DROP INDEX symbol_versions_to;")
        sql = ("SELECT DISTINCT symbol_id FROM symbol_versions WHERE valid_from_sha = ? OR valid_to_sha = ? "
               "ORDER BY symbol_id")
        without = per_call(lambda sha: raw.execute(sql, (sha, sha)).fetchall(), sample[:50])
        raw.close()
        print(f"  {'symbols_changed_in, no v4 indexes':<34} {without:8.3f} ms")

        if args.profile:
            out = io.StringIO()
            pstats.Stats(profiler, stream=out).sort_stats("cumulative").print_stats(18)
            print("\nsymbols stage profile (cumulative)\n" + out.getvalue())
        if args.keep:
            shutil.copy(db, args.keep)
            print(f"index kept at {args.keep}")
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

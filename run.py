"""Run the whole Sonic Iceberg pipeline with one command.

    python run.py                          # sample data -> data/iceberg.html
    python run.py --src data/real          # your own export
    python run.py --fresh                  # delete the database first (fixes stale tables)
    python run.py --skip-recs              # faster: no recommendation lookups
    python run.py --title "Achal's iceberg"

Steps, in order:
    ingest    JSON export -> DuckDB `plays`
    enrich    Last.fm + Deezer -> `artists`          (cached; only new artists cost API calls)
    stats     -> `artist_stats`
    features  -> `iceberg_tiers`, `dropped_artists`, `taste_depth`
    recommend -> `recommendations`                   (optional)
    render    -> a single HTML file
"""

import argparse
import sys
import time
from pathlib import Path

# Make `iceberg` importable when run from the project root.
sys.path.insert(0, str(Path(__file__).parent / "src"))

import duckdb  # noqa: E402

from iceberg import enrich, features, ingest, recommend, render, stats  # noqa: E402

# Cache tables and the columns they must have. If a table exists with an
# older shape (e.g. before deezer_fans was added), it's dropped and rebuilt
# instead of crashing the run.
EXPECTED_COLUMNS = {
    "artists": {"deezer_fans"},
    "candidate_cache": {"canonical"},
}


def drop_stale_tables(db: Path) -> None:
    if not db.exists():
        return
    con = duckdb.connect(str(db))
    for table, required in EXPECTED_COLUMNS.items():
        cols = {r[0] for r in con.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = ?", [table]
        ).fetchall()}
        if cols and not required <= cols:
            print(f"  {table}: outdated schema, rebuilding")
            con.execute(f"DROP TABLE {table}")
    con.close()


def step(name: str):
    """Tiny decorator-free timer: prints a header, returns a closure that prints elapsed."""
    print(f"\n== {name} ==")
    t0 = time.perf_counter()
    return lambda: print(f"   ({time.perf_counter() - t0:.1f}s)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--src", type=Path, default=Path("data/samples"),
                        help="folder containing Streaming_History_Audio_*.json")
    parser.add_argument("--db", type=Path, default=Path("data/iceberg.duckdb"))
    parser.add_argument("--out", type=Path, default=Path("data/iceberg.html"))
    parser.add_argument("--title", default="Your music iceberg")
    parser.add_argument("--fresh", action="store_true", help="delete the database before running")
    parser.add_argument("--skip-recs", action="store_true", help="skip recommendation lookups")
    parser.add_argument("--churn-plays", type=int, default=20)
    parser.add_argument("--churn-gap", type=int, default=180)
    args = parser.parse_args()

    if args.fresh and args.db.exists():
        args.db.unlink()
        print(f"Deleted {args.db}")
    drop_stale_tables(args.db)

    done = step("ingest")
    n = ingest.ingest(args.src, args.db)
    print(f"   {n:,} plays from {args.src}")
    done()

    done = step("enrich")
    enrich.enrich(args.db)
    done()

    done = step("stats")
    stats.build(args.db)
    done()

    done = step("features")
    features.build(args.db, args.churn_plays, args.churn_gap)
    done()

    if not args.skip_recs:
        done = step("recommend")
        recommend.recommend(args.db, n_genres=3, per_genre=5, max_listeners=750_000)
        done()

    done = step("render")
    render.render(args.db, args.out, args.title)
    done()

    print(f"\nDone. Open {args.out}")


if __name__ == "__main__":
    main()

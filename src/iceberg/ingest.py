"""Load Spotify Extended Streaming History JSON files into DuckDB.

Reads every Streaming_History_Audio_*.json in a folder, keeps only music
plays (drops podcasts), renames Spotify's long column names to clean ones,
and writes a `plays` table. Re-running fully rebuilds the table, so it is
safe to run as many times as you like.

Usage:
    python src/iceberg/ingest.py                       # data/samples -> data/iceberg.duckdb
    python src/iceberg/ingest.py --src data/real --db data/iceberg.duckdb
"""

import argparse
from pathlib import Path

import duckdb

DEFAULT_SRC = Path("data/samples")
DEFAULT_DB = Path("data/iceberg.duckdb")

# DuckDB can read a glob of JSON files straight into a table.
# We select and rename only the columns the project needs.
CREATE_PLAYS = """
CREATE OR REPLACE TABLE plays AS
SELECT
    CAST(ts AS TIMESTAMP)                          AS played_at,
    ms_played,
    ms_played / 60000.0                            AS minutes_played,
    master_metadata_track_name                     AS track_name,
    master_metadata_album_artist_name              AS artist_name,
    master_metadata_album_album_name               AS album_name,
    spotify_track_uri                              AS track_uri,
    -- 'spotify:track:abc123' -> 'abc123'; this id is what the Spotify API wants
    split_part(spotify_track_uri, ':', 3)          AS track_id,
    platform,
    reason_start,
    reason_end,
    shuffle,
    skipped,
    offline
FROM read_json_auto(?, format = 'array')
WHERE spotify_track_uri IS NOT NULL     -- podcasts have episode_uri instead; drop them
  AND ms_played > 0                     -- zero-length rows are noise
ORDER BY played_at
"""


def ingest(src: Path, db: Path) -> int:
    """Rebuild the plays table from all JSON files in `src`. Returns row count."""
    pattern = str(src / "Streaming_History_Audio_*.json")
    db.parent.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect(str(db))
    con.execute(CREATE_PLAYS, [pattern])
    (n_rows,) = con.execute("SELECT COUNT(*) FROM plays").fetchone()
    con.close()
    return n_rows


def summary(db: Path) -> None:
    """Print a few sanity checks so you can eyeball the load."""
    con = duckdb.connect(str(db), read_only=True)
    print("\nDate range:")
    print(con.execute("SELECT MIN(played_at), MAX(played_at) FROM plays").fetchone())
    print("\nTop artists by plays:")
    for row in con.execute("""
        SELECT artist_name, COUNT(*) AS plays, ROUND(SUM(minutes_played) / 60, 1) AS hours
        FROM plays GROUP BY artist_name ORDER BY plays DESC LIMIT 5
    """).fetchall():
        print(f"  {row[0]:<20} {row[1]:>6} plays  {row[2]:>6} h")
    con.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--src", type=Path, default=DEFAULT_SRC)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    args = parser.parse_args()

    n = ingest(args.src, args.db)
    print(f"Loaded {n:,} plays from {args.src} into {args.db}")
    summary(args.db)


if __name__ == "__main__":
    main()

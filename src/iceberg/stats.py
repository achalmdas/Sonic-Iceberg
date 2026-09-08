"""Build the artist_stats table: one row per artist with listening totals.

Joins `plays` (your listening history) with `artists` (Last.fm enrichment)
and aggregates per artist:
    plays, hours, avg_minutes_per_play, skip_rate, first_played,
    last_played, days_since_last, top_track, listeners, tags

Everything downstream (iceberg tiers, churn detection, the renderer)
reads from this table instead of re-aggregating raw plays.

Usage:
    python src/iceberg/stats.py
"""

import argparse
from pathlib import Path

import duckdb

DEFAULT_DB = Path("data/iceberg.duckdb")

CREATE_ARTIST_STATS = """
CREATE OR REPLACE TABLE artist_stats AS
WITH
-- Per-artist listening totals from raw plays.
totals AS (
    SELECT
        artist_name,
        COUNT(*)                                     AS plays,
        ROUND(SUM(minutes_played) / 60, 2)           AS hours,
        ROUND(AVG(minutes_played), 2)                AS avg_minutes_per_play,
        ROUND(AVG(CASE WHEN skipped THEN 1 ELSE 0 END), 3) AS skip_rate,
        MIN(played_at)                               AS first_played,
        MAX(played_at)                               AS last_played
    FROM plays
    GROUP BY artist_name
),
-- Each artist's single most-played track. ROW_NUMBER ranks tracks within
-- each artist; we keep rank 1.
ranked_tracks AS (
    SELECT
        artist_name,
        track_name,
        COUNT(*) AS track_plays,
        ROW_NUMBER() OVER (PARTITION BY artist_name ORDER BY COUNT(*) DESC, track_name) AS rn
    FROM plays
    GROUP BY artist_name, track_name
),
top_tracks AS (
    SELECT artist_name, track_name AS top_track, track_plays AS top_track_plays
    FROM ranked_tracks
    WHERE rn = 1
),
-- The most recent play in the whole dataset stands in for "today", so the
-- numbers make sense on old exports and on the fake sample alike.
dataset_end AS (
    SELECT MAX(played_at) AS end_ts FROM plays
)
SELECT
    t.artist_name,
    t.plays,
    t.hours,
    t.avg_minutes_per_play,
    t.skip_rate,
    t.first_played,
    t.last_played,
    DATE_DIFF('day', t.last_played, d.end_ts)        AS days_since_last,
    DATE_DIFF('day', t.first_played, t.last_played)  AS days_active,
    tt.top_track,
    tt.top_track_plays,
    a.listeners,
    a.playcount                                      AS global_playcount,
    a.tags
FROM totals t
LEFT JOIN top_tracks tt USING (artist_name)
LEFT JOIN artists a     USING (artist_name)
CROSS JOIN dataset_end d
ORDER BY t.plays DESC
"""


def build(db: Path) -> None:
    con = duckdb.connect(str(db))
    con.execute(CREATE_ARTIST_STATS)
    (n,) = con.execute("SELECT COUNT(*) FROM artist_stats").fetchone()
    print(f"Built artist_stats with {n} artists\n")
    con.sql("""
        SELECT artist_name, plays, hours, top_track, days_since_last, listeners
        FROM artist_stats
        ORDER BY plays DESC
        LIMIT 10
    """).show()
    con.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    args = parser.parse_args()
    build(args.db)


if __name__ == "__main__":
    main()

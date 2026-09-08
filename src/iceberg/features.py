"""Compute the three analytical features that power the iceberg.

Builds three tables from `plays` and `artist_stats`:

  iceberg_tiers   one row per artist: obscurity score (0-100) and tier name
  dropped_artists artists you once played heavily and then abandoned
  taste_depth     one row per month: plays-weighted obscurity over time

Obscurity is derived from Last.fm global listener count on a log scale:
    10,000,000 listeners -> 0   (the most mainstream)
        10,000 listeners -> 100 (essentially unknown)
Log scale matters because listener counts span several orders of magnitude;
the gap between 50K and 500K is as meaningful as the gap between 500K and 5M.

Usage:
    python src/iceberg/features.py
    python src/iceberg/features.py --churn-plays 30 --churn-gap 240
"""

import argparse
from pathlib import Path

import duckdb

DEFAULT_DB = Path("data/iceberg.duckdb")

# Tier boundaries in listeners. Fixed thresholds (rather than percentiles)
# mean two people's icebergs are comparable.
TIERS = [
    ("Surface",   3_000_000),   # >= 3M listeners
    ("Shallows",  1_000_000),
    ("Twilight",    300_000),
    ("Midnight",    100_000),
    ("Abyss",             0),
]

CREATE_TIERS = """
CREATE OR REPLACE TABLE iceberg_tiers AS
SELECT
    artist_name,
    plays,
    hours,
    listeners,
    -- log10 scale: 1e7 -> 0, 1e4 -> 100, clipped to [0, 100]
    ROUND(LEAST(100, GREATEST(0,
        (7 - LOG10(GREATEST(listeners, 1))) / 3 * 100
    )), 1)                                                    AS obscurity,
    CASE
        WHEN listeners IS NULL      THEN 'Unknown'
        WHEN listeners >= 3000000   THEN 'Surface'
        WHEN listeners >= 1000000   THEN 'Shallows'
        WHEN listeners >= 300000    THEN 'Twilight'
        WHEN listeners >= 100000    THEN 'Midnight'
        ELSE                             'Abyss'
    END                                                       AS tier,
    CASE
        WHEN listeners IS NULL      THEN 99
        WHEN listeners >= 3000000   THEN 1
        WHEN listeners >= 1000000   THEN 2
        WHEN listeners >= 300000    THEN 3
        WHEN listeners >= 100000    THEN 4
        ELSE                             5
    END                                                       AS tier_rank,
    top_track,
    tags,
    first_played,
    last_played,
    days_since_last
FROM artist_stats
ORDER BY tier_rank, plays DESC
"""

# Churn: find each artist's busiest 90-day stretch. If that stretch had
# >= churn_plays plays AND the artist has been silent for >= churn_gap days,
# they were "dropped".
CREATE_DROPPED = """
CREATE OR REPLACE TABLE dropped_artists AS
WITH
monthly AS (
    SELECT artist_name, DATE_TRUNC('month', played_at) AS month, COUNT(*) AS plays
    FROM plays
    GROUP BY 1, 2
),
-- Rolling 3-month window per artist, ordered by month.
rolling AS (
    SELECT
        artist_name,
        month,
        SUM(plays) OVER (
            PARTITION BY artist_name
            ORDER BY month
            ROWS BETWEEN 2 PRECEDING AND CURRENT ROW
        ) AS plays_3mo
    FROM monthly
),
peak AS (
    SELECT artist_name, MAX(plays_3mo) AS peak_3mo_plays,
           ARG_MAX(month, plays_3mo)   AS peak_month
    FROM rolling
    GROUP BY artist_name
)
SELECT
    s.artist_name,
    s.plays        AS total_plays,
    p.peak_3mo_plays,
    p.peak_month,
    s.last_played,
    s.days_since_last,
    t.tier
FROM artist_stats s
JOIN peak p           USING (artist_name)
JOIN iceberg_tiers t  USING (artist_name)
WHERE p.peak_3mo_plays >= ?
  AND s.days_since_last >= ?
ORDER BY p.peak_3mo_plays DESC
"""

# Taste depth: for each month, the plays-weighted average obscurity of what
# you listened to. Rising line = getting more obscure over time.
CREATE_TASTE_DEPTH = """
CREATE OR REPLACE TABLE taste_depth AS
SELECT
    DATE_TRUNC('month', p.played_at)::DATE          AS month,
    COUNT(*)                                        AS plays,
    COUNT(DISTINCT p.artist_name)                   AS distinct_artists,
    ROUND(AVG(t.obscurity), 1)                      AS depth_score,
    ROUND(AVG(CASE WHEN t.tier_rank >= 3 THEN 1.0 ELSE 0.0 END) * 100, 1)
                                                    AS pct_below_surface
FROM plays p
JOIN iceberg_tiers t USING (artist_name)
WHERE t.listeners IS NOT NULL
GROUP BY 1
ORDER BY 1
"""


def build(db: Path, churn_plays: int, churn_gap: int) -> None:
    con = duckdb.connect(str(db))

    con.execute(CREATE_TIERS)
    print("Iceberg tiers:")
    con.sql("""
        SELECT tier, COUNT(*) AS artists, SUM(plays) AS plays
        FROM iceberg_tiers GROUP BY tier, tier_rank ORDER BY tier_rank
    """).show()

    con.execute(CREATE_DROPPED, [churn_plays, churn_gap])
    print(f"Dropped artists ({churn_plays}+ plays in 90 days, then {churn_gap}+ days silent):")
    con.sql("SELECT artist_name, peak_3mo_plays, peak_month::DATE AS peak_month, "
            "days_since_last, tier FROM dropped_artists").show()

    con.execute(CREATE_TASTE_DEPTH)
    print("Taste depth by month (first and last 6):")
    con.sql("""
        (SELECT * FROM taste_depth ORDER BY month LIMIT 6)
        UNION ALL
        (SELECT * FROM taste_depth ORDER BY month DESC LIMIT 6)
    """).show()
    con.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--churn-plays", type=int, default=20,
                        help="min plays in a 90-day window to count as a real phase")
    parser.add_argument("--churn-gap", type=int, default=180,
                        help="min days of silence to count as dropped")
    args = parser.parse_args()
    build(args.db, args.churn_plays, args.churn_gap)


if __name__ == "__main__":
    main()

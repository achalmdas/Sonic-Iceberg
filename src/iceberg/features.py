"""Compute the three analytical features that power the iceberg.

Builds three tables from `plays` and `artist_stats`:

  iceberg_tiers   one row per artist: obscurity score (0-100) and tier name
  dropped_artists artists you once played heavily and then abandoned
  taste_depth     one row per month: plays-weighted obscurity over time

Obscurity (0-100) puts two sources on a log scale, then blends them:
    Last.fm listeners: 10,000,000 -> 0,  10,000 -> 100   (weight 0.75)
    Deezer fans:       10,000,000 -> 0,   1,000 -> 100   (weight 0.25)
Log scale matters because counts span several orders of magnitude; the gap
between 50K and 500K is as meaningful as the gap between 500K and 5M.

Last.fm carries most of the weight: its listener counts are a broad, well
graded signal. Deezer's "fans" (favourites) are very top-heavy: huge for a
few superstars, then a cliff, so they mostly help confirm who is Surface.
When the two scores disagree by more than 45 points one of them matched
the wrong artist, and Deezer is dropped. If only one source knows the
artist, that one is used alone.

Usage:
    python src/iceberg/features.py
    python src/iceberg/features.py --churn-plays 30 --churn-gap 240
"""

import argparse
from pathlib import Path

import duckdb

DEFAULT_DB = Path("data/iceberg.duckdb")

# Tier boundaries on the blended obscurity score. Fixed thresholds (rather
# than percentiles) mean two people's icebergs are comparable.
#   Surface  < 20   roughly 3M+ Last.fm listeners
#   Shallows < 35   roughly 1M-3M
#   Twilight < 55   roughly 300K-1M
#   Midnight < 72   roughly 100K-300K
#   Abyss    >= 72  under 100K

CREATE_TIERS = """
CREATE OR REPLACE TABLE iceberg_tiers AS
WITH scored AS (
    SELECT
        s.*,
        a.deezer_fans,
        -- each source on its own log scale, clipped to [0, 100]; NULL if unknown
        CASE WHEN s.listeners IS NULL OR s.listeners < 1 THEN NULL
             ELSE LEAST(100, GREATEST(0, (7 - LOG10(s.listeners)) / 3 * 100)) END      AS lastfm_score,
        CASE WHEN a.deezer_fans IS NULL OR a.deezer_fans < 1 THEN NULL
             ELSE LEAST(100, GREATEST(0, (7 - LOG10(a.deezer_fans)) / 4 * 100)) END    AS deezer_score
    FROM artist_stats s
    LEFT JOIN artists a USING (artist_name)
),
blended AS (
    SELECT *,
        ROUND(CASE
            -- both known and roughly agree: weighted blend
            WHEN lastfm_score IS NOT NULL AND deezer_score IS NOT NULL
                 AND ABS(lastfm_score - deezer_score) <= 45
                THEN 0.75 * lastfm_score + 0.25 * deezer_score
            -- wild disagreement = one source matched the wrong artist; trust Last.fm
            WHEN lastfm_score IS NOT NULL THEN lastfm_score
            ELSE deezer_score
        END, 1) AS obscurity
    FROM scored
)
SELECT
    artist_name, plays, hours, listeners, deezer_fans,
    ROUND(lastfm_score, 1) AS lastfm_score,
    ROUND(deezer_score, 1) AS deezer_score,
    obscurity,
    CASE
        WHEN obscurity IS NULL THEN 'Unknown'
        WHEN obscurity < 20    THEN 'Surface'
        WHEN obscurity < 35    THEN 'Shallows'
        WHEN obscurity < 55    THEN 'Twilight'
        WHEN obscurity < 72    THEN 'Midnight'
        ELSE                        'Abyss'
    END AS tier,
    CASE
        WHEN obscurity IS NULL THEN 99
        WHEN obscurity < 20    THEN 1
        WHEN obscurity < 35    THEN 2
        WHEN obscurity < 55    THEN 3
        WHEN obscurity < 72    THEN 4
        ELSE                        5
    END AS tier_rank,
    top_track, top_track_id, spotify_id, image_url, tags, first_played, last_played, days_since_last
FROM blended
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

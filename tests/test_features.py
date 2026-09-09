from conftest import PLANTED_DROPPED, PLANTED_HEAVY, PLANTED_PHASE, PLANTED_STEADY


def test_churn_flags_only_planted_artists(con):
    dropped = {r[0] for r in con.execute("SELECT artist_name FROM dropped_artists").fetchall()}
    assert dropped <= PLANTED_DROPPED | PLANTED_PHASE          # no false positives
    assert PLANTED_HEAVY <= dropped                              # no misses among the obvious ones


def test_steady_artists_are_never_flagged(con):
    dropped = {r[0] for r in con.execute("SELECT artist_name FROM dropped_artists").fetchall()}
    assert not dropped & PLANTED_STEADY


def test_obscurity_is_monotonic_in_listeners(con):
    rows = con.execute("SELECT listeners, obscurity FROM iceberg_tiers ORDER BY listeners").fetchall()
    scores = [o for _, o in rows]
    assert scores == sorted(scores, reverse=True)      # fewer listeners -> higher obscurity


def test_obscurity_bounds(con):
    lo, hi = con.execute("SELECT MIN(obscurity), MAX(obscurity) FROM iceberg_tiers").fetchone()
    assert 0 <= lo <= hi <= 100


def test_tier_assignment_by_score(con):
    assert con.execute("SELECT tier FROM iceberg_tiers WHERE artist_name = 'Radiohead'").fetchone()[0] == "Surface"
    deepest = con.execute("SELECT artist_name FROM iceberg_tiers ORDER BY obscurity DESC LIMIT 1").fetchone()[0]
    assert deepest == "Cindy Lee"
    ranks = con.execute("SELECT DISTINCT tier_rank FROM iceberg_tiers").fetchall()
    assert len(ranks) >= 3                                        # the sample spans several tiers


def test_taste_depth_covers_every_month(con):
    months = con.execute("SELECT COUNT(*) FROM taste_depth").fetchone()[0]
    span = con.execute("SELECT DATE_DIFF('month', MIN(played_at), MAX(played_at)) + 1 FROM plays").fetchone()[0]
    assert months == span


def test_stats_top_track_is_the_most_played(con):
    artist, top = con.execute("SELECT artist_name, top_track FROM artist_stats ORDER BY plays DESC LIMIT 1").fetchone()
    best = con.execute("""
        SELECT track_name FROM plays WHERE artist_name = ?
        GROUP BY track_name ORDER BY COUNT(*) DESC, track_name LIMIT 1
    """, [artist]).fetchone()[0]
    assert top == best


def test_unknown_artists_are_not_scored(db, tmp_path):
    """An artist neither source knows must land in 'Unknown', not 'Abyss'.
    Works on a copy so the shared session database stays untouched."""
    import shutil
    import duckdb
    from iceberg import features, stats
    db = shutil.copy(db, tmp_path / "copy.duckdb")
    con = duckdb.connect(str(db))
    con.execute("INSERT INTO plays SELECT * REPLACE ('Nobody Knows Me' AS artist_name) FROM plays LIMIT 30")
    con.close()
    stats.build(db)
    features.build(db, churn_plays=20, churn_gap=180)
    con = duckdb.connect(str(db), read_only=True)
    tier, obs = con.execute("SELECT tier, obscurity FROM iceberg_tiers WHERE artist_name = 'Nobody Knows Me'").fetchone()
    con.close()
    assert tier == "Unknown" and obs is None

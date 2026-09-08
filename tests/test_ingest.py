def test_loads_all_music_plays(con):
    # 8000 generated + 1 podcast (dropped) + 1 zero-length (dropped)
    assert con.execute("SELECT COUNT(*) FROM plays").fetchone()[0] == 8000


def test_podcasts_and_empty_rows_are_filtered(con):
    assert con.execute("SELECT COUNT(*) FROM plays WHERE track_uri IS NULL").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM plays WHERE ms_played <= 0").fetchone()[0] == 0


def test_clean_schema(con):
    cols = {r[0] for r in con.execute("DESCRIBE plays").fetchall()}
    assert {"played_at", "artist_name", "track_name", "track_id", "minutes_played", "skipped"} <= cols


def test_track_id_is_extracted_from_uri(con):
    uri, tid = con.execute("SELECT track_uri, track_id FROM plays LIMIT 1").fetchone()
    assert uri == f"spotify:track:{tid}"
    assert len(tid) == 22


def test_ingest_is_idempotent(db, export_dir):
    from iceberg import ingest
    first = ingest.ingest(export_dir, db)
    second = ingest.ingest(export_dir, db)
    assert first == second == 8000

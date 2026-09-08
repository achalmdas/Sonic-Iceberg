"""Shared fixtures: a small generated export loaded into a temporary DuckDB.

No network access. The artists table is filled with fixed listener counts so
tier and churn tests are deterministic.
"""

import sys
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from iceberg import enrich, features, generate_sample, ingest, stats  # noqa: E402

# Last.fm / Deezer numbers frozen at a point in time so tests don't drift.
LISTENERS = {
    "Kendrick Lamar": 5_259_412, "Frank Ocean": 4_406_421, "Radiohead": 8_427_800,
    "SZA": 3_629_513, "Tame Impala": 4_621_921, "The Weeknd": 5_507_208, "Drake": 6_904_837,
    "Mac Miller": 3_702_026, "Arctic Monkeys": 7_285_715, "Steve Lacy": 3_309_452,
    "Yves Tumor": 1_082_128, "Mk.gee": 744_800, "Duster": 1_672_514, "Playboi Carti": 2_581_448,
    "Yeat": 1_755_978, "Slowdive": 2_342_694, "Alex G": 2_175_307, "Cindy Lee": 154_972,
    "Panchiko": 1_305_323, "Have a Nice Life": 1_120_385, "Jai Paul": 620_193, "Dijon": 857_763,
    "Ethel Cain": 1_357_214, "black midi": 512_923, "Mount Kimbie": 830_627,
}

# What generate_sample.py plants, by pattern (see ARTISTS there).
# "Heavy" ones have enough weight to clear the 20-plays-in-90-days bar at 8k plays;
# light ones (weight <= 2) may or may not, depending on the random draw.
PLANTED_DROPPED = {"The Weeknd", "Drake", "Mac Miller", "Arctic Monkeys", "black midi"}
PLANTED_PHASE = {"Playboi Carti", "Yeat", "Have a Nice Life", "Ethel Cain"}
PLANTED_HEAVY = {"The Weeknd", "Drake", "Mac Miller", "Arctic Monkeys", "Playboi Carti", "Yeat"}
PLANTED_STEADY = {"Kendrick Lamar", "Frank Ocean", "Radiohead", "SZA", "Tame Impala",
                  "Slowdive", "Alex G", "Panchiko", "Jai Paul", "Mount Kimbie"}


@pytest.fixture(scope="session")
def export_dir(tmp_path_factory) -> Path:
    """A generated export with a podcast row and a zero-length row mixed in."""
    d = tmp_path_factory.mktemp("export")
    events = generate_sample.generate(n_plays=8000, seed=42)
    events.append({**events[0], "spotify_track_uri": None, "spotify_episode_uri": "spotify:episode:x",
                   "episode_name": "Some Podcast", "master_metadata_track_name": None})
    events.append({**events[1], "ms_played": 0})
    import json
    (d / "Streaming_History_Audio_2023-2026_0.json").write_text(json.dumps(events))
    return d


@pytest.fixture(scope="session")
def db(tmp_path_factory, export_dir) -> Path:
    """Full pipeline up to features, with a stubbed artists table."""
    path = tmp_path_factory.mktemp("db") / "test.duckdb"
    ingest.ingest(export_dir, path)
    con = duckdb.connect(str(path))
    con.execute(enrich.CREATE_ARTISTS)
    con.executemany("INSERT INTO artists VALUES (?, ?, ?, ?, ?, ?, NULL)",
                    [(n, n, l, l * 20, ["tag"], int(l * 0.8)) for n, l in LISTENERS.items()])
    con.close()
    stats.build(path)
    features.build(path, churn_plays=20, churn_gap=180)
    return path


@pytest.fixture
def con(db):
    c = duckdb.connect(str(db), read_only=True)
    yield c
    c.close()

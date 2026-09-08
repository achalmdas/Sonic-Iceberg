"""Enrich artists in the plays table with popularity from two sources.

  Last.fm `artist.getInfo`  -> listeners, playcount, community tags (genres)
  Deezer  `search/artist`   -> fan count (no API key needed)

Two independent sources are used because each platform has its own
audience skew (Last.fm leans rock and older listeners; Deezer leans
European). features.py averages their log-scaled counts so one platform's
blind spot doesn't decide an artist's tier.

Why not Spotify? In February 2026 Spotify removed the `popularity` and
`followers` fields from artist objects for development-mode apps, so there
is no longer a mainstream score available from their API.

Results are written to an `artists` table in DuckDB, which doubles as a
cache: artists already present are skipped on re-runs.

Requires LASTFM_API_KEY in a .env file (free at last.fm/api/account/create).

Usage:
    python src/iceberg/enrich.py
    python src/iceberg/enrich.py --refresh      # re-fetch everything (needed once after adding Deezer)
"""

import argparse
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import requests
from dotenv import load_dotenv

DEFAULT_DB = Path("data/iceberg.duckdb")
LASTFM_URL = "https://ws.audioscrobbler.com/2.0/"
DEEZER_URL = "https://api.deezer.com/search/artist"
REQUEST_DELAY = 0.25  # both APIs are happy at ~4 requests/second

CREATE_ARTISTS = """
CREATE TABLE IF NOT EXISTS artists (
    artist_name     VARCHAR PRIMARY KEY,   -- matches plays.artist_name
    lastfm_name     VARCHAR,               -- Last.fm's canonical spelling
    listeners       BIGINT,                -- Last.fm unique listeners
    playcount       BIGINT,                -- Last.fm total plays
    tags            VARCHAR[],             -- Last.fm community tags (genres)
    deezer_fans     BIGINT,                -- Deezer fan count
    fetched_at      TIMESTAMP
)
"""


def get_api_key() -> str:
    load_dotenv()
    key = os.getenv("LASTFM_API_KEY")
    if not key:
        raise SystemExit("Missing LASTFM_API_KEY in .env")
    return key


def fetch_artist(name: str, api_key: str) -> dict | None:
    """Return {lastfm_name, listeners, playcount, tags} or None if not found."""
    params = {
        "method": "artist.getInfo",
        "artist": name,
        "api_key": api_key,
        "format": "json",
        "autocorrect": 1,   # fixes casing/spelling, e.g. 'alex g' -> 'Alex G'
    }
    try:
        resp = requests.get(LASTFM_URL, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        print(f"  request failed for {name!r}: {exc}")
        return None

    if "error" in data:            # Last.fm returns 200 with an error body for unknown artists
        return None

    artist = data["artist"]
    return {
        "lastfm_name": artist["name"],
        "listeners": int(artist["stats"]["listeners"]),
        "playcount": int(artist["stats"]["playcount"]),
        "tags": [t["name"] for t in artist.get("tags", {}).get("tag", [])],
    }


def fetch_deezer_fans(name: str) -> int | None:
    """Top Deezer search hit's fan count, or None. Case-insensitive exact name match preferred."""
    try:
        resp = requests.get(DEEZER_URL, params={"q": name, "limit": 5}, timeout=10)
        resp.raise_for_status()
        hits = resp.json().get("data", [])
    except (requests.RequestException, ValueError) as exc:
        print(f"  deezer failed for {name!r}: {exc}")
        return None
    if not hits:
        return None
    exact = [h for h in hits if h.get("name", "").lower() == name.lower()]
    return int((exact or hits)[0].get("nb_fan", 0)) or None


def enrich(db: Path, refresh: bool = False, progress=None) -> None:
    """`progress(done, total)` is called after each artist, if given."""
    con = duckdb.connect(str(db))
    con.execute(CREATE_ARTISTS)
    if refresh:
        con.execute("DELETE FROM artists")

    # Artists present in plays but not yet in the artists table.
    todo = [row[0] for row in con.execute("""
        SELECT DISTINCT p.artist_name
        FROM plays p
        LEFT JOIN artists a USING (artist_name)
        WHERE a.artist_name IS NULL
        ORDER BY p.artist_name
    """).fetchall()]

    if not todo:
        print("All artists already enriched. Use --refresh to re-fetch.")
        return

    api_key = get_api_key()
    print(f"Enriching {len(todo)} artists via Last.fm...")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    rows, misses = [], []

    for i, name in enumerate(todo, 1):
        info = fetch_artist(name, api_key)
        time.sleep(REQUEST_DELAY)
        fans = fetch_deezer_fans(name)
        time.sleep(REQUEST_DELAY)
        if info:
            rows.append((name, info["lastfm_name"], info["listeners"],
                         info["playcount"], info["tags"], fans, now))
        else:
            misses.append(name)
            rows.append((name, None, None, None, [], fans, now))  # cache the miss too
        if i % 25 == 0:
            print(f"  {i}/{len(todo)}")
        if progress:
            progress(i, len(todo))

    con.executemany("INSERT INTO artists VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    print(f"Done. {len(rows) - len(misses)} enriched, {len(misses)} not found.")
    if misses:
        print("  Not found:", ", ".join(misses[:10]), "..." if len(misses) > 10 else "")

    print("\nMost to least mainstream (Last.fm listeners / Deezer fans):")
    con.sql("""
        SELECT artist_name, listeners, deezer_fans, tags[1:3] AS top_tags
        FROM artists WHERE listeners IS NOT NULL
        ORDER BY listeners DESC
    """).show(max_rows=30)
    con.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    enrich(args.db, args.refresh)


if __name__ == "__main__":
    main()

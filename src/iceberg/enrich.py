"""Enrich artists in the plays table with global listener counts and tags.

Uses the Last.fm `artist.getInfo` endpoint, which returns how many people
worldwide have listened to an artist, total global plays, and community
tags (effectively genres). Global listener count is the project's
"mainstream vs obscure" axis.

Why not Spotify? In February 2026 Spotify removed the `popularity` and
`followers` fields from artist objects for development-mode apps, so there
is no longer a mainstream score available from their API.

Results are written to an `artists` table in DuckDB, which doubles as a
cache: artists already present are skipped on re-runs.

Requires LASTFM_API_KEY in a .env file (free at last.fm/api/account/create).

Usage:
    python src/iceberg/enrich.py
    python src/iceberg/enrich.py --refresh      # re-fetch everything
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
REQUEST_DELAY = 0.25  # Last.fm asks for <= ~5 requests/second

CREATE_ARTISTS = """
CREATE TABLE IF NOT EXISTS artists (
    artist_name     VARCHAR PRIMARY KEY,   -- matches plays.artist_name
    lastfm_name     VARCHAR,               -- Last.fm's canonical spelling
    listeners       BIGINT,                -- global unique listeners
    playcount       BIGINT,                -- global total plays
    tags            VARCHAR[],             -- top community tags (genres)
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


def enrich(db: Path, refresh: bool = False) -> None:
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
        if info:
            rows.append((name, info["lastfm_name"], info["listeners"],
                         info["playcount"], info["tags"], now))
        else:
            misses.append(name)
            rows.append((name, None, None, None, [], now))  # cache the miss too
        if i % 25 == 0:
            print(f"  {i}/{len(todo)}")
        time.sleep(REQUEST_DELAY)

    con.executemany("INSERT INTO artists VALUES (?, ?, ?, ?, ?, ?)", rows)
    print(f"Done. {len(rows) - len(misses)} enriched, {len(misses)} not found.")
    if misses:
        print("  Not found:", ", ".join(misses[:10]), "..." if len(misses) > 10 else "")

    print("\nMost to least mainstream:")
    con.sql("""
        SELECT artist_name, listeners, tags[1:3] AS top_tags
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

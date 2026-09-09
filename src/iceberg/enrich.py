"""Enrich artists in the plays table with popularity from two sources.

  Last.fm `artist.getInfo`  -> listeners, playcount, community tags (genres)
  Deezer  `search/artist`   -> fan count (no API key needed)
  Deezer  `search/artist`   -> also artist photo (no quota, so photos are always available)
  Spotify `search`          -> artist page link (optional; small daily quota, best-effort)

Spotify no longer exposes popularity or follower counts to development-mode
apps (Feb 2026), so it's used only for a link, never for ranking.

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
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import requests
from dotenv import load_dotenv

try:
    import spotipy
    from spotipy.oauth2 import SpotifyClientCredentials
except ImportError:          # spotipy is optional
    spotipy = None

DEFAULT_DB = Path("data/iceberg.duckdb")
LASTFM_URL = "https://ws.audioscrobbler.com/2.0/"
DEEZER_URL = "https://api.deezer.com/search/artist"
REQUEST_DELAY = 0.25  # per worker; with WORKERS=4 that's ~4 requests/second per source
WORKERS = 4
SPOTIFY_MAX_ARTISTS = 200   # dev-mode quota is small; photos only for your most-played artists
_spotify_disabled = False   # flipped on the first quota error so we stop hammering the API

CREATE_ARTISTS = """
CREATE TABLE IF NOT EXISTS artists (
    artist_name     VARCHAR PRIMARY KEY,   -- matches plays.artist_name
    lastfm_name     VARCHAR,               -- Last.fm's canonical spelling
    listeners       BIGINT,                -- Last.fm unique listeners
    playcount       BIGINT,                -- Last.fm total plays
    tags            VARCHAR[],             -- Last.fm community tags (genres)
    deezer_fans     BIGINT,                -- Deezer fan count
    spotify_id      VARCHAR,               -- for open.spotify.com links
    image_url       VARCHAR,               -- artist photo (Spotify CDN)
    fetched_at      TIMESTAMP
)
"""


def get_api_key() -> str:
    load_dotenv()
    key = os.getenv("LASTFM_API_KEY")
    if not key:
        raise SystemExit("Missing LASTFM_API_KEY in .env")
    return key


import re
import unicodedata


def normalize(name: str) -> str:
    """'Travi$ Scott' -> 'travis scott', for comparing names across sources."""
    name = name.replace("$", "s").replace("€", "e")
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9 ]", "", name.lower()).strip()


def _lastfm(api_key: str, **params) -> dict | None:
    params.update(api_key=api_key, format="json")
    try:
        resp = requests.get(LASTFM_URL, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        print(f"  lastfm {params.get('method')} failed for {params.get('artist')!r}: {exc}")
        return None
    return None if "error" in data else data   # Last.fm returns 200 with an error body for unknowns


def _info(name: str, api_key: str, autocorrect: int) -> dict | None:
    data = _lastfm(api_key, method="artist.getInfo", artist=name, autocorrect=autocorrect)
    if not data:
        return None
    a = data["artist"]
    return {
        "lastfm_name": a["name"],
        "listeners": int(a["stats"]["listeners"]),
        "playcount": int(a["stats"]["playcount"]),
        "tags": [t["name"] for t in a.get("tags", {}).get("tag", [])],
    }


def fetch_artist(name: str, api_key: str) -> dict | None:
    """Return {lastfm_name, listeners, playcount, tags} or None if not found.

    Last.fm's autocorrect is crowd-sourced and sometimes redirects a name to an
    old alias page (e.g. 'Travis Scott' -> 'Travi$ Scott', 188K listeners, while
    the real page has 3.7M). So we also search, keep exact-name matches, and if
    the biggest of those is clearly larger than what autocorrect gave us, use it:
    when a name matches several pages, the famous one is the one people meant.
    """
    info = _info(name, api_key, autocorrect=1)
    time.sleep(REQUEST_DELAY)

    search = _lastfm(api_key, method="artist.search", artist=name, limit=5)
    hits = (search or {}).get("results", {}).get("artistmatches", {}).get("artist", [])
    exact = [h for h in hits if normalize(h.get("name", "")) == normalize(name)]
    if exact:
        best = max(exact, key=lambda h: int(h.get("listeners", 0) or 0))
        best_n = int(best.get("listeners", 0) or 0)
        if best_n > 2 * (info["listeners"] if info else 0):
            time.sleep(REQUEST_DELAY)
            better = _info(best["name"], api_key, autocorrect=0)
            if better and better["listeners"] >= best_n * 0.5:
                if info:
                    print(f"  {name!r}: autocorrect gave {info['lastfm_name']!r} ({info['listeners']:,}); "
                          f"using {better['lastfm_name']!r} ({better['listeners']:,})")
                info = better
    return info


def fetch_deezer(name: str) -> tuple[int | None, str | None, str | None]:
    """(fans, picture_url, profile_url) from Deezer search, or Nones.
    Deezer needs no API key and has no daily quota, so it's the reliable
    source for artist photos; Spotify is only used for its link."""
    try:
        resp = requests.get(DEEZER_URL, params={"q": name, "limit": 10}, timeout=10)
        resp.raise_for_status()
        hits = resp.json().get("data", [])
    except (requests.RequestException, ValueError) as exc:
        print(f"  deezer failed for {name!r}: {exc}")
        return None, None, None
    if not hits:
        return None, None, None
    # Several artists can share a name (there are many "Drake"s on Deezer).
    # Prefer exact-name matches, then take the biggest of them: the famous
    # one is the one people who typed this name almost certainly meant.
    exact = [h for h in hits if normalize(h.get("name", "")) == normalize(name)]
    best = max(exact or hits, key=lambda h: int(h.get("nb_fan", 0) or 0))
    picture = best.get("picture_medium") or best.get("picture") or None
    # Deezer serves a placeholder for artists with no photo; treat that as none.
    if picture and "/artist//" in picture:
        picture = None
    return int(best.get("nb_fan", 0)) or None, picture, best.get("link") or None


def make_spotify():
    """A Spotify client if credentials are present and spotipy is installed, else None."""
    load_dotenv()
    cid, secret = os.getenv("SPOTIFY_CLIENT_ID"), os.getenv("SPOTIFY_CLIENT_SECRET")
    if not (spotipy and cid and secret):
        return None
    try:
        # retries=0: on a 429 spotipy would otherwise sleep for the whole Retry-After
        # window, which for a daily quota is ~23 hours. Fail fast instead.
        return spotipy.Spotify(auth_manager=SpotifyClientCredentials(cid, secret),
                               retries=0, status_retries=0, requests_timeout=10)
    except Exception as exc:  # noqa: BLE001
        print(f"  Spotify unavailable ({exc}); continuing without photos")
        return None


def fetch_spotify(sp, name: str) -> tuple[str | None, str | None]:
    """(spotify_id, image_url) for the best-matching artist, or (None, None)."""
    global _spotify_disabled
    if sp is None or _spotify_disabled:
        return None, None
    try:
        items = sp.search(q=f'artist:"{name}"', type="artist", limit=5)["artists"]["items"]
    except Exception as exc:  # noqa: BLE001 — any Spotify failure just means no photo
        if getattr(exc, "http_status", None) == 429 or "rate" in str(exc).lower():
            if not _spotify_disabled:
                print("  Spotify quota reached; skipping photos for the rest of this run "
                      "(re-run with --photos tomorrow to backfill)")
            _spotify_disabled = True
        else:
            print(f"  spotify failed for {name!r}: {exc}")
        return None, None
    if not items:
        return None, None
    exact = [a for a in items if a["name"].lower() == name.lower()]
    a = (exact or items)[0]
    images = a.get("images") or []
    # prefer a mid-size image (~300px); fall back to whatever exists
    mid = sorted(images, key=lambda im: abs((im.get("width") or 0) - 320))
    return a["id"], (mid[0]["url"] if mid else None)


def lookup(name: str, api_key: str, sp, want_photo: bool = True) -> tuple:
    """All three sources for one artist. Runs in a worker thread."""
    info = fetch_artist(name, api_key)
    time.sleep(REQUEST_DELAY)
    fans, deezer_pic, _ = fetch_deezer(name)
    time.sleep(REQUEST_DELAY)
    sp_id, sp_image = fetch_spotify(sp, name) if want_photo else (None, None)
    image = sp_image or deezer_pic   # Spotify's photo if we got one, else Deezer's
    return name, info, fans, sp_id, image


def enrich(db: Path, refresh: bool = False, progress=None, min_plays: int = 1) -> None:
    """Look up every artist in `plays` with at least `min_plays` plays that isn't
    cached yet. `progress(done, total)` is called after each artist, if given."""
    con = duckdb.connect(str(db))
    con.execute(CREATE_ARTISTS)
    if refresh:
        con.execute("DELETE FROM artists")

    # Artists present in plays but not yet in the artists table.
    todo = [row[0] for row in con.execute("""
        SELECT p.artist_name
        FROM plays p
        LEFT JOIN artists a USING (artist_name)
        WHERE a.artist_name IS NULL
        GROUP BY p.artist_name
        HAVING COUNT(*) >= ?
        ORDER BY COUNT(*) DESC
    """, [min_plays]).fetchall()]

    if not todo:
        print("All artists already enriched. Use --refresh to re-fetch.")
        return

    api_key = get_api_key()
    sp = make_spotify()
    print(f"Enriching {len(todo)} artists via Last.fm + Deezer" + (" + Spotify..." if sp else "..."))
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    rows, misses = [], []

    # Concurrent lookups; DuckDB writes stay on this thread and happen in
    # batches, so an interrupted run keeps everything fetched so far.
    # `todo` is ordered by plays, so the first SPOTIFY_MAX_ARTISTS get photos.
    pending = []

    def flush():
        if pending:
            con.executemany("INSERT INTO artists VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", pending)
            rows.extend(pending)
            pending.clear()

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [pool.submit(lookup, name, api_key, sp, idx < SPOTIFY_MAX_ARTISTS)
                   for idx, name in enumerate(todo)]
        for i, fut in enumerate(as_completed(futures), 1):
            name, info, fans, sp_id, image = fut.result()
            if info:
                pending.append((name, info["lastfm_name"], info["listeners"],
                                info["playcount"], info["tags"], fans, sp_id, image, now))
            else:
                misses.append(name)
                pending.append((name, None, None, None, [], fans, sp_id, image, now))  # cache the miss too
            if i % 25 == 0:
                print(f"  {i}/{len(todo)}")
                flush()
            if progress:
                progress(i, len(todo))
    flush()

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


def backfill_photos(db: Path, limit: int = SPOTIFY_MAX_ARTISTS) -> None:
    """Fetch Spotify photos/links for the most-played artists that don't have one yet."""
    sp = make_spotify()
    if sp is None:
        raise SystemExit("Spotify credentials missing from .env")
    con = duckdb.connect(str(db))
    todo = [r[0] for r in con.execute("""
        SELECT a.artist_name FROM artists a
        JOIN (SELECT artist_name, COUNT(*) AS n FROM plays GROUP BY 1) p USING (artist_name)
        WHERE a.spotify_id IS NULL AND a.listeners IS NOT NULL
        ORDER BY p.n DESC LIMIT ?
    """, [limit]).fetchall()]
    print(f"Backfilling photos for {len(todo)} artists...")
    done = 0
    for name in todo:
        sp_id, image = fetch_spotify(sp, name)
        if _spotify_disabled:
            break
        if sp_id:
            con.execute("UPDATE artists SET spotify_id = ?, image_url = COALESCE(image_url, ?) WHERE artist_name = ?",
                        [sp_id, image, name])
            done += 1
        time.sleep(REQUEST_DELAY)
    con.close()
    print(f"Added {done} photos.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--photos", action="store_true", help="only backfill missing Spotify photos/links")
    parser.add_argument("--min-plays", type=int, default=3,
                        help="skip artists with fewer plays than this (they'd be unreadable on the iceberg anyway)")
    args = parser.parse_args()
    if args.photos:
        backfill_photos(args.db)
        return
    enrich(args.db, args.refresh, min_plays=args.min_plays)


if __name__ == "__main__":
    main()

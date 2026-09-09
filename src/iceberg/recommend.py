"""Suggest obscure artists in your top genres that you haven't played.

How it works:
  1. Find your top genres: Last.fm tags weighted by hours listened.
  2. For each genre, take your deepest (most obscure) artists carrying that tag.
  3. Ask Last.fm for artists similar to each of them (`artist.getSimilar`).
  4. Drop anything already in your history, look up each candidate's
     listener count (cached in `candidate_cache`), keep the obscure ones,
     and rank by how many of your artists pointed at them.

Writes a `recommendations` table: genre, artist, listeners, similar_to.

Usage:
    python src/iceberg/recommend.py
    python src/iceberg/recommend.py --genres 3 --per-genre 5 --max-listeners 1500000
"""

import argparse
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import requests
from dotenv import load_dotenv

DEFAULT_DB = Path("data/iceberg.duckdb")
LASTFM_URL = "https://ws.audioscrobbler.com/2.0/"
REQUEST_DELAY = 0.25
SEEDS_PER_GENRE = 4       # how many of your own artists to branch out from
SIMILAR_PER_SEED = 20
MAX_LOOKUPS_PER_GENRE = 30

# Tags that are too broad to be useful as "genres"
IGNORE_TAGS = {"seen live", "favorites", "favourites", "all", "music", "spotify",
               "under 2000 listeners", "american", "british", "female vocalists",
               "male vocalists", "00s", "10s", "90s", "80s", "70s", "60s", "2020s"}

CREATE_CACHE = """
CREATE TABLE IF NOT EXISTS candidate_cache (
    artist_name VARCHAR PRIMARY KEY,   -- name as Last.fm suggested it
    canonical   VARCHAR,               -- name after autocorrect
    listeners   BIGINT,
    tags        VARCHAR[],
    fetched_at  TIMESTAMP
)
"""

CREATE_RECS = """
CREATE OR REPLACE TABLE recommendations (
    genre        VARCHAR,
    artist_name  VARCHAR,
    listeners    BIGINT,
    similar_to   VARCHAR[],
    score        DOUBLE,
    rank         INTEGER
)
"""


def get_api_key() -> str:
    load_dotenv()
    key = os.getenv("LASTFM_API_KEY")
    if not key:
        raise SystemExit("Missing LASTFM_API_KEY in .env")
    return key


def lastfm(method: str, api_key: str, **params) -> dict | None:
    params.update(method=method, api_key=api_key, format="json", autocorrect=1)
    try:
        resp = requests.get(LASTFM_URL, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        print(f"  {method} failed: {exc}")
        return None
    finally:
        time.sleep(REQUEST_DELAY)
    return None if "error" in data else data


def top_genres(con, n: int) -> list[str]:
    """Tags weighted by hours listened, skipping junk tags."""
    # Last.fm tags are user-typed: "Hip-Hop", "hip hop" and "hip-hop" are one genre.
    rows = con.execute("""
        SELECT REPLACE(LOWER(tag), '-', ' ') AS tag, SUM(hours) AS weighted_hours
        FROM (SELECT UNNEST(tags[1:5]) AS tag, hours FROM iceberg_tiers WHERE tags IS NOT NULL)
        GROUP BY 1 ORDER BY weighted_hours DESC LIMIT 40
    """).fetchall()
    out = []
    for tag, _ in rows:
        if tag.lower() not in IGNORE_TAGS:
            out.append(tag)
        if len(out) == n:
            break
    return out


def seeds_for(con, genre: str, n: int) -> list[str]:
    """Your most obscure artists that carry this tag."""
    return [r[0] for r in con.execute("""
        SELECT artist_name FROM iceberg_tiers
        WHERE list_contains(list_transform(tags, t -> REPLACE(LOWER(t), '-', ' ')), ?)
          AND obscurity IS NOT NULL
        ORDER BY obscurity DESC, hours DESC LIMIT ?
    """, [genre, n]).fetchall()]


import re
import unicodedata


def normalize(name: str) -> str:
    """'Travi$ Scott' -> 'travis scott'. Used to spot stylized aliases."""
    name = name.replace("$", "s").replace("€", "e")
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9 ]", "", name.lower()).strip()


def alias_of_bigger_artist(name: str, listeners: int, api_key: str) -> bool:
    """If the plain-spelled version of this name is a much bigger artist on Last.fm,
    this entry is a misspelling/stylization of that artist, not a separate act."""
    plain = normalize(name)
    if plain == name.lower():
        return False                     # nothing stylized about the name
    data = lastfm("artist.search", api_key, artist=plain, limit=1)
    hits = (data or {}).get("results", {}).get("artistmatches", {}).get("artist", [])
    if not hits:
        return False
    top = hits[0]
    return normalize(top["name"]) == plain and int(top.get("listeners", 0)) > 3 * max(listeners, 1)


def is_collab(name: str) -> bool:
    """'Drake & Future', 'A, B' — credits, not artists."""
    return " & " in name or ", " in name or " feat" in name.lower() or " x " in name.lower()


def candidate_info(con, name: str, api_key: str) -> tuple[str, int | None, list[str]]:
    """Return (canonical_name, listeners, tags). Last.fm autocorrect fixes aliases
    like 'Travi$ Scott' -> 'Travis Scott', so the canonical name is what we trust."""
    row = con.execute("SELECT canonical, listeners, tags FROM candidate_cache WHERE artist_name = ?", [name]).fetchone()
    if row:
        return row[0], row[1], row[2] or []
    data = lastfm("artist.getInfo", api_key, artist=name)
    canonical, listeners, tags = name, None, []
    if data:
        a = data["artist"]
        canonical = a["name"]
        listeners = int(a["stats"]["listeners"])
        tags = [t["name"] for t in a.get("tags", {}).get("tag", [])]
    con.execute("INSERT INTO candidate_cache VALUES (?, ?, ?, ?, ?)",
                [name, canonical, listeners, tags, datetime.now(timezone.utc).replace(tzinfo=None)])
    return canonical, listeners, tags


def recommend(db: Path, n_genres: int, per_genre: int, max_listeners: int) -> None:
    con = duckdb.connect(str(db))
    con.execute(CREATE_CACHE)
    con.execute(CREATE_RECS)
    api_key = get_api_key()

    known = {r[0].lower() for r in con.execute("SELECT artist_name FROM plays").fetchall()}
    genres = top_genres(con, n_genres)
    print(f"Top genres: {', '.join(genres)}")

    # Collect every genre's ranked list first, then dedupe across genres so an
    # artist only appears under the genre where it scored highest.
    per_genre_results: dict[str, list[tuple[str, int, list[str], float]]] = {}
    for genre in genres:
        seeds = seeds_for(con, genre, SEEDS_PER_GENRE)
        print(f"{genre}: branching from {', '.join(seeds)}")

        # candidate -> {seed: match_score}
        votes: dict[str, dict[str, float]] = defaultdict(dict)
        for seed in seeds:
            data = lastfm("artist.getSimilar", api_key, artist=seed, limit=SIMILAR_PER_SEED)
            for sim in (data or {}).get("similarartists", {}).get("artist", []):
                name = sim["name"]
                if name.lower() not in known and not is_collab(name):
                    votes[name][seed] = float(sim.get("match", 0))

        # Look up the most-voted candidates first; cap lookups so a big library stays cheap.
        ordered = sorted(votes.items(), key=lambda kv: (-len(kv[1]), -sum(kv[1].values())))
        results, seen = [], set()
        for name, by_seed in ordered[:MAX_LOOKUPS_PER_GENRE]:
            canonical, listeners, _ = candidate_info(con, name, api_key)
            # alias of something you already play, an alias we've already kept, or out of range
            if canonical.lower() in known or canonical.lower() in seen or is_collab(canonical):
                continue
            if listeners is None or listeners > max_listeners or listeners < 2000:
                continue
            if alias_of_bigger_artist(canonical, listeners, api_key):
                continue
            seen.add(canonical.lower())
            # more of your artists pointing here + stronger matches + more obscure = higher
            score = len(by_seed) + sum(by_seed.values()) + 0.5 * (1 - listeners / max_listeners)
            results.append((canonical, listeners, list(by_seed), round(score, 3)))
        results.sort(key=lambda r: -r[3])
        per_genre_results[genre] = results

    # Cross-genre dedupe: each artist goes to the genre where it scored highest.
    best_genre: dict[str, tuple[str, float]] = {}
    for genre, results in per_genre_results.items():
        for name, _, _, score in results:
            if name not in best_genre or score > best_genre[name][1]:
                best_genre[name] = (genre, score)

    for genre, results in per_genre_results.items():
        kept = [r for r in results if best_genre[r[0]][0] == genre][:per_genre]
        print(f"\n{genre}:")
        for rank, (name, listeners, similar_to, score) in enumerate(kept, 1):
            con.execute("INSERT INTO recommendations VALUES (?, ?, ?, ?, ?, ?)",
                        [genre, name, listeners, similar_to, score, rank])
            print(f"  {rank}. {name:<28} {listeners:>9,} listeners   (like {', '.join(similar_to[:2])})")

    con.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--genres", type=int, default=3)
    parser.add_argument("--per-genre", type=int, default=5)
    parser.add_argument("--max-listeners", type=int, default=750_000,
                        help="ignore candidates more popular than this on Last.fm")
    args = parser.parse_args()
    recommend(args.db, args.genres, args.per_genre, args.max_listeners)


if __name__ == "__main__":
    main()

"""Generate a fake Spotify Extended Streaming History export.

Spotify's real export is a folder of files named like
    Streaming_History_Audio_2023-2024_0.json
Each file is a JSON list of play events. This script produces one such file
with realistic listening patterns so the rest of the pipeline can be built
and tested before the real export arrives.

Usage:
    python -m iceberg.generate_sample            # writes to data/samples/
    python -m iceberg.generate_sample --plays 20000 --seed 7
"""

import argparse
import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Fake artist catalogue.
# Each artist has a listening "pattern" so later features have something to find:
#   steady   - listened to the whole time
#   dropped  - heavy early on, then disappears (feeds churn detection)
#   new      - only appears in the last year
#   phase    - a short intense burst in the middle, then gone
# ---------------------------------------------------------------------------
ARTISTS = [
    # name,               weight, pattern
    ("Kendrick Lamar",      10, "steady"),
    ("Frank Ocean",          9, "steady"),
    ("Radiohead",            8, "steady"),
    ("SZA",                  7, "steady"),
    ("Tame Impala",          6, "steady"),
    ("The Weeknd",           9, "dropped"),
    ("Drake",                8, "dropped"),
    ("Mac Miller",           5, "dropped"),
    ("Arctic Monkeys",       6, "dropped"),
    ("Steve Lacy",           5, "new"),
    ("Yves Tumor",           3, "new"),
    ("Mk.gee",               4, "new"),
    ("Duster",               3, "new"),
    ("Playboi Carti",        7, "phase"),
    ("Yeat",                 5, "phase"),
    ("Slowdive",             3, "steady"),
    ("Alex G",               4, "steady"),
    ("Cindy Lee",            2, "new"),
    ("Panchiko",             2, "steady"),
    ("Have a Nice Life",     1, "phase"),
    ("Jai Paul",             2, "steady"),
    ("Dijon",                3, "new"),
    ("Ethel Cain",           2, "phase"),
    ("black midi",           2, "dropped"),
    ("Mount Kimbie",         1, "steady"),
]

TRACKS_PER_ARTIST = 8
PLATFORMS = ["ios", "android", "osx", "windows", "web_player"]
REASON_START = ["trackdone", "clickrow", "fwdbtn", "playbtn", "appload"]
REASON_END = ["trackdone", "fwdbtn", "endplay", "logout"]

START = datetime(2023, 1, 1, tzinfo=timezone.utc)
END = datetime(2026, 8, 31, tzinfo=timezone.utc)


def _fake_track_uri(rng: random.Random) -> str:
    """Spotify track URIs look like spotify:track:<22 base62 chars>."""
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    return "spotify:track:" + "".join(rng.choice(alphabet) for _ in range(22))


def _active_weight(pattern: str, when: datetime) -> float:
    """How likely an artist is to be played at a given moment, 0..1.

    This is what creates the listening-history shapes. `frac` is how far
    through the whole time range `when` sits (0 = start, 1 = end).
    """
    frac = (when - START) / (END - START)
    if pattern == "steady":
        return 1.0
    if pattern == "dropped":
        return 1.0 if frac < 0.4 else 0.0
    if pattern == "new":
        return 1.0 if frac > 0.7 else 0.0
    if pattern == "phase":
        return 1.0 if 0.45 < frac < 0.6 else 0.0
    raise ValueError(pattern)


def _random_timestamp(rng: random.Random) -> datetime:
    """Random moment in range, skewed toward evenings like real listening."""
    day = START + timedelta(days=rng.randrange((END - START).days))
    hour = int(rng.gauss(mu=17, sigma=5)) % 24
    return day.replace(hour=hour, minute=rng.randrange(60), second=rng.randrange(60))


def generate(n_plays: int, seed: int) -> list[dict]:
    rng = random.Random(seed)

    # Build a fixed track list per artist so the same track shows up repeatedly.
    catalogue = {}
    for name, weight, pattern in ARTISTS:
        catalogue[name] = {
            "weight": weight,
            "pattern": pattern,
            "tracks": [
                {"name": f"{name} Track {i + 1}",
                 "album": f"{name} Album {(i // 4) + 1}",
                 "uri": _fake_track_uri(rng),
                 "duration_ms": rng.randrange(120_000, 360_000)}
                for i in range(TRACKS_PER_ARTIST)
            ],
        }

    events = []
    while len(events) < n_plays:
        ts = _random_timestamp(rng)

        # Pick an artist weighted by (base popularity for this user) x (active at this time).
        names = list(catalogue)
        weights = [catalogue[n]["weight"] * _active_weight(catalogue[n]["pattern"], ts)
                   for n in names]
        if sum(weights) == 0:
            continue
        artist = rng.choices(names, weights=weights, k=1)[0]
        track = rng.choice(catalogue[artist]["tracks"])

        # Most plays finish the track; some are skips.
        skipped = rng.random() < 0.15
        ms_played = rng.randrange(1_000, 30_000) if skipped else track["duration_ms"]

        events.append({
            "ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "platform": rng.choice(PLATFORMS),
            "ms_played": ms_played,
            "conn_country": "US",
            "ip_addr": "0.0.0.0",
            "master_metadata_track_name": track["name"],
            "master_metadata_album_artist_name": artist,
            "master_metadata_album_album_name": track["album"],
            "spotify_track_uri": track["uri"],
            "episode_name": None,
            "episode_show_name": None,
            "spotify_episode_uri": None,
            "reason_start": rng.choice(REASON_START),
            "reason_end": "fwdbtn" if skipped else rng.choice(REASON_END),
            "shuffle": rng.random() < 0.6,
            "skipped": skipped,
            "offline": rng.random() < 0.1,
            "offline_timestamp": None,
            "incognito_mode": False,
        })

    events.sort(key=lambda e: e["ts"])
    return events


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--plays", type=int, default=8000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=Path,
                        default=Path("data/samples/Streaming_History_Audio_2023-2026_0.json"))
    args = parser.parse_args()

    events = generate(args.plays, args.seed)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(events, indent=2))

    artists = {e["master_metadata_album_artist_name"] for e in events}
    print(f"Wrote {len(events):,} plays across {len(artists)} artists to {args.out}")


if __name__ == "__main__":
    main()

# Sonic Iceberg


**Your music taste, drawn as an iceberg.** Upload your Spotify listening history and get a page where the artists everyone knows sit at the top and the ones nobody's heard of sink to the bottom.

## What you get

**The iceberg.** Every artist you've listened to, placed by how famous they are. Bigger names are ones you've spent more hours on. Hover over anyone for your stats with them; click to pin. Every artist has a "play top track" link that opens your most-played song of theirs on Spotify.

**Pick a year.** Buttons at the top switch between your all-time iceberg and any single year.

**Lost at sea.** Artists you were obsessed with for a while and then stopped playing entirely.

**Worth diving for.** New artists in your three biggest genres, chosen because they're similar to the most obscure artists you already like.

**How deep you've gone.** A chart of how mainstream or underground your listening was, month by month.

## Getting your Spotify data

This uses the full listening history Spotify keeps on you, which you have to request from them. It's free, but it takes a few days.

1. Go to **[spotify.com/account/privacy](https://www.spotify.com/account/privacy)** in a web browser and log in.
2. Scroll to **Download your data**. Tick **Extended streaming history** (not the basic "Account data"). Click **Request data**.
3. **Go to your email now.** Spotify sends you a link to confirm the request. Nothing happens until you click it.
4. Wait 3–14 days. Spotify emails you a download link. Download the zip file and don't unzip it.
5. Upload that zip to Sonic Iceberg. Your page takes a minute or two to build.

Your file is deleted as soon as your page is built. Nothing about what you listened to is kept.

## Running it yourself

You need [Docker](https://www.docker.com/products/docker-desktop/) installed and a free [Last.fm API key](https://www.last.fm/api/account/create) (takes one minute; it's how the app finds out how well-known each artist is).

```
git clone https://github.com/achalmdas/sonic-iceberg.git
cd sonic-iceberg
cp .env.example .env         # open .env and paste your Last.fm key after LASTFM_API_KEY=
docker build -t sonic-iceberg .
docker run --rm -it -p 8000:8000 --env-file .env -v iceberg-data:/app/data sonic-iceberg
```

Then open **http://127.0.0.1:8000** in your browser. There's a demo link on the page if you want to see it work before your Spotify data arrives.

**Without Docker** (needs Python 3.12):

```
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn iceberg.app:app --reload --app-dir src
```

Same address, same page.

## For developers

Python 3.12 · DuckDB · FastAPI · Docker · pytest · GitHub Actions

**Pipeline:** `ingest` (Spotify JSON → DuckDB) → `enrich` (Last.fm + Deezer lookups, cached) → `stats` (per-artist totals) → `features` (obscurity score, tiers, churn detection, monthly depth) → `recommend` (similar-artist search) → `render` (one self-contained HTML file). `app.py` wraps it in an upload flow with background jobs and a shared lookup cache. `python run.py` runs the whole thing from the command line; `pytest` runs 18 tests against generated sample data with no network.

**Why Last.fm and Deezer instead of Spotify's popularity score.** Spotify removed `popularity` and `followers` from its API for individual developers in February 2026, along with batch lookups. So fame is measured two ways: Last.fm listeners (broad and well graded, skews rock) and Deezer fans (very top-heavy, skews European), each put on a log scale and blended 75/25. Name lookups against big catalogues hit predictable problems — there are many "Drake"s on Deezer, and Last.fm's autocorrect redirected "Travis Scott" to an old alias page with 5% of the listeners — so both sources are searched and the largest exact-name match is taken, and Deezer is dropped when the two disagree wildly. Both numbers are shown in the hover panel so anything that slips through is visible.

**Design notes.** Obscurity is on a log scale because listener counts span four orders of magnitude. Tiers use fixed thresholds so two people's icebergs are comparable. "Dropped" means 20+ plays in the busiest 90-day window, then 180+ days silent (both are flags). The bundled sample generator plants known patterns — steady, dropped, new, short phase — and the tests assert that churn detection recovers exactly those. Spotify photos and links are optional and best-effort; the dev-mode daily quota is small enough that one large library exhausts it, so artist photos come from Deezer.

**Next.** A real job queue so uploads survive restarts; one shared schema-migration path for the cache tables; PNG export for sharing.

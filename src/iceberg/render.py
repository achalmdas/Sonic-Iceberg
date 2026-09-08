"""Render the iceberg as a single self-contained HTML file.

Reads iceberg_tiers, dropped_artists and taste_depth from DuckDB, embeds
them as JSON, and writes an HTML page with:
  - an iceberg: five bands from sky to abyss, artists floating in their band,
    sized by hours listened, hover for details
  - a "lost at sea" list of dropped artists
  - a taste-depth line over time

No web framework, no external assets: the output opens from disk and can
be emailed or hosted anywhere.

Usage:
    python src/iceberg/render.py
    python src/iceberg/render.py --out data/iceberg.html --title "Achal's iceberg"
"""

import argparse
import json
from datetime import date, datetime
from pathlib import Path

import duckdb

DEFAULT_DB = Path("data/iceberg.duckdb")
DEFAULT_OUT = Path("data/iceberg.html")

TIER_ORDER = ["Surface", "Shallows", "Twilight", "Midnight", "Abyss"]
TIER_BLURB = {
    "Surface":  "Everyone knows these.",
    "Shallows": "Big, but not inescapable.",
    "Twilight": "Fans know them; most people don't.",
    "Midnight": "You had to go looking.",
    "Abyss":    "Nobody put you on to these.",
}


# Outlines as explicit (y, x) polylines in a 0-100 box, so the silhouette has
# real ledges and ridges instead of a smooth curve. Left and right edges are
# listed separately; rows of the mesh are sampled at every y that appears in
# either edge so the outline is reproduced exactly.
TIP_LEFT  = [(0, 54), (8, 50), (14, 46), (18, 44), (24, 40), (26, 36), (34, 33),
             (40, 29), (44, 30), (50, 27), (58, 22), (66, 20), (74, 16), (82, 14),
             (90, 12), (100, 10)]
TIP_RIGHT = [(0, 54), (6, 58), (12, 60), (20, 63), (28, 66), (34, 66), (42, 71),
             (50, 74), (58, 76), (66, 80), (74, 83), (84, 86), (92, 89), (100, 91)]

BODY_LEFT  = [(0, 2), (6, 4), (12, 9), (20, 13), (28, 14), (36, 20), (44, 22),
              (52, 26), (60, 28), (68, 34), (76, 36), (84, 41), (92, 45), (100, 50)]
BODY_RIGHT = [(0, 98), (6, 96), (14, 94), (22, 89), (30, 88), (38, 83), (46, 80),
              (54, 74), (62, 71), (70, 66), (78, 62), (86, 58), (94, 54), (100, 50)]


def _outline(kind: str) -> str:
    left, right = (TIP_LEFT, TIP_RIGHT) if kind == "tip" else (BODY_LEFT, BODY_RIGHT)
    pts = [(x, y) for y, x in left] + [(x, y) for y, x in reversed(right)]
    return " ".join(f"{x},{y}" for x, y in pts)


# Interior planes: a few large faces with jagged boundaries, lit from upper-left.
# Each is a polygon in the 0-100 box, clipped to the silhouette.
PLANES_TIP = [
    ("#FFFFFF", 0.70, [(0,0),(58,0),(52,18),(56,34),(48,52),(52,70),(44,100),(0,100)]),
    ("#C9DDEC", 0.70, [(58,0),(100,0),(100,100),(44,100),(52,70),(48,52),(56,34),(52,18)]),
    ("#B5CFE3", 0.45, [(70,20),(100,10),(100,60),(78,64),(84,44)]),
    ("#FFFFFF", 0.35, [(0,40),(30,36),(24,58),(34,74),(20,100),(0,100)]),
    ("#D9E8F2", 0.5,  [(40,60),(66,54),(72,78),(58,100),(36,100)]),
]
PLANES_BODY = [
    ("#F7FAFC", 0.14, [(0,0),(56,0),(50,20),(54,40),(46,62),(50,80),(42,100),(0,100)]),
    ("#F7FAFC", 0.05, [(56,0),(100,0),(100,100),(42,100),(50,80),(46,62),(54,40),(50,20)]),
    ("#F7FAFC", 0.10, [(62,10),(100,4),(100,50),(72,58),(80,32)]),
    ("#F7FAFC", 0.18, [(0,30),(28,26),(22,52),(34,70),(18,100),(0,100)]),
    ("#F7FAFC", 0.08, [(38,58),(64,52),(70,78),(56,100),(34,100)]),
]


def ice_svg(kind: str) -> str:
    outline = _outline(kind)
    above = kind == "tip"
    planes = PLANES_TIP if above else PLANES_BODY
    base = f'<polygon points="{outline}" fill="{"#E4EEF6" if above else "#F7FAFC"}" fill-opacity="{1 if above else 0.10}"/>'
    faces = "".join(
        f'<polygon points="{" ".join(f"{x},{y}" for x, y in pts)}" fill="{fill}" fill-opacity="{op}" clip-path="url(#clip_{kind})"/>'
        for fill, op, pts in planes)
    return (f'<svg class="ice" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">'
            f'<defs><clipPath id="clip_{kind}"><polygon points="{outline}"/></clipPath></defs>'
            f'{base}{faces}</svg>')


def _json_default(o):
    if isinstance(o, (date, datetime)):
        return o.isoformat()[:10]
    raise TypeError(type(o))


def load_data(db: Path) -> dict:
    con = duckdb.connect(str(db), read_only=True)

    def rows(sql):
        cur = con.execute(sql)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    tiers = rows("""
        SELECT artist_name, plays, hours, listeners, deezer_fans, obscurity, tier, tier_rank,
               top_track, tags, first_played, last_played, days_since_last
        FROM iceberg_tiers WHERE tier != 'Unknown' ORDER BY tier_rank, hours DESC
    """)
    dropped = rows("""
        SELECT artist_name, total_plays, peak_3mo_plays, peak_month, last_played, tier
        FROM dropped_artists ORDER BY peak_3mo_plays DESC
    """)
    depth = rows("SELECT month, plays, depth_score, pct_below_surface FROM taste_depth ORDER BY month")

    # Per-year view: same artists and tiers, but plays/hours/top track counted
    # within that year only. The page flips between years without re-running SQL.
    per_year = rows("""
        WITH yearly AS (
            SELECT YEAR(played_at) AS yr, artist_name, COUNT(*) AS plays,
                   ROUND(SUM(minutes_played) / 60, 2) AS hours,
                   MIN(played_at)::DATE AS first_played, MAX(played_at)::DATE AS last_played
            FROM plays GROUP BY 1, 2
        ),
        ranked AS (
            SELECT YEAR(played_at) AS yr, artist_name, track_name, COUNT(*) AS n,
                   ROW_NUMBER() OVER (PARTITION BY YEAR(played_at), artist_name ORDER BY COUNT(*) DESC, track_name) AS rn
            FROM plays GROUP BY 1, 2, 3
        )
        SELECT y.yr, y.artist_name, y.plays, y.hours, y.first_played, y.last_played,
               r.track_name AS top_track, t.listeners, t.deezer_fans, t.obscurity, t.tier, t.tier_rank, t.tags
        FROM yearly y
        JOIN ranked r ON r.yr = y.yr AND r.artist_name = y.artist_name AND r.rn = 1
        JOIN iceberg_tiers t ON t.artist_name = y.artist_name
        WHERE t.tier != 'Unknown'
        ORDER BY y.yr, t.tier_rank, y.hours DESC
    """)
    years = {}
    for r in per_year:
        years.setdefault(str(r.pop("yr")), []).append(r)

    # Recommendations are optional: the table only exists if recommend.py has run.
    has_recs = con.execute("""
        SELECT COUNT(*) FROM information_schema.tables WHERE table_name = 'recommendations'
    """).fetchone()[0] > 0
    recs = rows("SELECT genre, artist_name, listeners, similar_to, rank FROM recommendations ORDER BY genre, rank") if has_recs else []

    deepest = rows("""
        SELECT artist_name, listeners, plays, obscurity FROM iceberg_tiers
        WHERE obscurity IS NOT NULL ORDER BY obscurity DESC, plays DESC LIMIT 1
    """)
    deepest = deepest[0] if deepest else None
    totals = rows("""
        SELECT COUNT(*) AS artists, ROUND(SUM(hours)) AS hours, SUM(plays) AS plays,
               MIN(first_played)::DATE AS since,
               ROUND(SUM(obscurity * hours) / SUM(hours), 1) AS depth
        FROM iceberg_tiers WHERE tier != 'Unknown'
    """)[0]
    con.close()
    return {"tiers": tiers, "dropped": dropped, "depth": depth, "totals": totals,
            "years": years, "recs": recs, "deepest": deepest,
            "tier_order": TIER_ORDER, "tier_blurb": TIER_BLURB}


TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  :root {
    --sky: #DCE8F1; --ice: #F7FAFC; --ink: #0B1E2D; --foam: #DCE9F2;
    --surface: #5FA8D3; --shallows: #2F7FB3; --twilight: #1F5F8B;
    --midnight: #0F2E4F; --abyss: #06111F; --amber: #F2C57C;
    --serif: "Iowan Old Style", "Palatino Linotype", Palatino, "Book Antiqua", Georgia, serif;
  }
  * { box-sizing: border-box; }
  html { scroll-behavior: smooth; }
  @media (prefers-reduced-motion: reduce) { html { scroll-behavior: auto; } }
  body { margin: 0; font-family: var(--serif); color: var(--ink); background: var(--sky); line-height: 1.5; }

  /* --- above water --- */
  header { max-width: 40rem; margin: 0 auto; padding: 4rem 1.5rem 2rem; }
  h1 { font-size: clamp(2.2rem, 6vw, 3.6rem); font-weight: 400; line-height: 1.05; margin: 0 0 1rem; letter-spacing: -0.01em; }
  .lede { font-size: 1.1rem; margin: 0 0 1.5rem; max-width: 34rem; }
  .totals { display: flex; gap: 2rem; flex-wrap: wrap; font-size: 0.95rem; }
  .totals b { display: block; font-size: 1.6rem; font-weight: 400; line-height: 1.1; }
  .deepest { margin: 1.5rem 0 0; font-size: 1.05rem; max-width: 34rem; }
  .deepest b { font-weight: 400; border-bottom: 1px solid rgba(11,30,45,0.35); }
  .years { margin-top: 2rem; display: flex; gap: 0.25rem; flex-wrap: wrap; }
  .years button { font: inherit; font-size: 0.95rem; background: none; border: 1px solid rgba(11,30,45,0.25);
                  color: var(--ink); padding: 0.3rem 0.8rem; border-radius: 999px; cursor: pointer; }
  .years button[aria-pressed="true"] { background: var(--ink); color: var(--sky); border-color: var(--ink); }
  .years button:focus-visible { outline: 2px solid var(--ink); outline-offset: 2px; }

  /* --- the iceberg --- */
  .berg { position: relative; }
  .above, .below { position: relative; }
  .above { background: var(--sky); }
  .below { background: linear-gradient(180deg, var(--surface) 0%, var(--shallows) 22%, var(--twilight) 48%, var(--midnight) 74%, var(--abyss) 100%); }
  /* the ice: one SVG per region, stretched to fill it, sitting behind the names */
  svg.ice { position: absolute; inset: 0; width: 100%; height: 100%; pointer-events: none; }

  .waterline { position: relative; height: 4px; background: var(--ice); opacity: 0.9; z-index: 1;
               box-shadow: 0 2px 18px rgba(247,250,252,0.6); }

  .tier { position: relative; padding: 2.5rem 1.5rem 3rem; color: var(--foam); }
  .below .tier { text-shadow: 0 1px 12px rgba(6,17,31,0.7), 0 0 2px rgba(6,17,31,0.9); }
  .tier h2 { font-size: 1rem; font-weight: 400; margin: 0 0 0.2rem; opacity: 0.9; text-align: center; }
  .tier p.blurb { margin: 0 0 1.5rem; font-size: 0.9rem; opacity: 0.7; text-align: center; }
  .tier-inner { margin: 0 auto; max-width: min(var(--w), var(--vw)); }
  .tier-Surface  { --w: 30rem; --vw: 40vw; color: var(--ink); padding-top: 9rem; padding-bottom: 2.5rem; }
  .tier-Surface p.blurb { opacity: 0.7; }
  .tier-Shallows { --w: 58rem; --vw: 66vw; padding-top: 2.5rem; }
  .tier-Twilight { --w: 46rem; --vw: 52vw; }
  .tier-Midnight { --w: 34rem; --vw: 32vw; }
  .tier-Abyss    { --w: 22rem; --vw: 22vw; padding-bottom: 10rem; }
  @media (max-width: 700px) { .tier-inner { max-width: 92vw; } }

  .artists { display: flex; flex-wrap: wrap; justify-content: center; align-items: baseline; gap: 0.35rem 1.4rem; }
  .artist { background: none; border: 0; font: inherit; color: inherit; cursor: pointer; padding: 0.1rem 0.2rem;
            border-bottom: 1px solid transparent; }
  .artist:hover, .artist:focus-visible { border-bottom-color: currentColor; outline: none; }
  .artist.dropped { color: var(--amber); }
  .above .artist.dropped { color: #A8641A; }
  .empty { opacity: 0.5; font-style: italic; }

  /* detail panel */
  #panel { position: fixed; right: 1rem; bottom: 1rem; width: min(22rem, calc(100vw - 2rem));
           background: var(--ice); color: var(--ink); padding: 1.1rem 1.25rem; border-radius: 4px;
           box-shadow: 0 8px 30px rgba(6,17,31,0.35); display: none; font-size: 0.95rem; }
  #panel.open { display: block; }
  #panel h3 { margin: 0 0 0.3rem; font-weight: 400; font-size: 1.3rem; }
  #panel dl { margin: 0.6rem 0 0; display: grid; grid-template-columns: auto 1fr; gap: 0.15rem 0.8rem; }
  #panel dt { opacity: 0.6; } #panel dd { margin: 0; }
  #panel .tags { margin-top: 0.6rem; font-size: 0.85rem; opacity: 0.75; }
  #panel .note { margin-top: 0.6rem; color: #8A5A1E; }
  #panel button.close { position: absolute; top: 0.4rem; right: 0.6rem; background: none; border: 0; font: inherit; cursor: pointer; opacity: 0.6; }

  /* --- below the iceberg --- */
  section.after { background: var(--abyss); color: var(--foam); padding: 3rem 1.5rem 5rem; }
  section.after .inner { max-width: 40rem; margin: 0 auto; }
  section.after h2 { font-weight: 400; font-size: 1.6rem; margin: 0 0 0.5rem; }
  section.after p { opacity: 0.75; margin: 0 0 1.5rem; }
  ol.lost { list-style: none; padding: 0; margin: 0 0 3rem; }
  ol.lost li { display: grid; grid-template-columns: 1fr auto; gap: 1rem; padding: 0.6rem 0; border-bottom: 1px solid rgba(220,233,242,0.12); }
  ol.lost li span.name { color: var(--amber); }
  ol.lost li small { opacity: 0.6; display: block; }
  svg.depth { width: 100%; height: auto; display: block; overflow: visible; }
  svg.depth text { font-family: var(--serif); font-size: 12px; fill: var(--foam); opacity: 0.7; }
  svg.depth .sea { fill: url(#seafill); }
  svg.depth .floor { fill: none; stroke: var(--ice); stroke-width: 2; stroke-linejoin: round; }
  .recs { display: grid; gap: 2rem; grid-template-columns: repeat(auto-fit, minmax(14rem, 1fr)); margin-bottom: 3rem; }
  .recs h3 { font-weight: 400; font-size: 1.1rem; margin: 0 0 0.5rem; padding-bottom: 0.3rem; border-bottom: 1px solid rgba(220,233,242,0.25); }
  .recs ol { list-style: none; margin: 0; padding: 0; }
  .recs li { padding: 0.45rem 0; }
  .recs li small { display: block; opacity: 0.55; }
  footer { font-size: 0.85rem; opacity: 0.5; margin-top: 3rem; }
</style>
</head>
<body>

<header>
  <h1>__TITLE__</h1>
  <p class="lede">Every artist you've listened to, sorted by how many people on Earth know them. Scroll down to go deeper.</p>
  <div class="totals" id="totals"></div>
  <p class="deepest" id="deepest"></p>
  <div class="years" id="years" role="group" aria-label="Choose a year"></div>
</header>

<div class="berg" id="berg"></div>

<section class="after">
  <div class="inner">
    <h2>Lost at sea</h2>
    <p>Artists you played hard for a stretch, then stopped.</p>
    <ol class="lost" id="lost"></ol>

    <div id="recs-block">
      <h2>Worth diving for</h2>
      <p>Artists in your three biggest genres that you haven't played, chosen because they sit next to the deepest cuts you already like.</p>
      <div class="recs" id="recs"></div>
    </div>

    <h2>How deep you've gone</h2>
    <p>Each month's average obscurity, drawn as depth. The line is how far under the surface your listening sat.</p>
    <svg class="depth" id="depth" viewBox="0 0 640 220" role="img" aria-label="Taste depth over time"></svg>

    <footer>Listener counts are Last.fm listeners (people who have scrobbled the artist), which run far lower than Spotify monthly listeners but rank artists similarly. Listening history from your Spotify export.</footer>
  </div>
</section>

<aside id="panel" aria-live="polite">
  <button class="close" aria-label="Close">×</button>
  <h3></h3>
  <dl></dl>
  <div class="tags"></div>
  <div class="note"></div>
</aside>

<script id="data" type="application/json">__DATA__</script>
<script>
(function () {
  const D = JSON.parse(document.getElementById('data').textContent);
  const droppedSet = new Set(D.dropped.map(d => d.artist_name));
  const fmt = n => Number(n).toLocaleString();

  const berg = document.getElementById('berg');
  const panel = document.getElementById('panel');
  let current = D.tiers;          // the dataset currently on screen (all-time or one year)
  let byName = {};

  function totalsFor(rows) {
    const hours = rows.reduce((s, a) => s + a.hours, 0);
    const depth = hours ? (rows.reduce((s, a) => s + a.obscurity * a.hours, 0) / hours).toFixed(1) : '–';
    const since = rows.map(a => a.first_played).sort()[0] || '–';
    return { artists: rows.length, hours: Math.round(hours), plays: rows.reduce((s, a) => s + a.plays, 0), since, depth };
  }
  function renderTotals(rows) {
    const t = totalsFor(rows);
    document.getElementById('totals').innerHTML = [
      ['artists', fmt(t.artists)], ['hours', fmt(t.hours)],
      ['plays', fmt(t.plays)], ['listening since', t.since], ['depth score', t.depth]
    ].map(([k, v]) => `<div><b>${v}</b>${k}</div>`).join('');
  }

  // artist font size scales with sqrt(hours) so big listens dominate without swamping
  let size = h => '1rem';
  const tierHtml = tier => {
    const artists = current.filter(a => a.tier === tier);
    return `<div class="tier tier-${tier}"><div class="tier-inner">
      <h2>${tier}</h2><p class="blurb">${D.tier_blurb[tier]}</p>
      <div class="artists">` +
      (artists.length ? artists.map(a =>
        `<button class="artist${droppedSet.has(a.artist_name) ? ' dropped' : ''}"
                 style="font-size:${size(a.hours)}" data-name="${a.artist_name.replace(/"/g, '&quot;')}">${a.artist_name}</button>`
      ).join('') : '<span class="empty">Nothing this deep yet.</span>') +
      `</div></div></div>`;
  };
  function renderBerg(rows) {
    current = rows;
    byName = Object.fromEntries(rows.map(a => [a.artist_name, a]));
    const maxHours = Math.max(...rows.map(a => a.hours), 1);
    size = h => (0.85 + 1.9 * Math.sqrt(h / maxHours)).toFixed(2) + 'rem';
    berg.innerHTML =
      `<div class="above">__ICE_TIP__${tierHtml('Surface')}</div>` +
      `<div class="waterline"></div>` +
      `<div class="below">__ICE_BODY__${D.tier_order.slice(1).map(tierHtml).join('')}</div>`;
    renderTotals(rows);
  }

  // year selector
  const yearsEl = document.getElementById('years');
  const yearKeys = Object.keys(D.years).sort();
  const choices = [['all', 'All time'], ...yearKeys.map(y => [y, y])];
  yearsEl.innerHTML = choices.map(([k, label]) =>
    `<button type="button" data-year="${k}" aria-pressed="${k === 'all'}">${label}</button>`).join('');
  yearsEl.addEventListener('click', e => {
    const b = e.target.closest('button'); if (!b) return;
    yearsEl.querySelectorAll('button').forEach(x => x.setAttribute('aria-pressed', x === b));
    renderBerg(b.dataset.year === 'all' ? D.tiers : D.years[b.dataset.year]);
    panel.classList.remove('open');
  });
  renderBerg(D.tiers);

  // detail panel
  const dropInfo = Object.fromEntries(D.dropped.map(d => [d.artist_name, d]));
  function show(name) {
    const a = byName[name]; if (!a) return;
    panel.querySelector('h3').textContent = a.artist_name;
    panel.querySelector('dl').innerHTML = [
      ['plays', fmt(a.plays)], ['hours', a.hours],
      ['top track', a.top_track],
      ['Last.fm listeners', fmt(a.listeners)], ['Deezer fans', a.deezer_fans ? fmt(a.deezer_fans) : '–'],
      ['obscurity', a.obscurity + ' / 100'],
      ['first played', a.first_played], ['last played', a.last_played]
    ].map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('');
    panel.querySelector('.tags').textContent = (a.tags || []).slice(0, 5).join(', ');
    const d = dropInfo[name];
    panel.querySelector('.note').textContent = d
      ? `Dropped: ${fmt(d.peak_3mo_plays)} plays around ${d.peak_month}, then silence.` : '';
    panel.classList.add('open');
  }
  berg.addEventListener('mouseover', e => { const b = e.target.closest('.artist'); if (b) show(b.dataset.name); });
  berg.addEventListener('focusin',  e => { const b = e.target.closest('.artist'); if (b) show(b.dataset.name); });
  panel.querySelector('.close').addEventListener('click', () => panel.classList.remove('open'));

  // deepest cut
  if (D.deepest) {
    const d = D.deepest;
    document.getElementById('deepest').innerHTML =
      `Your deepest cut is <b>${d.artist_name}</b>: ${fmt(d.listeners)} Last.fm listeners, ${fmt(d.plays)} of them yours.`;
  }

  // recommendations
  if (D.recs.length) {
    const byGenre = {};
    D.recs.forEach(r => (byGenre[r.genre] = byGenre[r.genre] || []).push(r));
    document.getElementById('recs').innerHTML = Object.entries(byGenre).map(([g, list]) =>
      `<div><h3>${g}</h3><ol>` + list.map(r =>
        `<li>${r.artist_name}<small>${fmt(r.listeners)} listeners · like ${r.similar_to.slice(0, 2).join(', ')}</small></li>`
      ).join('') + `</ol></div>`).join('');
  } else {
    document.getElementById('recs-block').style.display = 'none';
  }

  // lost at sea
  document.getElementById('lost').innerHTML = D.dropped.length ? D.dropped.map(d =>
    `<li><div><span class="name">${d.artist_name}</span><small>${d.tier} · last played ${d.last_played}</small></div>
         <div>${fmt(d.peak_3mo_plays)} plays<small>peak, ${d.peak_month.slice(0,7)}</small></div></li>`
  ).join('') : '<li class="empty">Nobody dropped. Loyal listener.</li>';

  // depth line (plain SVG, no library)
  const svg = document.getElementById('depth');
  const pts = D.depth;
  if (pts.length > 1) {
    // Depth is drawn downward: surface at the top, deeper months lower.
    const W = 640, H = 220, L = 52, R = 8, T = 16, B = 28;
    const ys = pts.map(p => p.depth_score);
    const yMin = Math.max(0, Math.floor(Math.min(...ys) - 3)), yMax = Math.ceil(Math.max(...ys) + 3);
    const x = i => L + i * (W - L - R) / (pts.length - 1);
    const y = v => T + (H - T - B) * ((v - yMin) / (yMax - yMin));
    const line = pts.map((p, i) => (i ? 'L' : 'M') + x(i).toFixed(1) + ' ' + y(p.depth_score).toFixed(1)).join(' ');
    const area = `M${L} ${T} ` + line.slice(1) + ` L${x(pts.length - 1).toFixed(1)} ${T} Z`;
    const years = pts.map((p, i) => ({ i, yr: p.month.slice(0, 4) })).filter((p, i, arr) => i === 0 || p.yr !== arr[i - 1].yr);
    svg.innerHTML =
      `<defs><linearGradient id="seafill" x1="0" y1="0" x2="0" y2="1">
         <stop offset="0" stop-color="#5FA8D3"/><stop offset="0.5" stop-color="#1F5F8B"/><stop offset="1" stop-color="#0F2E4F"/>
       </linearGradient></defs>` +
      `<line x1="${L}" x2="${W - R}" y1="${T}" y2="${T}" stroke="#F7FAFC" stroke-opacity="0.9" stroke-width="2"/>` +
      `<path class="sea" d="${area}"/><path class="floor" d="${line}"/>` +
      `<text x="${L - 8}" y="${T + 4}" text-anchor="end">surface</text>` +
      `<text x="${L - 8}" y="${y(yMax) + 4}" text-anchor="end">deep</text>` +
      years.map(p => `<text x="${x(p.i)}" y="${H - 8}">${p.yr}</text>`).join('');
  }
})();
</script>
</body>
</html>
"""


def render(db: Path, out: Path, title: str) -> None:
    data = load_data(db)
    payload = json.dumps(data, default=_json_default).replace("</", "<\\/")
    html = (TEMPLATE.replace("__TITLE__", title).replace("__DATA__", payload)
            .replace("__ICE_TIP__", ice_svg("tip")).replace("__ICE_BODY__", ice_svg("body")))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"Wrote {out} ({len(data['tiers'])} artists, {len(data['dropped'])} dropped)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--title", default="Your music iceberg")
    args = parser.parse_args()
    render(args.db, args.out, args.title)


if __name__ == "__main__":
    main()

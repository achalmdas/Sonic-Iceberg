"""Web app: upload a Spotify Extended Streaming History zip, get an iceberg.

    uvicorn iceberg.app:app --reload --app-dir src

Routes:
    GET  /                 upload page
    POST /upload           accept the zip, start a background job, redirect to /job/{id}
    GET  /job/{id}         status page that polls until the job finishes
    GET  /api/job/{id}     JSON status for the poller
    GET  /iceberg/{id}     the finished HTML
    GET  /demo             iceberg built from the bundled sample data

Each job runs in its own folder under data/jobs/{id}/ with its own DuckDB.
Artist lookups are shared through data/cache.duckdb so an artist is only
ever fetched from Last.fm / Deezer once, across all users.
"""

import json
import os
import re
import shutil
import traceback
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import duckdb
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from iceberg import enrich, features, ingest, recommend, render, stats

DATA_DIR = Path(os.getenv("ICEBERG_DATA_DIR", "data"))
JOBS_DIR = DATA_DIR / "jobs"
CACHE_DB = DATA_DIR / "cache.duckdb"
SAMPLE_DIR = DATA_DIR / "samples"

MAX_UPLOAD_BYTES = 200 * 1024 * 1024        # 200 MB zip
MAX_JSON_BYTES = 1_500 * 1024 * 1024        # 1.5 GB unpacked, total
HISTORY_FILE = re.compile(r"Streaming_History_Audio_.*\.json$")
SKIP_RECS = os.getenv("ICEBERG_SKIP_RECS") == "1"

app = FastAPI(title="Sonic Iceberg")


# --------------------------------------------------------------------------
# Job bookkeeping: one folder per job, a status.json inside it.
# --------------------------------------------------------------------------
def job_dir(job_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{12}", job_id):
        raise HTTPException(404)
    return JOBS_DIR / job_id


def write_status(job: Path, stage: str, **extra) -> None:
    payload = {"stage": stage, "updated": datetime.now(timezone.utc).isoformat(), **extra}
    (job / "status.json").write_text(json.dumps(payload))


def read_status(job: Path) -> dict:
    try:
        return json.loads((job / "status.json").read_text())
    except FileNotFoundError:
        raise HTTPException(404)


# --------------------------------------------------------------------------
# Shared artist cache
# --------------------------------------------------------------------------
def warm_from_cache(db: Path) -> None:
    """Copy already-known artists from the shared cache into this job's DB."""
    if not CACHE_DB.exists():
        return
    con = duckdb.connect(str(db))
    con.execute(f"ATTACH '{CACHE_DB}' AS cache (READ_ONLY)")
    con.execute(enrich.CREATE_ARTISTS)
    con.execute(recommend.CREATE_CACHE)
    # BY NAME matches columns by name, so cache and job schemas can differ in order.
    for table, where in [("artists", "WHERE artist_name IN (SELECT DISTINCT artist_name FROM plays)"),
                         ("candidate_cache", "")]:
        if _has_table(con, "cache", table):
            con.execute(f"""INSERT INTO {table} BY NAME
                            SELECT * FROM cache.{table} {where or "WHERE 1=1"}
                            AND artist_name NOT IN (SELECT artist_name FROM {table})""")
    con.close()


def _has_table(con, schema_db: str, table: str) -> bool:
    return con.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_catalog = ? AND table_name = ?",
        [schema_db, table]).fetchone()[0] > 0


def store_to_cache(db: Path) -> None:
    """Merge this job's lookups back into the shared cache."""
    con = duckdb.connect(str(CACHE_DB))
    con.execute(enrich.CREATE_ARTISTS)
    con.execute(recommend.CREATE_CACHE)
    con.execute(f"ATTACH '{db}' AS job (READ_ONLY)")
    for table in ("artists", "candidate_cache"):
        if _has_table(con, "job", table):
            con.execute(f"""INSERT INTO {table} BY NAME
                            SELECT * FROM job.{table}
                            WHERE artist_name NOT IN (SELECT artist_name FROM {table})""")
    con.close()


# --------------------------------------------------------------------------
# The pipeline, as a background task
# --------------------------------------------------------------------------
def run_pipeline(job: Path, title: str) -> None:
    db = job / "iceberg.duckdb"
    out = job / "iceberg.html"
    try:
        write_status(job, "ingesting")
        n = ingest.ingest(job / "export", db)
        if n == 0:
            raise ValueError("No music plays found in the export.")

        write_status(job, "enriching", plays=n)
        warm_from_cache(db)
        enrich.enrich(db, progress=lambda done, total: write_status(job, "enriching", plays=n, done=done, total=total))
        store_to_cache(db)

        write_status(job, "analyzing", plays=n)
        stats.build(db)
        features.build(db, churn_plays=20, churn_gap=180)

        if not SKIP_RECS:
            write_status(job, "recommending", plays=n)
            recommend.recommend(db, n_genres=3, per_genre=5, max_listeners=750_000)
            store_to_cache(db)

        write_status(job, "rendering", plays=n)
        render.render(db, out, title)
        write_status(job, "done", plays=n)
    except Exception as exc:  # noqa: BLE001 — surface anything to the status page
        traceback.print_exc()
        write_status(job, "failed", error=str(exc))
    finally:
        shutil.rmtree(job / "export", ignore_errors=True)   # the raw export is personal; don't keep it


def extract_history(zip_path: Path, dest: Path) -> int:
    """Pull only the streaming-history JSON files out of the zip. Returns count."""
    dest.mkdir(parents=True, exist_ok=True)
    total, count = 0, 0
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            name = Path(info.filename).name
            if info.is_dir() or not HISTORY_FILE.search(name) or ".." in info.filename:
                continue
            total += info.file_size
            if total > MAX_JSON_BYTES:
                raise ValueError("Export too large.")
            with zf.open(info) as src, open(dest / name, "wb") as dst:
                shutil.copyfileobj(src, dst)
            count += 1
    return count


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return PAGE.format(body=UPLOAD_FORM)


@app.post("/upload")
async def upload(background: BackgroundTasks, file: UploadFile = File(...), title: str = Form("")) -> RedirectResponse:
    if not file.filename or not file.filename.lower().endswith(".zip"):
        raise HTTPException(400, "Please upload the .zip Spotify sent you.")

    job_id = uuid.uuid4().hex[:12]
    job = JOBS_DIR / job_id
    job.mkdir(parents=True)
    zip_path = job / "upload.zip"

    size = 0
    with open(zip_path, "wb") as f:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                shutil.rmtree(job)
                raise HTTPException(413, "Zip is over 200 MB.")
            f.write(chunk)

    try:
        n_files = extract_history(zip_path, job / "export")
    except (zipfile.BadZipFile, ValueError) as exc:
        shutil.rmtree(job)
        raise HTTPException(400, f"Couldn't read that zip: {exc}")
    finally:
        zip_path.unlink(missing_ok=True)
    if n_files == 0:
        shutil.rmtree(job)
        raise HTTPException(400, "No Streaming_History_Audio_*.json files in that zip. "
                                 "Make sure you requested the *extended* streaming history.")

    write_status(job, "queued", files=n_files)
    background.add_task(run_pipeline, job, title.strip() or "Your music iceberg")
    return RedirectResponse(f"/job/{job_id}", status_code=303)


@app.get("/job/{job_id}", response_class=HTMLResponse)
def job_page(job_id: str) -> str:
    read_status(job_dir(job_id))
    return PAGE.format(body=STATUS_PAGE.replace("__JOB__", job_id))


@app.get("/api/job/{job_id}")
def job_status(job_id: str) -> JSONResponse:
    return JSONResponse(read_status(job_dir(job_id)))


@app.get("/iceberg/{job_id}", response_class=HTMLResponse)
def iceberg(job_id: str) -> str:
    html = job_dir(job_id) / "iceberg.html"
    if not html.exists():
        raise HTTPException(404)
    return html.read_text(encoding="utf-8")


@app.get("/demo", response_class=HTMLResponse)
def demo(background: BackgroundTasks) -> RedirectResponse:
    """Build (once) and show the iceberg for the bundled sample data."""
    job = JOBS_DIR / "demo"
    if (job / "iceberg.html").exists():
        return RedirectResponse("/iceberg/demo")
    job.mkdir(parents=True, exist_ok=True)
    shutil.copytree(SAMPLE_DIR, job / "export", dirs_exist_ok=True)
    write_status(job, "queued")
    background.add_task(run_pipeline, job, "Sample iceberg")
    return RedirectResponse("/job/demo", status_code=303)


# `demo` is not a 12-hex id, so let it through job_dir's check.
_orig_job_dir = job_dir
def job_dir(job_id: str) -> Path:  # noqa: F811
    return JOBS_DIR / "demo" if job_id == "demo" else _orig_job_dir(job_id)


# --------------------------------------------------------------------------
# Minimal pages, same palette as the iceberg itself
# --------------------------------------------------------------------------
PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Sonic Iceberg</title>
<style>
  body {{ margin:0; font-family:"Iowan Old Style","Palatino Linotype",Palatino,Georgia,serif; color:#0B1E2D;
         background:linear-gradient(180deg,#DCE8F1 0,#DCE8F1 55%,#5FA8D3 100%); min-height:100vh; line-height:1.5; }}
  main {{ max-width:36rem; margin:0 auto; padding:5rem 1.5rem; }}
  h1 {{ font-weight:400; font-size:2.8rem; margin:0 0 .5rem; letter-spacing:-.01em; }}
  p {{ margin:0 0 1.25rem; }}
  label {{ display:block; margin:1.25rem 0 .4rem; }}
  input[type=text] {{ font:inherit; width:100%; padding:.5rem .7rem; border:1px solid rgba(11,30,45,.3); border-radius:4px; background:#F7FAFC; }}
  input[type=file] {{ font:inherit; }}
  button {{ font:inherit; background:#0B1E2D; color:#DCE8F1; border:0; padding:.6rem 1.3rem; border-radius:999px; cursor:pointer; margin-top:1.5rem; }}
  details {{ margin-top:2.5rem; font-size:.95rem; }} summary {{ cursor:pointer; }}
  ol {{ padding-left:1.2rem; }} li {{ margin:.3rem 0; }}
  .status {{ font-size:1.4rem; }} .muted {{ opacity:.6; }} a {{ color:inherit; }}
</style></head><body><main>{body}</main></body></html>"""

UPLOAD_FORM = """
<h1>Sonic Iceberg</h1>
<p>Turn your Spotify listening history into an iceberg: the artists everyone knows at the top, the ones nobody's heard of at the bottom. Plus the artists you've dropped, how deep your taste has gone over time, and what to listen to next.</p>
<form action="/upload" method="post" enctype="multipart/form-data">
  <label for="file">Your Spotify export (.zip)</label>
  <input id="file" type="file" name="file" accept=".zip" required>
  <label for="title">Title (optional)</label>
  <input id="title" type="text" name="title" placeholder="Achal's iceberg">
  <button type="submit">Build my iceberg</button>
</form>
<p style="margin-top:1.5rem"><a href="/demo">Or see a demo built from sample data</a></p>
<details><summary>How do I get my export?</summary>
<ol>
  <li>Go to spotify.com/account/privacy</li>
  <li>Under "Download your data", tick <b>Extended streaming history</b> and request it</li>
  <li>Spotify emails a zip in 3–14 days. Upload that zip here as-is.</li>
</ol>
<p class="muted">Your export is processed and deleted; only the finished page and anonymous artist lookups are kept.</p>
</details>"""

STATUS_PAGE = """
<h1>Building your iceberg</h1>
<p class="status" id="stage">Queued…</p>
<p class="muted" id="detail">Large histories take a few minutes: every artist is looked up on Last.fm and Deezer, and results are cached for the next person.</p>
<script>
  const labels = { queued:'Queued…', ingesting:'Reading your history…', enriching:'Looking up artists…',
                   analyzing:'Finding tiers, dropped artists, and taste depth…', recommending:'Finding things worth diving for…',
                   rendering:'Drawing the iceberg…', done:'Done. Opening…', failed:'Something went wrong.' };
  async function poll() {
    const r = await fetch('/api/job/__JOB__'); const s = await r.json();
    document.getElementById('stage').textContent = labels[s.stage] || s.stage;
    let detail = s.plays ? s.plays.toLocaleString() + ' plays found.' : '';
    if (s.total) detail += ' ' + s.done + ' / ' + s.total + ' artists looked up.';
    if (detail) document.getElementById('detail').textContent = detail;
    if (s.stage === 'done') { location.href = '/iceberg/__JOB__'; return; }
    if (s.stage === 'failed') { document.getElementById('detail').textContent = s.error || ''; return; }
    setTimeout(poll, 2000);
  }
  poll();
</script>"""

"""Round-trips a zip through the web app. The pipeline runs synchronously under
TestClient, and network lookups are skipped via ICEBERG_SKIP_RECS + a warmed cache."""

import io
import os
import zipfile

import duckdb
import pytest
from conftest import LISTENERS


@pytest.fixture
def client(tmp_path, export_dir, monkeypatch):
    monkeypatch.setenv("ICEBERG_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ICEBERG_SKIP_RECS", "1")
    # Pre-fill the shared cache so enrich() has nothing to fetch.
    from iceberg import enrich
    con = duckdb.connect(str(tmp_path / "cache.duckdb"))
    con.execute(enrich.CREATE_ARTISTS)
    con.executemany("INSERT INTO artists (artist_name, lastfm_name, listeners, playcount, tags, deezer_fans) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    [(n, n, l, l * 20, ["tag"], int(l * 0.8)) for n, l in LISTENERS.items()])
    con.close()

    import importlib
    from iceberg import app as app_module
    importlib.reload(app_module)              # pick up the env vars
    from fastapi.testclient import TestClient
    return TestClient(app_module.app), export_dir


def make_zip(export_dir, nested=True) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for f in export_dir.glob("*.json"):
            zf.write(f, f"Spotify Extended Streaming History/{f.name}" if nested else f.name)
        zf.writestr("Spotify Extended Streaming History/ReadMeFirst.pdf", b"ignored")
    return buf.getvalue()


def test_upload_round_trip(client):
    c, export_dir = client
    r = c.post("/upload", files={"file": ("data.zip", make_zip(export_dir), "application/zip")},
               data={"title": "Test berg"}, follow_redirects=False)
    assert r.status_code == 303
    job = r.headers["location"].rsplit("/", 1)[-1]
    assert c.get(f"/api/job/{job}").json()["stage"] == "done"
    page = c.get(f"/iceberg/{job}")
    assert page.status_code == 200
    assert "<title>Test berg</title>" in page.text
    assert "Kendrick Lamar" in page.text


def test_raw_export_is_deleted_after_processing(client, tmp_path):
    c, export_dir = client
    r = c.post("/upload", files={"file": ("data.zip", make_zip(export_dir), "application/zip")}, follow_redirects=False)
    job = r.headers["location"].rsplit("/", 1)[-1]
    assert not (tmp_path / "jobs" / job / "export").exists()
    assert not (tmp_path / "jobs" / job / "upload.zip").exists()


def test_rejects_non_zip(client):
    c, _ = client
    r = c.post("/upload", files={"file": ("data.zip", b"definitely not a zip", "application/zip")})
    assert r.status_code == 400


def test_rejects_zip_without_history(client):
    c, _ = client
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Playlist1.json", "[]")
    r = c.post("/upload", files={"file": ("data.zip", buf.getvalue(), "application/zip")})
    assert r.status_code == 400
    assert "extended" in r.json()["detail"].lower()


def test_unknown_job_is_404(client):
    c, _ = client
    assert c.get("/iceberg/000000000000").status_code == 404
    assert c.get("/iceberg/../../.env").status_code in (404, 400)

"""API-layer tests via FastAPI TestClient: slug validation, canvas lifecycle
(create/duplicate/delete), render/code/figure endpoints, palettes, and dataset
list/signals/upload. Canvas tests use the real user dir, so any canvas created is
deleted again; dataset tests run against a throwaway data dir (data_dir fixture)."""
import io

import pytest
from fastapi.testclient import TestClient

from figfoundry.app import app


@pytest.fixture
def client():
    return TestClient(app)


def _create(client):
    r = client.post("/api/canvases", json={"mode": "blank"})
    assert r.status_code == 200
    return r.json()["slug"]


def test_slug_traversal_rejected(client):
    # A crafted slug with path separators must 404, not read outside the canvases dir.
    r = client.get("/api/canvases/..%2F..%2Fetc")
    assert r.status_code == 404


def test_dataset_signals_requires_valid_name(client):
    # Traversal-ish names must never be served (400 = rejected, 404 = no route).
    for url in ["/api/datasets/..%2F..%2Fetc%2Fpasswd/signals", "/api/datasets/....//signals"]:
        assert client.get(url).status_code in (400, 404)


def test_blank_canvas_renders(client):
    import base64

    r = client.post("/api/canvases", json={"mode": "blank"})
    assert r.status_code == 200
    slug = r.json()["slug"]
    try:
        rr = client.post(f"/api/canvases/{slug}/render", json={})
        assert rr.status_code == 200
        body = rr.json()
        assert body["mime"] == "image/png" and body["draft"] is False
        assert body["hotspots"] == []  # blank canvas: nothing to hit-test
        assert base64.b64decode(body["img"])[:8] == b"\x89PNG\r\n\x1a\n"
        assert body["img_w"] > 0 and body["img_h"] > 0
    finally:
        client.delete(f"/api/canvases/{slug}")


def test_duplicate_and_delete_canvas(client):
    slug = _create(client)
    try:
        orig = client.get(f"/api/canvases/{slug}").json()
        r = client.post(f"/api/canvases/{slug}/duplicate")
        assert r.status_code == 200
        body = r.json()
        assert body["slug"] != slug
        assert body["name"] == "Untitled copy"
        copy = client.get(f"/api/canvases/{body['slug']}").json()
        assert copy["spec"] == orig["spec"]  # deep copy, same scene
        assert client.delete(f"/api/canvases/{body['slug']}").status_code == 200
        assert client.get(f"/api/canvases/{body['slug']}").status_code == 404
    finally:
        client.delete(f"/api/canvases/{slug}")
    assert client.get(f"/api/canvases/{slug}").status_code == 404


def test_code_endpoint(client):
    slug = _create(client)
    try:
        r = client.get(f"/api/canvases/{slug}/code")
        assert r.status_code == 200
        assert "def build():" in r.json()["code"]
    finally:
        client.delete(f"/api/canvases/{slug}")


def test_figure_vector_export_via_api(client):
    slug = _create(client)
    try:
        svg = client.get(f"/api/canvases/{slug}/figure", params={"format": "svg"})
        assert svg.status_code == 200
        assert svg.content[:5] == b"<?xml"
        pdf = client.get(f"/api/canvases/{slug}/figure", params={"format": "pdf"})
        assert pdf.status_code == 200
        assert pdf.content[:4] == b"%PDF"
        assert client.get(f"/api/canvases/{slug}/figure", params={"format": "gif"}).status_code == 400
    finally:
        client.delete(f"/api/canvases/{slug}")


def test_palettes_endpoint(client):
    r = client.get("/api/palettes")
    assert r.status_code == 200
    pals = r.json()["palettes"]
    assert "Okabe-Ito (colorblind-safe)" in pals
    for colors in pals.values():
        assert colors and all(c.startswith("#") and len(c) == 7 for c in colors)


def test_datasets_endpoint_lists_and_signals(client, data_dir):
    r = client.get("/api/datasets")
    assert r.status_code == 200
    row = next(d for d in r.json()["datasets"] if d["name"] == "sample.npz")
    assert row["kind"] == "npz"
    assert row["n_signals"] == 7
    s = client.get("/api/datasets/sample.npz/signals")
    assert s.status_code == 200
    assert {"x", "y", "M", "group"} <= {sig["id"] for sig in s.json()["signals"]}
    assert client.get("/api/datasets/nope.npz/signals").status_code == 404


def test_upload_dataset_csv_and_reject_txt(client, data_dir):
    r = client.post("/api/datasets", files={"file": ("up.csv", b"a,b\n1,2\n3,4\n", "text/csv")})
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "up.csv"
    assert body["kind"] == "csv"
    assert {sig["id"] for sig in body["signals"]} == {"a", "b"}
    assert client.post("/api/datasets", files={"file": ("x.txt", b"hello", "text/plain")}).status_code == 400


# ------------------------------------------------------------------ render body
def test_render_accepts_body_spec_and_draft(client, data_dir):
    """POST /render with a spec in the body overrides the stored canvas, and
    draft=True switches to the soft JPEG path (no hotspots)."""
    import base64

    from figfoundry import canvas
    slug = _create(client)  # stored spec is blank
    try:
        spec = canvas.blank_spec()
        spec["dataset"] = "sample.npz"
        spec["plots"] = [canvas._new_plot("zz", [0.1, 0.15, 0.8, 0.7], "@x", "@y")]
        full = client.post(f"/api/canvases/{slug}/render", json={"spec": spec})
        assert full.status_code == 200
        body = full.json()
        assert body["mime"] == "image/png"
        assert "plot:zz" in {h["id"] for h in body["hotspots"]}  # body spec, not the blank stored one
        draft = client.post(f"/api/canvases/{slug}/render", json={"spec": spec, "draft": True})
        assert draft.status_code == 200
        b2 = draft.json()
        assert b2["mime"] == "image/jpeg" and b2["draft"] is True and b2["hotspots"] == []
        assert base64.b64decode(b2["img"])[:2] == b"\xff\xd8"
    finally:
        client.delete(f"/api/canvases/{slug}")


# ------------------------------------------------------- 2-D dataset end-to-end
def test_2d_pkl_dataset_end_to_end(client, data_dir):
    """A 2-D pkl dataset: upload -> signals report ndim 2 -> a canvas bound to it
    renders an imshow (exercises the whitelist unpickler embedded in the codegen)."""
    import base64
    import pickle

    import numpy as np
    buf = io.BytesIO()
    pickle.dump({"field": np.outer(np.arange(6.0), np.arange(6.0))}, buf)
    r = client.post("/api/datasets", files={"file": ("grid.pkl", buf.getvalue(), "application/octet-stream")})
    assert r.status_code == 200 and r.json()["kind"] == "pkl"
    sig = next(s for s in r.json()["signals"] if s["id"] == "field")
    assert sig["ndim"] == 2 and sig["shape"] == [6, 6]

    from figfoundry import canvas
    slug = _create(client)
    try:
        spec = canvas.blank_spec()
        spec["dataset"] = "grid.pkl"
        p = canvas._new_plot("p0", [0.1, 0.15, 0.8, 0.7], None, None)
        p["type"] = "imshow"
        p["data"]["field"] = "@field"
        spec["plots"] = [p]
        assert client.put(f"/api/canvases/{slug}", json={"spec": spec}).status_code == 200
        rr = client.post(f"/api/canvases/{slug}/render", json={})
        assert rr.status_code == 200
        body = rr.json()
        assert "plot:p0" in {h["id"] for h in body["hotspots"]}
        assert base64.b64decode(body["img"])[:8] == b"\x89PNG\r\n\x1a\n"
    finally:
        client.delete(f"/api/canvases/{slug}")


# ------------------------------------------------------- duplicate then edit
def test_duplicate_then_edit_isolates_specs(client):
    slug = _create(client)
    copy = None
    from figfoundry import canvas
    spec = canvas.blank_spec()
    spec["plots"] = [canvas._new_plot("p0", [0.1, 0.15, 0.8, 0.7], "@x", "@y")]
    try:
        assert client.put(f"/api/canvases/{slug}", json={"spec": spec}).status_code == 200
        r = client.post(f"/api/canvases/{slug}/duplicate")
        assert r.status_code == 200
        copy = r.json()["slug"]
        # edit the copy only: add a second plot
        spec2 = client.get(f"/api/canvases/{copy}").json()["spec"]
        spec2["plots"] = spec2["plots"] + [canvas._new_plot("p1", [0.1, 0.55, 0.8, 0.3], "@x", "@y")]
        assert client.put(f"/api/canvases/{copy}", json={"spec": spec2}).status_code == 200
        assert len(client.get(f"/api/canvases/{slug}").json()["spec"]["plots"]) == 1  # original untouched
        got = client.get(f"/api/canvases/{copy}").json()["spec"]
        assert [q["id"] for q in got["plots"]] == ["p0", "p1"]
        assert "# ---- plot p1 ----" in client.get(f"/api/canvases/{copy}/code").json()["code"]
    finally:
        client.delete(f"/api/canvases/{slug}")
        if copy:
            client.delete(f"/api/canvases/{copy}")


# ------------------------------------------------------- list + download
def test_canvas_list_and_download(client):
    import os

    from figfoundry import app as app_module
    s1 = _create(client)
    s2 = _create(client)
    try:
        r = client.get("/api/canvases")
        assert r.status_code == 200
        rows = {c["slug"]: c for c in r.json()["canvases"]}
        assert s1 in rows and s2 in rows
        assert rows[s1]["name"] == "Untitled" and rows[s1]["plots"] == 0
        d = client.get(f"/api/canvases/{s1}/download")
        assert d.status_code == 200
        assert "text/x-python" in d.headers["content-type"]
        assert 'filename="Untitled.py"' in d.headers["content-disposition"]
        assert "def build():" in d.text
    finally:
        client.delete(f"/api/canvases/{s1}")
        client.delete(f"/api/canvases/{s2}")
        # download writes canvas_<slug>.py into the (real) OUT_DIR; clean it up
        p = os.path.join(app_module.OUT_DIR, f"canvas_{s1}.py")
        if os.path.exists(p):
            os.remove(p)


# ------------------------------------------------------- frontend pages
def test_serves_frontend_pages(client):
    """The wheel/sdist must ship figfoundry/static; these 200s prove the mount works."""
    for url in ["/", "/canvas", "/static/canvas.js", "/static/style.css", "/static/canvas.css"]:
        assert client.get(url).status_code == 200, url
    assert "Fig Foundry" in client.get("/").text

"""API-layer tests via FastAPI TestClient: slug validation, canvas lifecycle
(create/duplicate/delete), render/code/figure endpoints, palettes, and dataset
list/signals/upload. Canvas tests use the real user dir, so any canvas created is
deleted again; dataset tests run against a throwaway data dir (data_dir fixture)."""
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
    r = client.post("/api/canvases", json={"mode": "blank"})
    assert r.status_code == 200
    slug = r.json()["slug"]
    try:
        rr = client.post(f"/api/canvases/{slug}/render", json={})
        assert rr.status_code == 200
        body = rr.json()
        assert body["img"] and body["mime"] == "image/png"
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

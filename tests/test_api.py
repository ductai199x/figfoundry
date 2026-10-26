"""API-layer tests via FastAPI TestClient: slug validation, dataset signals, and a
blank-canvas render round-trip. These use the real user dir, so any canvas created is
deleted again."""
import pytest
from fastapi.testclient import TestClient

from figfoundry.app import app


@pytest.fixture
def client():
    return TestClient(app)


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

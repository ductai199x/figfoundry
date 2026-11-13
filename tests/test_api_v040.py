"""API additions in 0.4.0: warnings in the render / code / figure responses, drawn
plot bboxes in the render response, export dpi == canvas dpi, and canvas history
(automatic snapshot per save + list / get / restore)."""
import json

import pytest
from fastapi.testclient import TestClient

from figfoundry import canvas
from figfoundry.app import app


@pytest.fixture
def client():
    return TestClient(app)


def _create(client, spec=None, name=None):
    slug = client.post("/api/canvases", json={"mode": "blank"}).json()["slug"]
    if spec is not None or name is not None:
        body = {}
        if spec is not None:
            body["spec"] = spec
        if name is not None:
            body["name"] = name
        assert client.put(f"/api/canvases/{slug}", json=body).status_code == 200
    return slug


def _line_spec(**style):
    spec = canvas.blank_spec()
    spec["dataset"] = "sample.npz"
    p = canvas._new_plot("p0", [0.1, 0.15, 0.8, 0.7], "@x", "@y")
    p["style"].update(style)
    spec["plots"] = [p]
    return spec


# ------------------------------------------------------------------ warnings
def test_render_code_and_figure_report_warnings(client, data_dir):
    spec = _line_spec()
    spec["plots"][0]["artists"] = [{"type": "sparkle"}]
    slug = _create(client, spec)
    r = client.post(f"/api/canvases/{slug}/render", json={})
    assert r.status_code == 200
    assert any("sparkle" in w for w in r.json()["warnings"])
    c = client.get(f"/api/canvases/{slug}/code").json()
    assert any("sparkle" in w for w in c["warnings"]) and "# WARNING (figfoundry)" in c["code"]
    f = client.get(f"/api/canvases/{slug}/figure", params={"format": "pdf"})
    assert f.status_code == 200
    assert any("sparkle" in w for w in json.loads(f.headers["x-figfoundry-warnings"]))


def test_clean_canvas_has_empty_warnings(client, data_dir):
    slug = _create(client, _line_spec())
    assert client.post(f"/api/canvases/{slug}/render", json={}).json()["warnings"] == []
    assert json.loads(client.get(f"/api/canvases/{slug}/figure").headers["x-figfoundry-warnings"]) == []


def test_render_returns_plot_bboxes(client, data_dir):
    slug = _create(client, _line_spec())
    body = client.post(f"/api/canvases/{slug}/render", json={}).json()
    box = body["plot_bboxes"]["p0"]
    assert box["axes"] == pytest.approx([0.1, 0.15, 0.8, 0.7], abs=1e-4)
    assert len(box["tight"]) == 4


def test_figure_export_uses_canvas_dpi(client, data_dir):
    import io

    import matplotlib.pyplot as plt

    spec = _line_spec()
    spec["canvas"].update({"width": 2.0, "height": 1.0, "dpi": 110})
    slug = _create(client, spec)
    r = client.get(f"/api/canvases/{slug}/figure", params={"format": "png"})
    assert r.headers["x-figfoundry-dpi"] == "110"
    assert plt.imread(io.BytesIO(r.content)).shape[:2] == (110, 220)


# ------------------------------------------------------------------ history
def test_history_snapshot_per_save_and_restore(client, data_dir):
    slug = _create(client)
    assert client.get(f"/api/canvases/{slug}/history").json()["history"] == []
    v1 = _line_spec(color="#111111")
    v2 = _line_spec(color="#222222")
    client.put(f"/api/canvases/{slug}", json={"spec": v1, "name": "first"})
    client.put(f"/api/canvases/{slug}", json={"spec": v2, "name": "second"})
    client.put(f"/api/canvases/{slug}", json={"spec": v2, "name": "second"})  # no-op: no snapshot
    hist = client.get(f"/api/canvases/{slug}/history").json()["history"]
    assert len(hist) == 2  # blank -> v1, v1 -> v2
    newest = hist[0]
    snap = client.get(f"/api/canvases/{slug}/history/{newest['id']}").json()
    assert snap["name"] == "first" and snap["spec"]["plots"][0]["style"]["color"] == "#111111"
    r = client.post(f"/api/canvases/{slug}/restore", json={"id": newest["id"]})
    assert r.status_code == 200 and r.json()["restored"] == newest["id"]
    cur = client.get(f"/api/canvases/{slug}").json()
    assert cur["name"] == "first" and cur["spec"]["plots"][0]["style"]["color"] == "#111111"
    # the restore itself snapshotted v2, so it can be undone
    hist2 = client.get(f"/api/canvases/{slug}/history").json()["history"]
    assert len(hist2) == 3
    undo = client.get(f"/api/canvases/{slug}/history/{hist2[0]['id']}").json()
    assert undo["spec"]["plots"][0]["style"]["color"] == "#222222"


def test_history_rejects_bad_ids(client):
    slug = _create(client)
    assert client.get(f"/api/canvases/{slug}/history/..%2F..%2Fx").status_code == 404
    assert client.get(f"/api/canvases/{slug}/history/20200101-000000-000000").status_code == 404
    assert client.post(f"/api/canvases/{slug}/restore", json={"id": "nope/../x"}).status_code == 404
    assert client.get("/api/canvases/missing/history").status_code == 404


def test_history_is_capped(client, monkeypatch):
    from figfoundry import app as app_module

    monkeypatch.setattr(app_module, "HISTORY_KEEP", 3)
    slug = _create(client)
    for i in range(6):
        client.put(f"/api/canvases/{slug}", json={"name": f"n{i}"})
    hist = client.get(f"/api/canvases/{slug}/history").json()["history"]
    assert [h["name"] for h in hist] == ["n4", "n3", "n2"]


def test_history_dir_not_listed_as_canvas(client):
    slug = _create(client)
    client.put(f"/api/canvases/{slug}", json={"name": "x"})
    slugs = [c["slug"] for c in client.get("/api/canvases").json()["canvases"]]
    assert slugs == [slug]


def test_put_preserves_unknown_fields(client):
    """The server stores the spec verbatim (the editor relies on this too), so
    fields a version does not know survive a save."""
    slug = _create(client)
    spec = canvas.blank_spec()
    spec["future_field"] = {"keep": True}
    spec["figure_artists"] = [{"type": "text", "x": 0.5, "y": 0.5, "text": "t", "tag": "keep-me"}]
    client.put(f"/api/canvases/{slug}", json={"spec": spec})
    got = client.get(f"/api/canvases/{slug}").json()["spec"]
    assert got["future_field"] == {"keep": True} and got["figure_artists"][0]["tag"] == "keep-me"

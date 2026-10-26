"""Regression tests for data_io edge cases and app endpoints: the SafeUnpickler
numpy allowlist (exact names, not a module prefix), _numeric_dict filtering, CSV
quirks (empty file, BOM, duplicate headers), npz zip entries that are not arrays,
slug validation on DELETE, upload validation-before-write, and the export
Content-Disposition header."""
import io
import os
import pickle
import zipfile

import numpy as np
import pytest
from fastapi.testclient import TestClient

from figfoundry import app as app_module
from figfoundry import data_io
from figfoundry.data_io import (
    SafeUnpickler,
    _csv_to_arrays,
    _numeric_dict,
    load_arrays,
    load_arrays_bytes,
)


def _dump(obj):
    buf = io.BytesIO()
    pickle.dump(obj, buf)
    buf.seek(0)
    return buf


# ------------------------------------------------------------------ SafeUnpickler

def test_unpickler_blocks_numpy_file_readers():
    # A pickle passes whitelisted attributes as the callable its reducer invokes, so
    # the numpy allowlist must be exact names: np.fromfile / np.load would read files.
    for evil in [(np.fromfile, ("C:/WINDOWS/system.ini", "S16")), (np.load, ("x.npz",))]:
        with pytest.raises(pickle.UnpicklingError):
            SafeUnpickler(_dump(evil)).load()


def test_unpickler_blocks_non_container_builtins():
    with pytest.raises(pickle.UnpicklingError):
        SafeUnpickler(_dump((eval, ("1+1",)))).load()


def test_unpickler_still_loads_real_datasets():
    good = {"x": np.arange(5.0), "M": np.ones((3, 4)), "n": np.float64(2.5), "tags": {1, 2}}
    data = SafeUnpickler(_dump(good)).load()
    assert data["x"].shape == (5,)
    assert data["M"].shape == (3, 4)
    assert float(data["n"]) == 2.5
    assert data["tags"] == {1, 2}


# ------------------------------------------------------------------ _numeric_dict

def test_numeric_dict_drops_text_zero_dim_and_metadata():
    out = _numeric_dict({
        "__meta": np.arange(3),            # dunder metadata
        "txt": np.array(["a", "b"]),       # text, not numbers
        "n": 5,                            # 0-d scalar
        "empty": np.array([]),             # empty
        "obj": [object(), object()],       # object dtype
        "ok": np.arange(3.0),
    })
    assert set(out) == {"ok"}


def test_numeric_dict_skips_ragged_values():
    # A single ragged entry must not make the whole dataset unloadable.
    out = _numeric_dict({"ragged": [[1, 2], [3]], "ok": np.arange(3.0)})
    assert set(out) == {"ok"}


# ------------------------------------------------------------------ CSV

def test_csv_duplicate_headers_do_not_overwrite():
    out = _csv_to_arrays([["a", "a"], ["1", "2"], ["3", "4"]])
    assert set(out) == {"a", "a_2"}
    assert out["a"].tolist() == [1.0, 3.0]
    assert out["a_2"].tolist() == [2.0, 4.0]


def test_csv_empty_file_and_bom(tmp_path):
    p = tmp_path / "e.csv"
    p.write_bytes(b"")
    assert load_arrays(str(p)) == ({}, "csv")
    q = tmp_path / "b.csv"
    q.write_bytes(b"\xef\xbb\xbfname,val\n1,2\n")
    arrays, _ = load_arrays(str(q))
    assert set(arrays) == {"name", "val"}  # BOM must not stick to the first header


def test_npz_ignores_non_array_zip_entries(tmp_path):
    p = tmp_path / "d.npz"
    with zipfile.ZipFile(p, "w") as z:
        buf = io.BytesIO()
        np.save(buf, np.arange(4.0))
        z.writestr("a.npy", buf.getvalue())
        z.writestr("note.txt", "not an array")
    arrays, kind = load_arrays(str(p))
    assert kind == "npz"
    assert set(arrays) == {"a"}


def test_load_arrays_bytes_matches_file_load(tmp_path):
    p = tmp_path / "d.npz"
    np.savez(p, a=np.arange(4.0))
    arrays, kind = load_arrays_bytes(p.read_bytes(), "d.npz")
    assert kind == "npz" and set(arrays) == {"a"}
    with pytest.raises((zipfile.BadZipFile, ValueError)):
        load_arrays_bytes(b"not a zip", "d.npz")


# ------------------------------------------------------------------ app: DELETE slug

def test_delete_canvas_rejects_traversal_slug(tmp_path, monkeypatch):
    # DELETE was the one endpoint without slug validation: a ..%2F-encoded slug
    # could remove a .json file up to two levels above the canvases dir.
    canvases = tmp_path / "canvases"
    canvases.mkdir()
    sentinel = tmp_path / "outside.json"
    sentinel.write_text("{}")
    monkeypatch.setattr(app_module, "CANVAS_DIR", str(canvases))
    client = TestClient(app_module.app)
    r = client.delete("/api/canvases/..%2Foutside")
    assert r.status_code == 404
    assert sentinel.exists()


# ------------------------------------------------------------------ app: upload

def test_upload_rejects_evil_pkl_and_preserves_existing(data_dir):
    client = TestClient(app_module.app)
    d = os.path.join(data_dir, "d.npz")
    np.savez(d, a=np.arange(4.0))
    # 1) a malicious pkl is rejected and leaves no file behind
    evil = io.BytesIO()
    pickle.dump((np.fromfile, (d, "S16")), evil)
    r = client.post("/api/datasets", files={"file": ("evil.pkl", evil.getvalue(), "application/octet-stream")})
    assert r.status_code == 400
    assert not os.path.exists(os.path.join(data_dir, "evil.pkl"))
    # 2) a corrupt re-upload of an existing name must not clobber the good file
    r = client.post("/api/datasets", files={"file": ("d.npz", b"not a zip", "application/octet-stream")})
    assert r.status_code == 400
    arrays, kind = data_io.load_arrays(d)
    assert kind == "npz" and set(arrays) == {"a"}
    # 3) a good upload still works
    good = io.BytesIO()
    np.savez(good, b=np.arange(3.0))
    r = client.post("/api/datasets", files={"file": ("g.npz", good.getvalue(), "application/octet-stream")})
    assert r.status_code == 200 and r.json()["n_signals"] == 1


# ------------------------------------------------------------------ app: export header

def test_export_figure_filename_header_is_safe():
    # The canvas name is client-controlled; quotes must not reach the hand-built
    # Content-Disposition header. (Uses the real user dir, like test_api.py.)
    client = TestClient(app_module.app)
    slug = client.post("/api/canvases", json={}).json()["slug"]
    try:
        assert client.put(f"/api/canvases/{slug}", json={"name": 'my "fig"'}).status_code == 200
        r = client.get(f"/api/canvases/{slug}/figure", params={"format": "png"})
        assert r.status_code == 200
        assert r.headers["content-disposition"] == 'attachment; filename="my__fig_.png"'
    finally:
        client.delete(f"/api/canvases/{slug}")

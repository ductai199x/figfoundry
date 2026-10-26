"""Regression tests for data_io edge cases and app endpoints: the SafeUnpickler
numpy allowlist (exact names, not a module prefix), _numeric_dict filtering, CSV
quirks (empty file, BOM, duplicate headers), npz zip entries that are not arrays,
slug validation on DELETE, upload validation-before-write, and the export
Content-Disposition header.

Round 2: adversarial SafeUnpickler bypass attempts (RCE vectors, __reduce__
chains, evil __setstate__ on builtin subclasses, protocol 5), corrupt/malformed
canvas JSON failing cleanly, list_datasets parse caching, same-name upload
invalidating the dataset cache, atomic concurrent uploads, and the download
Content-Disposition header."""
import copyreg
import io
import json
import os
import pickle
import tempfile
import threading
import zipfile

import numpy as np
import pytest
from fastapi.testclient import TestClient

from figfoundry import app as app_module
from figfoundry import canvas, data_io
from figfoundry.data_io import (
    SafeUnpickler,
    _csv_to_arrays,
    _numeric_dict,
    load_arrays,
    load_arrays_bytes,
)


@pytest.fixture(autouse=True)
def _clear_module_caches():
    # app._ds_list_cache and canvas._ds_cache are process-global; keep tests
    # independent of each other (and of a real user dir) by clearing them.
    canvas._ds_cache.clear()
    app_module._ds_list_cache.clear()
    yield
    canvas._ds_cache.clear()
    app_module._ds_list_cache.clear()


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


# ------------------------------------------------------------------ SafeUnpickler: adversarial bypass attempts

@pytest.mark.parametrize("evil", [
    (os.system, ("cmd /c echo pwned",)),          # RCE via direct global
    (open, ("C:/WINDOWS/system.ini",)),           # file read via _io.open
    (__import__, ("os",)),                        # import via builtin
    (np.frombuffer, (b"\x00\x01", "f8")),         # numpy callable outside allowlist
    (np.str_, ("x",)),                            # numpy type outside allowlist
    (np.lib.format.write_array, (io.BytesIO(), np.arange(3))),  # numpy.lib.format object
    (copyreg._reconstructor, (object, object, None)),            # copyreg chain
])
def test_unpickler_blocks_rce_file_and_import_vectors(evil):
    with pytest.raises(pickle.UnpicklingError):
        SafeUnpickler(_dump(evil)).load()


def test_unpickler_blocks_custom_reduce_to_file_reader():
    # A class whose __reduce__ hands the unpickler a numpy callable as the
    # reconstruction function must be blocked by the exact-name allowlist.
    class Evil:
        def __reduce__(self):
            return (np.fromfile, ("C:/WINDOWS/system.ini", "S16"))
    with pytest.raises(pickle.UnpicklingError):
        SafeUnpickler(_dump(Evil())).load()


class _EvilDict(dict):
    """Module-level (pickle needs a resolvable global). If it ever unpickles, its
    __setstate__ writes the marker file the test asserts on."""

    def __setstate__(self, state):
        with open(os.path.join(tempfile.gettempdir(), "ff_pwned_marker"), "w") as f:
            f.write("pwned")


def test_unpickler_blocks_evil_setstate_on_builtin_subclass():
    # Protocol 2/4 NEWOBJ+BUILD on a dict subclass: the class global is not
    # allowlisted, so the evil __setstate__ can never run.
    marker = os.path.join(tempfile.gettempdir(), "ff_pwned_marker")
    if os.path.exists(marker):
        os.remove(marker)
    for proto in (2, 4):
        buf = io.BytesIO()
        pickle.dump(_EvilDict(), buf, protocol=proto)
        buf.seek(0)
        with pytest.raises(pickle.UnpicklingError):
            SafeUnpickler(buf).load()
    assert not os.path.exists(marker)


def test_unpickler_blocks_callables_smuggled_in_containers():
    # A callable inside an allowed container (set/tuple/object array) is still a
    # GLOBAL opcode on load, so it is blocked before the container is built.
    for evil in ({os.system}, (os.system,), np.array([os.system], dtype=object)):
        with pytest.raises(pickle.UnpicklingError):
            SafeUnpickler(_dump(evil)).load()


def test_pkl_with_text_arrays_drops_text(tmp_path):
    p = tmp_path / "t.pkl"
    with open(p, "wb") as f:
        pickle.dump({"nums": np.arange(4.0), "labels": np.array(["a", "b"])}, f)
    arrays, kind = load_arrays(str(p))
    assert kind == "pkl" and set(arrays) == {"nums"}


# ------------------------------------------------------------------ app: corrupt / malformed canvas JSON

def _patch_canvas_dir(tmp_path, monkeypatch):
    canvases = tmp_path / "canvases"
    canvases.mkdir()
    monkeypatch.setattr(app_module, "CANVAS_DIR", str(canvases))
    return canvases, TestClient(app_module.app, raise_server_exceptions=False)


def test_corrupt_canvas_json_fails_cleanly_on_all_endpoints(tmp_path, monkeypatch):
    # A truncated JSON file must yield clean JSON errors, never a 500 traceback,
    # on every endpoint that reads a canvas.
    canvases, client = _patch_canvas_dir(tmp_path, monkeypatch)
    (canvases / "corrupt.json").write_text('{"name": "x", "spec": {"plots": [')
    for method, url in [
        ("GET", "/api/canvases/corrupt"),
        ("GET", "/api/canvases/corrupt/signals"),
        ("GET", "/api/canvases/corrupt/code"),
        ("GET", "/api/canvases/corrupt/download"),
        ("GET", "/api/canvases/corrupt/figure"),
        ("POST", "/api/canvases/corrupt/render"),
        ("PUT", "/api/canvases/corrupt"),
        ("POST", "/api/canvases/corrupt/duplicate"),
    ]:
        r = client.request(method, url, json={} if method in ("PUT", "POST") else None)
        assert r.status_code == 500, (method, url, r.status_code, r.text)
        assert "corrupt" in r.json()["detail"], (method, url, r.text)


def test_canvas_with_non_dict_spec_fails_cleanly(tmp_path, monkeypatch):
    canvases, client = _patch_canvas_dir(tmp_path, monkeypatch)
    (canvases / "listspec.json").write_text(json.dumps({"name": "a", "spec": [1, 2]}))
    for method, url in [
        ("GET", "/api/canvases/listspec"),
        ("GET", "/api/canvases/listspec/code"),
        ("POST", "/api/canvases/listspec/render"),
    ]:
        r = client.request(method, url, json={} if method == "POST" else None)
        assert r.status_code == 500, (method, url, r.text)
        assert "corrupt" in r.json()["detail"], (method, url, r.text)


def test_canvas_with_malformed_plot_degrades_cleanly(data_dir):
    # A single plot entry with the wrong shape (a string, not a dict) must not
    # sink the whole figure: canvas.py blanks just that plot, so the endpoints
    # succeed. (Uses the real user dir, cleaned up after.)
    client = TestClient(app_module.app)
    slug = client.post("/api/canvases", json={}).json()["slug"]
    try:
        spec = canvas.blank_spec()
        spec["plots"] = ["not a dict"]
        assert client.put(f"/api/canvases/{slug}", json={"spec": spec}).status_code == 200
        r = client.get(f"/api/canvases/{slug}/code")
        assert r.status_code == 200
        assert "skipped (invalid spec)" in r.json()["code"]
        r = client.post(f"/api/canvases/{slug}/render", json={})
        assert r.status_code == 200
    finally:
        client.delete(f"/api/canvases/{slug}")


def test_canvas_spec_unrecoverable_shape_returns_400(data_dir):
    # A non-string dataset ref can't be degraded per-plot (dataset loading happens
    # once, outside the plot loop): codegen/render must 400 cleanly, not 500.
    # (A missing "canvas" section, by contrast, falls back to blanks -> 200.)
    client = TestClient(app_module.app)
    slug = client.post("/api/canvases", json={}).json()["slug"]
    try:
        spec = canvas.blank_spec()
        spec["dataset"] = 42
        assert client.put(f"/api/canvases/{slug}", json={"spec": spec}).status_code == 200
        for method, url in [
            ("GET", f"/api/canvases/{slug}/code"),
            ("GET", f"/api/canvases/{slug}/download"),
            ("GET", f"/api/canvases/{slug}/figure"),
            ("POST", f"/api/canvases/{slug}/render"),
        ]:
            r = client.request(method, url, json={} if method == "POST" else None)
            assert r.status_code == 400, (method, url, r.status_code, r.text)
    finally:
        client.delete(f"/api/canvases/{slug}")


def test_canvas_signals_missing_dataset_returns_400(data_dir):
    # Dataset referenced by the spec but absent on disk: clean 400, not a 500.
    client = TestClient(app_module.app)
    slug = client.post("/api/canvases", json={}).json()["slug"]
    try:
        spec = canvas.blank_spec()
        spec["dataset"] = "nope.npz"
        assert client.put(f"/api/canvases/{slug}", json={"spec": spec}).status_code == 200
        r = client.get(f"/api/canvases/{slug}/signals")
        assert r.status_code == 400
    finally:
        client.delete(f"/api/canvases/{slug}")


# ------------------------------------------------------------------ app: dataset listing cache

def test_list_datasets_caches_parsing(data_dir, monkeypatch):
    calls = {"n": 0}
    orig = data_io.load_arrays

    def counting(path):
        calls["n"] += 1
        return orig(path)

    monkeypatch.setattr(data_io, "load_arrays", counting)
    client = TestClient(app_module.app)
    r1 = client.get("/api/datasets")
    n1 = calls["n"]
    assert n1 >= 1
    r2 = client.get("/api/datasets")
    assert calls["n"] == n1, "second listing must be served from the (mtime,size) cache"
    assert r1.json() == r2.json()
    # a new upload appears on the next listing, and only the new file is parsed
    buf = io.BytesIO()
    np.savez(buf, z=np.arange(7.0))
    assert client.post("/api/datasets", files={"file": ("new.npz", buf.getvalue(), "application/octet-stream")}).status_code == 200
    r3 = client.get("/api/datasets")
    assert "new.npz" in {d["name"] for d in r3.json()["datasets"]}
    assert calls["n"] == n1 + 1


def test_same_name_upload_invalidates_dataset_cache(data_dir):
    # canvas.get_dataset_arrays caches by filename; a same-name re-upload must
    # invalidate it, or the signal list keeps showing the old file's signals.
    client = TestClient(app_module.app)
    p = os.path.join(data_dir, "s.npz")
    np.savez(p, v=np.ones(4))
    slug = client.post("/api/canvases", json={}).json()["slug"]
    try:
        spec = canvas.blank_spec()
        spec["dataset"] = "s.npz"
        assert client.put(f"/api/canvases/{slug}", json={"spec": spec}).status_code == 200
        r = client.get(f"/api/canvases/{slug}/signals")
        assert [s["id"] for s in r.json()["signals"]] == ["v"]
        buf = io.BytesIO()
        np.savez(buf, w=np.arange(9.0))
        assert client.post("/api/datasets", files={"file": ("s.npz", buf.getvalue(), "application/octet-stream")}).status_code == 200
        r = client.get(f"/api/canvases/{slug}/signals")
        assert [s["id"] for s in r.json()["signals"]] == ["w"], "stale dataset cache served old signals"
    finally:
        client.delete(f"/api/canvases/{slug}")


# ------------------------------------------------------------------ app: concurrent uploads

def test_concurrent_same_name_uploads_leave_valid_file(data_dir):
    results = []

    def up(i):
        c = TestClient(app_module.app)
        buf = io.BytesIO()
        np.savez(buf, a=np.full(10, float(i)))
        r = c.post("/api/datasets", files={"file": ("race.npz", buf.getvalue(), "application/octet-stream")})
        results.append(r.status_code)

    threads = [threading.Thread(target=up, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [200, 200, 200, 200]
    z = np.load(os.path.join(data_dir, "race.npz"), allow_pickle=False)  # torn write would raise
    assert z["a"].shape == (10,)
    assert [f for f in os.listdir(data_dir) if ".tmp" in f] == []


def test_write_atomic_bytes_raises_and_cleans(tmp_path):
    # Normal path works and leaves no temp behind; an OS-illegal path (newline on
    # Windows) raises OSError so the upload endpoint can turn it into a clean 400.
    p = str(tmp_path / "x.npz")
    app_module._write_atomic_bytes(p, b"hello")
    with open(p, "rb") as f:
        assert f.read() == b"hello"
    assert [f for f in os.listdir(tmp_path) if ".tmp" in f] == []
    if os.name == "nt":
        with pytest.raises(OSError):
            app_module._write_atomic_bytes(str(tmp_path / "bad\nname.npz"), b"data")


# ------------------------------------------------------------------ app: download header + dataset name safety

def test_download_filename_header_is_safe():
    # The canvas name is client-controlled; control characters (CRLF) and quotes
    # must not reach the Content-Disposition header.
    client = TestClient(app_module.app)
    slug = client.post("/api/canvases", json={}).json()["slug"]
    try:
        assert client.put(f"/api/canvases/{slug}", json={"name": 'evil\nX: 1 "q"'}).status_code == 200
        r = client.get(f"/api/canvases/{slug}/download")
        assert r.status_code == 200
        cd = r.headers["content-disposition"]
        assert all(ch >= " " or ch == "\t" for ch in cd), repr(cd)
        assert cd == 'attachment; filename="evil_X__1__q_.py"', repr(cd)
    finally:
        client.delete(f"/api/canvases/{slug}")


def test_dataset_name_with_newline_is_rejected_cleanly(data_dir):
    client = TestClient(app_module.app)
    r = client.get("/api/datasets/bad%0Aname.npz/signals")
    assert r.status_code == 404, r.text  # newline is not a path separator: no such file
    r = client.get("/api/datasets/..%2F..%2Fetc.npz/signals")
    # %2F is rejected at the routing layer (404) on modern starlette, or by the
    # basename check (400) on older versions: either way, never a traversal.
    assert r.status_code in (400, 404), r.text

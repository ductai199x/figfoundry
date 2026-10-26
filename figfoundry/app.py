"""Fig Foundry -- FastAPI backend.

The spec (a scene graph of the figure) is the single source of truth. The browser
manipulates it; the backend renders it by EXECUTING the generated code (so the
preview is byte-identical to the export); and it can serialize the spec to a
standalone .py that references the dataset.
"""
import contextlib
import json
import os
import re
import threading
import time

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import canvas, data_io

HERE = os.path.dirname(os.path.abspath(__file__))
USER_DIR = data_io.USER_DIR
CANVAS_DIR = os.path.join(USER_DIR, "canvases")
OUT_DIR = os.path.join(USER_DIR, "out")
os.makedirs(CANVAS_DIR, exist_ok=True)
os.makedirs(OUT_DIR, exist_ok=True)

app = FastAPI(title="Fig Foundry")


@app.middleware("http")
async def no_cache(request, call_next):
    """Local tool: never let the browser serve stale JS/HTML/CSS after an update."""
    resp = await call_next(request)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


class CanvasBody(BaseModel):
    spec: dict | None = None
    draft: bool = False


class NewCanvasBody(BaseModel):
    mode: str = "blank"


class UpdateCanvasBody(BaseModel):
    name: str | None = None
    spec: dict | None = None


def _cpath(slug):
    return os.path.join(CANVAS_DIR, f"{slug}.json")


def _check_slug(slug):
    # Slugs are generated as <timestamp>-<hex>; reject anything with path separators
    # (including %-decoded ones, e.g. "..%2F..%2Fetc") so a crafted slug can't
    # traverse out of the canvases dir.
    if not re.fullmatch(r"[A-Za-z0-9_-]+", slug):
        raise HTTPException(404, "canvas not found")


def _read(slug):
    _check_slug(slug)
    p = _cpath(slug)
    if not os.path.exists(p):
        raise HTTPException(404, "canvas not found")
    try:
        with open(p) as f:
            doc = json.load(f)
    except (OSError, ValueError) as e:  # ValueError covers json.JSONDecodeError
        raise HTTPException(500, "canvas file is corrupt") from e
    # Every endpoint below does doc["spec"] / doc["spec"].get(...); a partial or
    # hand-edited file without a dict spec would be a raw 500 KeyError/AttributeError.
    if not isinstance(doc, dict) or not isinstance(doc.get("spec"), dict):
        raise HTTPException(500, "canvas file is corrupt: missing spec")
    return doc


def _check_dataset_name(name):
    if os.path.basename(name) != name or name in (".", ".."):
        raise HTTPException(400, "invalid dataset name")


_write_lock = threading.Lock()


def _write(slug, doc):
    # Serialize writes (threadpool) and make each atomic (temp + rename) so a reader
    # never sees a partial/corrupt file. On Windows a concurrent reader's open handle
    # can briefly refuse the rename, so retry it; clean up the temp on any failure.
    with _write_lock:
        tmp = _cpath(slug) + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(doc, f, indent=2)
            last = None
            for _ in range(20):
                try:
                    os.replace(tmp, _cpath(slug))
                    last = None
                    break
                except PermissionError as e:
                    last = e
                    time.sleep(0.02)
            if last is not None:
                raise last
        except BaseException:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise


def _list_canvases():
    out = []
    for f in sorted(os.listdir(CANVAS_DIR), reverse=True):
        if not f.endswith(".json"):
            continue
        try:
            with open(os.path.join(CANVAS_DIR, f)) as fh:
                doc = json.load(fh)
            out.append({"slug": f[:-5], "name": doc.get("name", "Untitled"),
                        "plots": len(doc.get("spec", {}).get("plots", [])), "updated": doc.get("updated")})
        except Exception:
            continue
    return out


# --------------------------------------------------------------------------- pages
@app.get("/")
def index():
    return FileResponse(os.path.join(HERE, "static", "index.html"))


@app.get("/canvas")
def canvas_page():
    return FileResponse(os.path.join(HERE, "static", "canvas.html"))


# --------------------------------------------------------------------------- datasets
# The picker calls this on every open; re-parsing (unpickling!) each dataset file
# every time is pure waste for large files. Cache per-file summaries keyed by
# (mtime, size) -- a re-upload of the same name changes both, so it invalidates.
_ds_list_cache = {}


@app.get("/api/datasets")
def list_datasets():
    out = []
    for f in sorted(os.listdir(data_io.DATA_DIR)):
        if f.endswith((".npz", ".pkl", ".pickle", ".csv")):
            p = os.path.join(data_io.DATA_DIR, f)
            try:
                st = os.stat(p)
            except OSError:
                continue
            hit = _ds_list_cache.get(f)
            if hit is not None and hit[0] == st.st_mtime and hit[1] == st.st_size:
                out.append(hit[2])
                continue
            try:
                arrays, kind = data_io.load_arrays(p)
                info = {"name": f, "kind": kind, "n_signals": len(arrays)}
            except Exception as e:
                info = {"name": f, "kind": "?", "n_signals": 0, "error": str(e)}
            _ds_list_cache[f] = (st.st_mtime, st.st_size, info)
            out.append(info)
    return {"datasets": out}


@app.get("/api/datasets/{name}/signals")
def dataset_signals(name):
    """The signals in a specific dataset file (independent of any canvas)."""
    _check_dataset_name(name)
    path = os.path.join(data_io.DATA_DIR, name)
    if not os.path.exists(path):
        raise HTTPException(404, "dataset not found")
    try:
        arrays, kind = data_io.load_arrays(path)
    except Exception as e:
        raise HTTPException(400, f"could not load dataset: {e}") from e
    return {"dataset": name, "kind": kind, "signals": data_io.list_signals(arrays)}


@app.post("/api/datasets")
async def upload_dataset(file: UploadFile = File(...)):
    """Upload a .npz / .pkl / .csv dataset. pkl goes through the whitelist
    SafeUnpickler. The payload is validated before it is written, so a bad upload
    can never clobber an existing dataset of the same name."""
    name = os.path.basename(file.filename or "upload")
    if not name.endswith((".npz", ".pkl", ".pickle", ".csv")):
        raise HTTPException(400, "only .npz / .pkl / .csv datasets are supported")
    payload = await file.read()
    try:
        arrays, kind = data_io.load_arrays_bytes(payload, name)
    except Exception as e:
        raise HTTPException(400, f"could not load dataset: {e}") from e
    try:
        _write_atomic_bytes(os.path.join(data_io.DATA_DIR, name), payload)
    except OSError as e:  # e.g. a filename character the OS refuses (newline on Windows)
        raise HTTPException(400, f"could not save dataset: {e}") from e
    # Belt and braces: canvas.get_dataset_arrays already invalidates by
    # (mtime, size) on read, but dropping the entry here too guarantees the next
    # signal-list call re-stats and re-loads the freshly uploaded file.
    canvas._ds_cache.pop(name, None)
    return {"name": name, "kind": kind, "n_signals": len(arrays), "signals": data_io.list_signals(arrays)}


def _write_atomic_bytes(path, payload):
    """Atomic byte write (unique temp + rename) so a reader never sees a partial
    file and two concurrent uploads of the same name can't clobber each other's
    temp. The temp name is unique per thread: a shared "<path>.tmp" would let one
    thread's os.replace grab the other's in-progress write. On Windows a
    concurrent reader's handle can briefly refuse the rename, so retry it."""
    tmp = f"{path}.{threading.get_ident()}.tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(payload)
        last = None
        for _ in range(20):
            try:
                os.replace(tmp, path)
                last = None
                break
            except PermissionError as e:
                last = e
                time.sleep(0.02)
        if last is not None:
            raise last
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise


# --------------------------------------------------------------------------- canvases
@app.get("/api/canvases")
def list_canvases():
    return {"canvases": _list_canvases()}


@app.post("/api/canvases")
def new_canvas(body: NewCanvasBody):
    slug = time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(2).hex()
    spec = canvas.blank_spec()  # always blank; load a dataset to get signals
    _write(slug, {"name": "Untitled", "spec": spec, "updated": time.time()})
    return {"slug": slug, "name": "Untitled"}


@app.get("/api/canvases/{slug}")
def get_canvas(slug):
    doc = _read(slug)
    return {"slug": slug, "name": doc.get("name", "Untitled"), "spec": doc["spec"]}


@app.put("/api/canvases/{slug}")
def update_canvas(slug, body: UpdateCanvasBody):
    doc = _read(slug)
    if body.name is not None:
        doc["name"] = body.name
    if body.spec is not None:
        doc["spec"] = body.spec
    doc["updated"] = time.time()
    _write(slug, doc)
    return {"ok": True}


@app.delete("/api/canvases/{slug}")
def delete_canvas(slug):
    _check_slug(slug)
    p = _cpath(slug)
    if os.path.exists(p):
        os.remove(p)
    return {"ok": True}


@app.post("/api/canvases/{slug}/duplicate")
def duplicate_canvas(slug):
    doc = _read(slug)
    newslug = time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(2).hex()
    name = str(doc.get("name") or "Untitled")  # a hand-edited file may hold a non-str
    newdoc = {"name": name + " copy", "spec": json.loads(json.dumps(doc["spec"])), "updated": time.time()}
    _write(newslug, newdoc)
    return {"slug": newslug, "name": newdoc["name"]}


@app.get("/api/canvases/{slug}/signals")
def canvas_signals(slug):
    """The signal library for a canvas = the signals in its dataset (empty if none)."""
    doc = _read(slug)
    ds = doc["spec"].get("dataset")
    if not ds:
        return {"dataset": None, "signals": []}
    try:
        arrays = canvas.get_dataset_arrays(ds)
    except HTTPException:
        raise
    except Exception as e:  # dataset deleted from disk, or a non-str name in a hand-edited spec
        raise HTTPException(400, f"could not load dataset: {e}") from e
    return {"dataset": ds, "signals": data_io.list_signals(arrays)}


@app.post("/api/canvases/{slug}/render")
def render_canvas(slug, body: CanvasBody | None = None):
    spec = body.spec if (body is not None and body.spec is not None) else _read(slug)["spec"]
    draft = body.draft if body is not None else False
    try:
        return canvas.render_to_png(spec, draft=draft)
    except Exception as e:
        raise HTTPException(400, f"render failed: {e}") from e


@app.get("/api/canvases/{slug}/code")
def code_canvas(slug):
    try:
        code = canvas.codegen_canvas(_read(slug)["spec"])
    except HTTPException:
        raise  # _read's 404/500 must not be re-wrapped as a codegen failure
    except Exception as e:
        raise HTTPException(400, f"codegen failed: {e}") from e
    return JSONResponse({"code": code})


@app.get("/api/canvases/{slug}/download")
def download_canvas(slug):
    doc = _read(slug)
    try:
        code = canvas.codegen_canvas(doc["spec"])
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f"codegen failed: {e}") from e
    # Same filename-safe character set as export_figure: the name is client-
    # controlled, and FileResponse's Content-Disposition handling of it has
    # changed across starlette versions (older ones quoted it raw).
    base = str(doc.get("name") or "canvas").strip().replace(" ", "_") or "canvas"
    fname = re.sub(r"[^A-Za-z0-9._-]", "_", base) + ".py"
    path = os.path.join(OUT_DIR, f"canvas_{slug}.py")
    with open(path, "w") as f:
        f.write(code)
    return FileResponse(path, filename=fname, media_type="text/x-python")


_FIG_CT = {"png": "image/png", "svg": "image/svg+xml", "pdf": "application/pdf"}


@app.get("/api/canvases/{slug}/figure")
def export_figure(slug, format: str = "png"):
    """Render the figure (by executing the code) and return it as png / svg / pdf.
    svg and pdf are vector, suitable for publication."""
    if format not in _FIG_CT:
        raise HTTPException(400, "format must be png, svg, or pdf")
    doc = _read(slug)
    try:
        data = canvas.render_to_bytes(doc["spec"], fmt=format)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f"render failed: {e}") from e
    base = str(doc.get("name") or "figure").strip().replace(" ", "_") or "figure"
    # The canvas name is client-controlled; keep the hand-built header value to
    # filename-safe characters so a quote/newline can't break Content-Disposition.
    fname = re.sub(r"[^A-Za-z0-9._-]", "_", base) + "." + format
    return Response(content=data, media_type=_FIG_CT[format],
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@app.get("/api/palettes")
def palettes():
    return {"palettes": canvas.get_palettes()}


app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")

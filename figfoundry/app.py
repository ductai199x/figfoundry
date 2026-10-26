"""Fig Foundry -- FastAPI backend.

The spec (a scene graph of the figure) is the single source of truth. The browser
manipulates it; the backend renders it by EXECUTING the generated code (so the
preview is byte-identical to the export); and it can serialize the spec to a
standalone .py that references the dataset.
"""
import json
import os
import re
import threading
import time

from fastapi import FastAPI, HTTPException, UploadFile, File
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


def _read(slug):
    # Slugs are generated as <timestamp>-<hex>; reject anything with path separators
    # so a crafted slug can't traverse out of the canvases dir.
    if not re.fullmatch(r"[A-Za-z0-9_-]+", slug):
        raise HTTPException(404, "canvas not found")
    p = _cpath(slug)
    if not os.path.exists(p):
        raise HTTPException(404, "canvas not found")
    return json.load(open(p))


def _check_dataset_name(name):
    if os.path.basename(name) != name or name in (".", ".."):
        raise HTTPException(400, "invalid dataset name")


_write_lock = threading.Lock()


def _write(slug, doc):
    # Serialize writes (threadpool) and make each atomic (temp + rename) so a reader
    # never sees a partial/corrupt file.
    with _write_lock:
        tmp = _cpath(slug) + ".tmp"
        json.dump(doc, open(tmp, "w"), indent=2)
        os.replace(tmp, _cpath(slug))


def _list_canvases():
    out = []
    for f in sorted(os.listdir(CANVAS_DIR), reverse=True):
        if not f.endswith(".json"):
            continue
        try:
            doc = json.load(open(os.path.join(CANVAS_DIR, f)))
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
@app.get("/api/datasets")
def list_datasets():
    out = []
    for f in sorted(os.listdir(data_io.DATA_DIR)):
        if f.endswith((".npz", ".pkl", ".pickle", ".csv")):
            try:
                arrays, kind = data_io.load_arrays(os.path.join(data_io.DATA_DIR, f))
                out.append({"name": f, "kind": kind, "n_signals": len(arrays)})
            except Exception as e:
                out.append({"name": f, "kind": "?", "n_signals": 0, "error": str(e)})
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
        raise HTTPException(400, f"could not load dataset: {e}")
    return {"dataset": name, "kind": kind, "signals": data_io.list_signals(arrays)}


@app.post("/api/datasets")
async def upload_dataset(file: UploadFile = File(...)):
    """Upload a .npz / .pkl dataset. pkl goes through the whitelist SafeUnpickler."""
    name = os.path.basename(file.filename or "upload")
    if not name.endswith((".npz", ".pkl", ".pickle", ".csv")):
        raise HTTPException(400, "only .npz / .pkl / .csv datasets are supported")
    dest = os.path.join(data_io.DATA_DIR, name)
    with open(dest, "wb") as f:
        f.write(await file.read())
    try:
        arrays, kind = data_io.load_arrays(dest)
    except Exception as e:
        os.remove(dest)
        raise HTTPException(400, f"could not load dataset: {e}")
    return {"name": name, "kind": kind, "n_signals": len(arrays), "signals": data_io.list_signals(arrays)}


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
    p = _cpath(slug)
    if os.path.exists(p):
        os.remove(p)
    return {"ok": True}


@app.post("/api/canvases/{slug}/duplicate")
def duplicate_canvas(slug):
    doc = _read(slug)
    newslug = time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(2).hex()
    newdoc = {"name": doc.get("name", "Untitled") + " copy", "spec": json.loads(json.dumps(doc["spec"])), "updated": time.time()}
    _write(newslug, newdoc)
    return {"slug": newslug, "name": newdoc["name"]}


@app.get("/api/canvases/{slug}/signals")
def canvas_signals(slug):
    """The signal library for a canvas = the signals in its dataset (empty if none)."""
    doc = _read(slug)
    ds = doc["spec"].get("dataset")
    if not ds:
        return {"dataset": None, "signals": []}
    arrays = canvas.get_dataset_arrays(ds)
    return {"dataset": ds, "signals": data_io.list_signals(arrays)}


@app.post("/api/canvases/{slug}/render")
def render_canvas(slug, body: CanvasBody | None = None):
    spec = body.spec if (body is not None and body.spec is not None) else _read(slug)["spec"]
    draft = body.draft if body is not None else False
    try:
        return canvas.render_to_png(spec, draft=draft)
    except Exception as e:
        raise HTTPException(400, f"render failed: {e}")


@app.get("/api/canvases/{slug}/code")
def code_canvas(slug):
    return JSONResponse({"code": canvas.codegen_canvas(_read(slug)["spec"])})


@app.get("/api/canvases/{slug}/download")
def download_canvas(slug):
    doc = _read(slug)
    fname = (doc.get("name", "canvas").strip().replace(" ", "_") or "canvas") + ".py"
    path = os.path.join(OUT_DIR, f"canvas_{slug}.py")
    open(path, "w").write(canvas.codegen_canvas(doc["spec"]))
    return FileResponse(path, filename=fname, media_type="text/x-python")


_FIG_CT = {"png": "image/png", "svg": "image/svg+xml", "pdf": "application/pdf"}


@app.get("/api/canvases/{slug}/figure")
def export_figure(slug, format: str = "png"):
    """Render the figure (by executing the code) and return it as png / svg / pdf.
    svg and pdf are vector, suitable for publication."""
    if format not in _FIG_CT:
        raise HTTPException(400, "format must be png, svg, or pdf")
    doc = _read(slug)
    data = canvas.render_to_bytes(doc["spec"], fmt=format)
    fname = (doc.get("name", "figure").strip().replace(" ", "_") or "figure") + "." + format
    return Response(content=data, media_type=_FIG_CT[format],
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@app.get("/api/palettes")
def palettes():
    return {"palettes": canvas.get_palettes()}


app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")

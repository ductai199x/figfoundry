"""Command-line interface.

`figfoundry` (no args) starts the web app. Subcommands let you drive the same
engines from the shell, for scripting and automation:

    figfoundry serve [--port 8037] [--host 127.0.0.1] [--no-browser]
    figfoundry list                          # list saved figures
    figfoundry new [--name NAME]             # create a blank figure, print its slug
    figfoundry export SLUG|spec.json [--py P] [--pdf P] [--png P] [--svg P]
                                     [--out-dir DIR] [--data FILE] [--dpi N]
                                             # headless export, no server needed
    figfoundry render SLUG|spec.json [--format png|svg|pdf] [--out PATH]
    figfoundry datasets                      # list available datasets
"""
import argparse
import json
import os
import re
import shutil
import sys
import time
import webbrowser

import uvicorn

from . import __version__, canvas, data_io
from .app import CANVAS_DIR, OUT_DIR, app


def _read_canvas(slug):
    p = os.path.join(CANVAS_DIR, f"{slug}.json")
    if not os.path.exists(p):
        raise SystemExit(f"canvas not found: {slug}")
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _load_source(src):
    """A saved canvas slug, or a path to a JSON file holding either a canvas
    document ({"name", "spec"}) or a bare spec. Returns (doc, data_dir): data_dir
    is the JSON file's folder when the spec's dataset sits next to it (a
    reproducibility bundle), else None (use ~/.figfoundry/datasets)."""
    if src.lower().endswith(".json") or os.path.sep in src or "/" in src:
        if not os.path.isfile(src):
            raise SystemExit(f"spec file not found: {src}")
        with open(src, encoding="utf-8") as f:
            obj = json.load(f)
        if not isinstance(obj, dict):
            raise SystemExit(f"not a spec: {src}")
        doc = obj if isinstance(obj.get("spec"), dict) else {"name": os.path.splitext(os.path.basename(src))[0], "spec": obj}
        here = os.path.dirname(os.path.abspath(src))
        ds = doc["spec"].get("dataset")
        data_dir = here if isinstance(ds, str) and os.path.isfile(os.path.join(here, ds)) else None
        return doc, data_dir
    return _read_canvas(src), None


def _resolve_data(doc, data_path, data_dir):
    """--data FILE overrides where the dataset is read from: the spec keeps its
    dataset name unless the file is named differently (then it is renamed in a copy)."""
    if not data_path:
        return doc, data_dir
    if not os.path.isfile(data_path):
        raise SystemExit(f"dataset not found: {data_path}")
    spec = dict(doc["spec"])
    spec["dataset"] = os.path.basename(data_path)
    return {**doc, "spec": spec}, os.path.dirname(os.path.abspath(data_path))


def _safe_name(doc, ext):
    base = (doc.get("name", "canvas").strip().replace(" ", "_") or "canvas") + ext
    return os.path.join(OUT_DIR, base)


def _base_name(doc):
    base = str(doc.get("name") or "figure").strip().replace(" ", "_") or "figure"
    return re.sub(r"[^A-Za-z0-9._-]", "_", base)


def _ensure_parent(path):
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)


def _warn(warnings):
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)


def cmd_serve(a):
    data_io.ensure_sample_npz()
    url = f"http://{a.host}:{a.port}"
    print(f"Fig Foundry v{__version__}")
    print(f"  running at {url}   (Ctrl+C to stop)")
    if not a.no_browser:
        webbrowser.open(url)
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")


def cmd_list(a):
    rows = []
    for f in sorted(os.listdir(CANVAS_DIR), reverse=True):
        if not f.endswith(".json"):
            continue
        try:
            with open(os.path.join(CANVAS_DIR, f)) as fh:
                d = json.load(fh)
            rows.append((f[:-5], d.get("name", "Untitled"), len(d.get("spec", {}).get("plots", []))))
        except Exception:
            continue
    if not rows:
        print("no figures yet")
        return
    for slug, name, n in rows:
        print(f"{slug}   {name}   ({n} plot{'s' if n != 1 else ''})")


def cmd_new(a):
    slug = time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(2).hex()
    os.makedirs(CANVAS_DIR, exist_ok=True)
    with open(os.path.join(CANVAS_DIR, f"{slug}.json"), "w") as f:
        json.dump({"name": a.name or "Untitled", "spec": canvas.blank_spec(), "updated": time.time()}, f, indent=2)
    print(slug)


def cmd_export(a):
    """Headless export (no server): any of --py / --pdf / --png / --svg, or
    --out-dir for a full reproducibility bundle. With none of them it writes the
    .py to ~/.figfoundry/out (or --out), as before."""
    doc, data_dir = _load_source(a.slug)
    doc, data_dir = _resolve_data(doc, getattr(a, "data", None), data_dir)
    spec = doc["spec"]
    py = getattr(a, "py", None) or a.out
    figs = {fmt: getattr(a, fmt, None) for fmt in ("pdf", "png", "svg")}
    out_dir = getattr(a, "out_dir", None)
    dpi = getattr(a, "dpi", None)
    if out_dir:
        # bundle: <name>.py + .pdf + .png + .json + the dataset copy, so the
        # script runs from that folder with no arguments
        base = os.path.join(out_dir, _base_name(doc))
        py = py or base + ".py"
        figs["pdf"] = figs["pdf"] or base + ".pdf"
        figs["png"] = figs["png"] or base + ".png"
        _ensure_parent(base + ".json")
        with open(base + ".json", "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2)
        print(base + ".json")
        ds = spec.get("dataset")
        if isinstance(ds, str) and ds:
            src = os.path.join(data_dir or data_io.DATA_DIR, ds)
            dst = os.path.join(out_dir, ds)
            if os.path.isfile(src) and os.path.abspath(src) != os.path.abspath(dst):
                shutil.copyfile(src, dst)
                print(dst)
    if not py and not any(figs.values()):
        py = _safe_name(doc, ".py")
    warned = False
    if py:
        code, warnings = canvas.codegen_canvas_full(spec)
        _ensure_parent(py)
        with open(py, "w", encoding="utf-8") as f:
            f.write(code)
        _warn(warnings)
        warned = True
        print(py)
    for fmt, path in figs.items():
        if not path:
            continue
        data, warnings = canvas.render_to_bytes_full(spec, fmt=fmt, dpi=dpi, data_dir=data_dir)
        _ensure_parent(path)
        with open(path, "wb") as f:
            f.write(data)
        if not warned:
            _warn(warnings)
            warned = True
        print(path)


def cmd_render(a):
    doc, data_dir = _load_source(a.slug)
    doc, data_dir = _resolve_data(doc, getattr(a, "data", None), data_dir)
    out = a.out or _safe_name(doc, "." + a.format)
    _ensure_parent(out)
    data, warnings = canvas.render_to_bytes_full(doc["spec"], fmt=a.format, dpi=getattr(a, "dpi", None),
                                                 data_dir=data_dir)
    with open(out, "wb") as f:
        f.write(data)
    _warn(warnings)
    print(out)


def cmd_datasets(a):
    data_io.ensure_sample_npz()
    for f in sorted(os.listdir(data_io.DATA_DIR)):
        if f.endswith((".npz", ".pkl", ".pickle", ".csv")):
            try:
                arrays, kind = data_io.load_arrays(os.path.join(data_io.DATA_DIR, f))
                print(f"{f}   ({kind}, {len(arrays)} signals)")
            except Exception as e:
                print(f"{f}   (error: {e})")


def main():
    ap = argparse.ArgumentParser(prog="figfoundry", description="Fig Foundry -- WYSIWYG matplotlib figure composer")
    ap.add_argument("--version", action="version", version=f"figfoundry {__version__}")
    sub = ap.add_subparsers(dest="cmd")

    s = sub.add_parser("serve", help="start the web app (default)")
    s.add_argument("--port", type=int, default=8037)
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--no-browser", action="store_true")
    s.set_defaults(func=cmd_serve)

    sub.add_parser("list", help="list saved figures").set_defaults(func=cmd_list)

    n = sub.add_parser("new", help="create a blank figure and print its slug")
    n.add_argument("--name", default=None)
    n.set_defaults(func=cmd_new)

    e = sub.add_parser("export", help="export a figure headlessly (.py / .pdf / .png / .svg, no server)",
                       description="Export a saved canvas (slug) or a spec .json file without a running server.")
    e.add_argument("slug", help="canvas slug, or a path to a spec/canvas .json file")
    e.add_argument("--py", default=None, help="write the standalone .py here")
    e.add_argument("--pdf", default=None, help="write the figure as PDF here")
    e.add_argument("--png", default=None, help="write the figure as PNG here")
    e.add_argument("--svg", default=None, help="write the figure as SVG here")
    e.add_argument("--out-dir", dest="out_dir", default=None,
                   help="write a bundle: <name>.py/.pdf/.png/.json plus a copy of the dataset")
    e.add_argument("--data", default=None, help="dataset file to use (default: next to the .json, else ~/.figfoundry/datasets)")
    e.add_argument("--dpi", type=int, default=None, help="raster dpi (default: the canvas dpi, same as the script)")
    e.add_argument("--out", default=None, help="(legacy) same as --py")
    e.set_defaults(func=cmd_export)

    r = sub.add_parser("render", help="render a figure to png/svg/pdf")
    r.add_argument("slug", help="canvas slug, or a path to a spec/canvas .json file")
    r.add_argument("--format", default="png", choices=["png", "svg", "pdf"])
    r.add_argument("--out", default=None)
    r.add_argument("--data", default=None, help="dataset file to use")
    r.add_argument("--dpi", type=int, default=None, help="raster dpi (default: the canvas dpi)")
    r.set_defaults(func=cmd_render)

    sub.add_parser("datasets", help="list available datasets").set_defaults(func=cmd_datasets)

    a = ap.parse_args()
    if not hasattr(a, "func"):  # bare `figfoundry` -> serve
        a = argparse.Namespace(port=8037, host="127.0.0.1", no_browser=False)
        a.func = cmd_serve
    a.func(a)


if __name__ == "__main__":
    main()

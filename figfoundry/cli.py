"""Command-line interface.

`figfoundry` (no args) starts the web app. Subcommands let you drive the same
engines from the shell, for scripting and automation:

    figfoundry serve [--port 8037] [--host 127.0.0.1] [--no-browser]
    figfoundry list                          # list saved figures
    figfoundry new [--name NAME]             # create a blank figure, print its slug
    figfoundry export SLUG [--out PATH]      # write a figure's .py
    figfoundry render SLUG [--format png|svg|pdf] [--out PATH]
    figfoundry datasets                      # list available datasets
"""
import argparse
import json
import os
import time
import webbrowser

import uvicorn

from . import __version__, canvas, data_io
from .app import app, CANVAS_DIR, OUT_DIR


def _read_canvas(slug):
    p = os.path.join(CANVAS_DIR, f"{slug}.json")
    if not os.path.exists(p):
        raise SystemExit(f"canvas not found: {slug}")
    with open(p) as f:
        return json.load(f)


def _safe_name(doc, ext):
    base = (doc.get("name", "canvas").strip().replace(" ", "_") or "canvas") + ext
    return os.path.join(OUT_DIR, base)


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
            d = json.load(open(os.path.join(CANVAS_DIR, f)))
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
    doc = _read_canvas(a.slug)
    out = a.out or _safe_name(doc, ".py")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        f.write(canvas.codegen_canvas(doc["spec"]))
    print(out)


def cmd_render(a):
    doc = _read_canvas(a.slug)
    out = a.out or _safe_name(doc, "." + a.format)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "wb") as f:
        f.write(canvas.render_to_bytes(doc["spec"], fmt=a.format))
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

    e = sub.add_parser("export", help="write a figure's generated .py")
    e.add_argument("slug")
    e.add_argument("--out", default=None)
    e.set_defaults(func=cmd_export)

    r = sub.add_parser("render", help="render a figure to png/svg/pdf")
    r.add_argument("slug")
    r.add_argument("--format", default="png", choices=["png", "svg", "pdf"])
    r.add_argument("--out", default=None)
    r.set_defaults(func=cmd_render)

    sub.add_parser("datasets", help="list available datasets").set_defaults(func=cmd_datasets)

    a = ap.parse_args()
    if not hasattr(a, "func"):  # bare `figfoundry` -> serve
        a = argparse.Namespace(port=8037, host="127.0.0.1", no_browser=False)
        a.func = cmd_serve
    a.func(a)


if __name__ == "__main__":
    main()

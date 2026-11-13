# Fig Foundry — guide for AI agents

Fig Foundry is a WYSIWYG matplotlib figure composer. A human drives it through the
browser; **an agent drives it through a small HTTP API that edits the same JSON spec the
browser uses.** This document is everything you need to load data, compose a figure,
render it, and export it — no GUI required.

## The one idea

A figure is a **spec**: a JSON scene with a canvas (size), a dataset (named signals), a
list of **plots** (each an explicit rectangle + data refs + style + axes + title +
artists), and a list of **figure-level artists** (text, suptitle, legend, line). The backend
renders a spec by **executing the Python it generates from it**, so whatever you render is
byte-for-byte what the export produces. There is no second renderer to drift from.

Nothing in a spec is dropped silently: unknown plot types, artist types and keys come back
as a `warnings` list (see [Warnings](#warnings)).

## No server needed: headless export

```
figfoundry export <slug | spec.json> --pdf fig.pdf --py fig.py [--png fig.png] [--svg fig.svg]
figfoundry export <slug | spec.json> --out-dir bundle/   # .py + .pdf + .png + .json + dataset copy
figfoundry render <slug | spec.json> --format pdf --out fig.pdf
```

The `.json` may be a saved canvas document (`{"name", "spec"}`) or a bare spec. If the spec's
dataset file sits next to the `.json`, it is read from there; otherwise from
`~/.figfoundry/datasets`. `--data FILE` points at any dataset, and `--dpi N` overrides the
raster dpi. Warnings print to stderr.

## Start the server

```
uv tool install figfoundry        # or: pip install figfoundry
figfoundry serve --port 8037 --no-browser
# → http://127.0.0.1:8037
```

Runtime state (datasets, canvases, exports) lives in `~/.figfoundry/`.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/datasets` | list datasets |
| POST | `/api/datasets` | upload a `.npz` / `.pkl` / `.csv` (multipart field `file`) |
| GET | `/api/datasets/{name}/signals` | signals in a dataset (name, length, shape, ndim) |
| POST | `/api/canvases` | create a blank canvas → `{slug}` |
| GET | `/api/canvases/{slug}` | get `{name, spec}` |
| PUT | `/api/canvases/{slug}` | save `{name?, spec?}` (the previous version is snapshotted first) |
| GET | `/api/canvases/{slug}/history` | saved versions, newest first → `{history: [{id, name, updated, plots}]}` |
| GET | `/api/canvases/{slug}/history/{id}` | one saved version → `{name, spec}` |
| POST | `/api/canvases/{slug}/restore` | restore `{id}` (the current version is snapshotted first, so it can be undone) |
| POST | `/api/canvases/{slug}/duplicate` | duplicate → new `{slug}` |
| DELETE | `/api/canvases/{slug}` | delete |
| POST | `/api/canvases/{slug}/render` | preview → `{img (base64), mime, hotspots, warnings, plot_bboxes}`; body may carry `{spec, draft}` |
| GET | `/api/canvases/{slug}/code` | the standalone `.py` → `{code, warnings}` |
| GET | `/api/canvases/{slug}/figure?format=png\|svg\|pdf` | the figure bytes at the canvas dpi; headers `X-Figfoundry-Warnings` (JSON list), `X-Figfoundry-Dpi` |
| GET | `/api/palettes` | color palettes (Okabe–Ito, …) |

## The spec

```jsonc
{
  "schema_version": 2,            // spec format version (a newer one than this figfoundry knows -> warning)
  "min_figfoundry": "0.4.0",      // optional: warn when the running figfoundry is older
  "dataset": "name.npz",          // null until a dataset is set
  "canvas":  { "width": 6.875, "height": 1.92, "dpi": 220, "bg": "#ffffff",
               "font_family": "DejaVu Sans", "font_size": 10 },
  "layout":  { "margin": 0.05, "gap": 0.04 },
  "plots":         [ /* <plot> … */ ],
  "figure_artists": [ /* <figartist> … */ ]
}
```

`dpi` is the export resolution: `GET /figure`, the CLI and the generated script's default
`--dpi` all use it, so the server export and a script run produce the same file. (The
interactive preview renders at a fixed 150 dpi for speed.)

### A plot

```jsonc
{
  "id": "p1",
  "tag": "heatmap",                         // optional free label (any plot or artist may carry "id"/"tag")
  "rect": [left, bottom, width, height],   // figure fractions (0..1), bottom-origin
  "type": "line",                           // 23 types (below)
  "data": { "x": "@x", "y": "@y", "z": null, "field": null, "band": null, "errorbar": null },
  "style": { "color": "#b03a2e", "lw": 1.8, "marker": "o", "ms": 3, "alpha": 1.0,
             "cmap": "viridis", "bins": 20, "colorbar": false,
             "aspect": "equal",                 // imshow/contour(f)/pcolormesh: "equal" | "auto" | number
             "vmin": 0.0, "vmax": 1.0, "norm": "linear",  // colormapped types; norm: linear | log | symlog
             "elev": 30, "azim": -60, "dist": 10 },
  "extra_lines": [ { "ref": "@y2", "color": "#8c8c8c", "marker": "^", "ms": 3, "label": "series" } ],
  "axes": { "xticks": "auto", "xticklabels": null, "yticks": "auto", "yticklabels": null,
            "xlabel": "", "ylabel": "", "xlim": null, "ylim": [0.55, 1.0],
            "xscale": "linear", "yscale": "linear",
            "spines": { "top": false, "right": false, "bottom": true, "left": true },
            "tick_labelsize": 8, "label_fontsize": 0, "labelbottom": true, "labelleft": true,
            "tick_params": { "bottom": true, "left": true, "length": 3.5, "direction": "out" } },
  "title": { "text": "", "x": 0.5, "y": 1.0, "pad": 2, "fontsize": 10,
             "weight": "normal", "color": "#000000", "visible": false, "rotation": 0 },
  "artists": [ /* <artist> … */ ]
}
```

- **Data refs** are `@signalName` (an index into the dataset); `null`/omitted = none. `x`
  may be `null` (plotted against the array index). `xlim`/`ylim` are `[lo, hi]` or `null`.
- **Slices:** `@M[0:8]`, `@M[8:]`, `@M[:, 2]`, `@M[::-1]` take a numpy basic slice of a
  signal, so one array can feed several panels (e.g. a heatmap split into two stacked plots).
- **Ticks:** `"xticks": "auto"` (or `null`) lets matplotlib choose; a list sets them;
  **an empty list `[]` hides them** (`ax.set_xticks([])`). Same for `yticks`.
  `axes.tick_params` controls the tick marks themselves (`bottom`/`top`/`left`/`right`
  visibility, `length`, `width`, `pad`, `direction` `in|out|inout`, optional `axis` `x|y`).
- **Color scale:** `vmin`/`vmax`/`norm` apply to `imshow`, `pcolormesh`, `contourf`,
  `contour`, `hexbin`, `hist2d` and `scatter-grouped`. Set the same `vmin`/`vmax` on two
  panels to keep their colors comparable.
- **`extra_lines`** overlay one or more extra series on the same plot (a second line, a
  baseline, …), each its own color/marker.
- **Mathtext:** any text field (title, label, text artist, `xticklabels`) accepts LaTeX
  mathtext — `$x_0$`, `$\alpha$`, `$v_\text{gen}$` — and it passes through to matplotlib
  untouched.

### Plot types
`line`, `step`, `scatter`, `scatter-grouped`, `bar`, `barh`, `histogram`, `boxplot`,
`violinplot`, `hist2d`, `hexbin`, `imshow`, `contourf`, `contour`, `pcolormesh`,
`quiver`, `streamplot`, `decision-boundary`, `pie`, `polar`, `scatter-3d`, `surface`,
`wireframe`.

### A per-plot artist
```jsonc
{ "type": "text", "x": 0.5, "y": 0.5, "text": "label", "ha": "center", "va": "center",
  "fontsize": 10, "color": "#000", "coords": "axes", "rotation": 0,
  "stroke": { "width": 2, "color": "#000" },   // optional outline (white text on pale cells)
  "id": "lbl-1", "tag": "labels" }             // optional, for finding it again
```
Other types: `vline` (`x`), `hline` (`y`), `annotation` (`xy`, `xytext`),
`arrow` (`x0,y0,x1,y1`), `box` (`x0,y0,w,h,fc,ec,lw,zorder`), `circle` (`x,y,r`),
`ellipse` (`x,y,w,h,angle`), `polygon` (`points`), `vspan` (`x0,x1`), `cell_marks` (below).
`rotation` (degrees) is honored on text, annotation, title, suptitle and figure text.

**Coordinates.** `text` and `box` take `"coords": "axes"` (0..1 panel fractions) or
`"coords": "data"` (axis data units). Without `coords`, a box is in panel fractions and a text
follows the older `axes_frac` flag. Other artists (`vline`, `circle`, …) are always data units.
On an `imshow` heatmap, cell (row `r`, col `c`) spans data `x ∈ [c-0.5, c+0.5]`,
`y ∈ [r-0.5, r+0.5]`, so `{"type": "box", "coords": "data", "x0": c-0.5, "y0": r-0.5, "w": 1, "h": 1}`
outlines one cell and stays on it when the plot is resized or re-gridded.

**Heatmap cell markers.** One artist marks many cells:
```jsonc
{ "type": "cell_marks", "cells": [[0, 2], [3, 5]],   // [row, col] pairs
  "marker": "box",          // "box" | "x" | "dot"
  "color": "#1f1f1f", "lw": 1.2, "inset": 0.08,      // inset: fraction of the cell left empty on each side
  "fc": "none", "alpha": 1.0, "zorder": 4, "tag": "published-k" }
```
Cells are in data coordinates (centered on integers for `imshow`, spanning `[c, c+1]` for
`pcolormesh`), so they follow the grid.

**Ids and tags.** Any plot, artist or figure artist may carry `"id"` and/or `"tag"`. They do
not change the drawing; they are kept through editor round-trips and written as comments in
the generated code, so you can find "the published-k boxes" by tag instead of by color.

### A figure-level artist
```jsonc
{ "type": "text",     "x": 0.5, "y": 0.02, "text": "x-axis", "ha": "center", "fontsize": 12, "color": "#000" }
{ "type": "suptitle", "text": "Title", "x": 0.5, "y": 1.0, "fontsize": 16, "color": "#000" }
{ "type": "legend",   "handles": [ { "color": "#b03a2e", "marker": "o", "lw": 1.8, "ms": 3 } ],
                      "labels": [ "STAMP" ], "loc": [0.5, 1.0], "ncol": 1, "frameon": false, "fontsize": 10 }
{ "type": "line",     "x0": 0.03, "y0": 0.53, "x1": 0.9, "y1": 0.53, "color": "#333", "lw": 0.8, "ls": "-" }
```
Figure artists use figure fractions (0..1, bottom-origin). To place one against a plot, use
the plot's drawn box from `plot_bboxes` (below), not its spec `rect`.

## What a render returns

`POST /render` (and the CLI) report two things besides the image:

- **`plot_bboxes`**: each plot's *drawn* geometry in figure fractions
  `{"p1": {"axes": [l, b, w, h], "tight": [l, b, w, h]}}`. `axes` is the actual axes box,
  which differs from the spec `rect` under `aspect: "equal"`; `tight` also includes the tick
  labels, axis labels and title.
- **`warnings`**: a list of strings (empty when all is well).

## Warnings

Rendering never fails because of one bad element; instead, everything that is not drawn is
reported:

- unknown plot types (drawn as a plain line), unknown artist and figure-artist types (not
  drawn), unknown keys (ignored), invalid values (`aspect`, `norm`, `cell_marks` marker,
  `coords`), and elements that are malformed and skipped;
- a spec whose `schema_version` is newer than this figfoundry supports, or whose
  `min_figfoundry` is newer than the running version;
- layout problems found after drawing (full renders and exports): tick labels that overlap,
  and plots (incl. tick labels) or texts that extend past the canvas edge.

They are returned in the render response, the `/code` response, the `X-Figfoundry-Warnings`
header of `/figure`, on stderr from the CLI, and as `# WARNING (figfoundry)` comments at the
top of the generated code.

## Worked example: a 2×2 line figure from a dataset

```python
import json, base64, urllib.request
B = "http://127.0.0.1:8037"
def call(p, data=None, m="GET"):
    r = urllib.request.Request(B + p,
        data=(json.dumps(data).encode() if data is not None else None),
        headers={"Content-Type": "application/json"}, method=m)
    return json.load(urllib.request.urlopen(r))

def plot(pid, rect, x, y1, y2, xlabels, ylim, title, ylabel):
    return {"id": pid, "rect": rect, "type": "line",
            "data": {"x": f"@{x}", "y": f"@{y1}", "z": None, "field": None, "band": None, "errorbar": None},
            "style": {"color": "#b03a2e", "lw": 1.8, "marker": "o", "ms": 3, "alpha": 1.0},
            "extra_lines": [{"ref": f"@{y2}", "color": "#8c8c8c", "marker": "^", "ms": 3, "label": "baseline"}],
            "axes": {"xticks": "auto", "xticklabels": xlabels, "yticks": "auto", "yticklabels": None,
                     "xlabel": "", "ylabel": ylabel, "ylim": list(ylim),
                     "spines": {"top": False, "right": False, "bottom": True, "left": True}, "tick_labelsize": 8},
            "title": {"text": title, "x": 0.5, "y": 1.0, "pad": 2, "fontsize": 10, "visible": bool(title)},
            "artists": []}

slug = call("/api/canvases", {"mode": "blank"}, "POST")["slug"]
spec = {
  "dataset": "fig5.npz",   # already in ~/.figfoundry/datasets/ (or POST /api/datasets to upload)
  "canvas": {"width": 3.28, "height": 2.2, "dpi": 220, "bg": "#ffffff",
             "font_family": "DejaVu Sans", "font_size": 9},
  "plots": [
    plot("tl", [0.055, 0.57, 0.45, 0.32], "crf_x", "open_crf_stamp", "open_crf_wave",
         ["orig","18","23","28","32","38"], (0.55, 1.0), "Open-source", "AUC"),
    plot("tr", [0.54,  0.57, 0.45, 0.32], "crf_x", "closed_crf_stamp", "closed_crf_wave",
         ["orig","18","23","28","32","38"], (0.55, 1.0), "Closed-source", ""),
    plot("bl", [0.055, 0.16, 0.45, 0.32], "rs_x",  "open_rs_stamp",  "open_rs_wave",
         ["orig","720","480","360"], (0.92, 1.0), "", "AUC"),
    plot("br", [0.54,  0.16, 0.45, 0.32], "rs_x",  "closed_rs_stamp","closed_rs_wave",
         ["orig","720","480","360"], (0.92, 1.0), "", ""),
  ],
  "figure_artists": [
    {"type": "legend", "handles": [
       {"color": "#b03a2e", "marker": "o", "lw": 1.8, "ms": 3},
       {"color": "#8c8c8c", "marker": "^", "lw": 1.6, "ms": 3}],
     "labels": ["STAMP", "WaveRep"], "loc": [0.5, 1.0], "ncol": 2, "frameon": False, "fontsize": 9},
  ],
}
call(f"/api/canvases/{slug}", {"name": "My figure", "spec": spec}, "PUT")

png = base64.b64decode(call(f"/api/canvases/{slug}/render", {"spec": spec}, "POST")["img"])
open("fig.pdf", "wb").write(urllib.request.urlopen(f"{B}/api/canvases/{slug}/figure?format=pdf").read())
code = call(f"/api/canvases/{slug}/code")["code"]     # the standalone .py
```

The exported `.py` is **machine-independent**: by default it reads the dataset file *next to
the script* (no absolute path is baked in), takes `--data /path/to/your/dataset` to override,
`--format png|svg|pdf`, `--dpi` (default: the canvas dpi) and `--out` (default `fig.<format>`).
`figfoundry export … --out-dir DIR` writes the script, figure, spec and a dataset copy together,
ready to commit next to a paper.

## Tips for agents

- **Iterate on the spec, then render.** Change the JSON, `PUT` it, `POST /render` — the
  returned image is the ground truth. Render cheaply (draft) while adjusting, export once.
- **Check `warnings` after every render.** An empty list means everything in the spec was
  drawn. Fix what it reports before exporting.
- **Every save is undoable.** `PUT` snapshots the previous version; list them with
  `GET /history` and roll back with `POST /restore` — no manual backups needed.
- **Tag what you will edit again** (`"tag": "published-k"`) and select by tag, not by color.
- **Rectangles are figure fractions** (0..1), bottom-origin. For a 2×2 grid in a
  3.28 × 2.2 in canvas, columns sit at `left ≈ 0.055` and `0.54`; rows at
  `bottom ≈ 0.16` and `0.57`.
- **Colors:** a highlighted "ours" series = `#b03a2e` (red), a baseline = `#8c8c8c`
  (gray); the colorblind-safe Okabe–Ito palette is at `/api/palettes`.
- **A bad plot blanks itself** (mismatched/missing data, an unknown type) — it never breaks
  the whole render, so you can build a figure incrementally and watch each panel appear.
- **Sizing for a paper:** a single CVPR/IEEE column is 3.28 in wide; a full-width `figure*`
  is 6.875 in. Set the canvas to the target width and the figure drops in at the right size.

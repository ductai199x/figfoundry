# Fig Foundry — guide for AI agents

Fig Foundry is a WYSIWYG matplotlib figure composer. A human drives it through the
browser; **an agent drives it through a small HTTP API that edits the same JSON spec the
browser uses.** This document is everything you need to load data, compose a figure,
render it, and export it — no GUI required.

## The one idea

A figure is a **spec**: a JSON scene with a canvas (size), a dataset (named signals), a
list of **plots** (each an explicit rectangle + data refs + style + axes + title +
artists), and a list of **figure-level artists** (text, suptitle, legend). The backend
renders a spec by **executing the Python it generates from it**, so whatever you render is
byte-for-byte what the export produces. There is no second renderer to drift from.

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
| PUT | `/api/canvases/{slug}` | save `{name?, spec?}` |
| POST | `/api/canvases/{slug}/duplicate` | duplicate → new `{slug}` |
| DELETE | `/api/canvases/{slug}` | delete |
| POST | `/api/canvases/{slug}/render` | render → `{img (base64), mime, hotspots}`; body may carry `{spec}` |
| GET | `/api/canvases/{slug}/code` | the standalone `.py` → `{code}` |
| GET | `/api/canvases/{slug}/figure?format=png\|svg\|pdf` | the rendered figure bytes |
| GET | `/api/palettes` | color palettes (Okabe–Ito, …) |

## The spec

```jsonc
{
  "dataset": "name.npz",          // null until a dataset is set
  "canvas":  { "width": 6.875, "height": 1.92, "dpi": 220, "bg": "#ffffff",
               "font_family": "DejaVu Sans", "font_size": 10 },
  "layout":  { "margin": 0.05, "gap": 0.04 },
  "plots":         [ /* <plot> … */ ],
  "figure_artists": [ /* <figartist> … */ ]
}
```

### A plot

```jsonc
{
  "id": "p1",
  "rect": [left, bottom, width, height],   // figure fractions (0..1), bottom-origin
  "type": "line",                           // 23 types (below)
  "data": { "x": "@x", "y": "@y", "z": null, "field": null, "band": null, "errorbar": null },
  "style": { "color": "#b03a2e", "lw": 1.8, "marker": "o", "ms": 3, "alpha": 1.0,
             "cmap": "viridis", "bins": 20, "colorbar": false,
             "elev": 30, "azim": -60, "dist": 10 },
  "extra_lines": [ { "ref": "@y2", "color": "#8c8c8c", "marker": "^", "ms": 3, "label": "series" } ],
  "axes": { "xticks": "auto", "xticklabels": null, "yticks": "auto", "yticklabels": null,
            "xlabel": "", "ylabel": "", "xlim": null, "ylim": [0.55, 1.0],
            "xscale": "linear", "yscale": "linear",
            "spines": { "top": false, "right": false, "bottom": true, "left": true },
            "tick_labelsize": 8, "labelbottom": true, "labelleft": true },
  "title": { "text": "", "x": 0.5, "y": 1.0, "pad": 2, "fontsize": 10,
             "weight": "normal", "color": "#000000", "visible": false, "rotation": 0 },
  "artists": [ /* <artist> … */ ]
}
```

- **Data refs** are `@signalName` (an index into the dataset); `null`/omitted = none. `x`
  may be `null` (plotted against the array index). `xlim`/`ylim` are `[lo, hi]` or `null`.
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
  "fontsize": 10, "color": "#000", "axes_frac": true, "rotation": 0 }
```
Other types: `vline` (`x`), `hline` (`y`), `annotation` (`xy`, `xytext`),
`arrow` (`x0,y0,x1,y1`), `box` (`x0,y0,w,h,fc,ec`), `circle` (`x,y,r`),
`ellipse` (`x,y,w,h,angle`), `polygon` (`points`), `vspan` (`x0,x1`). `axes_frac` text/box
use 0..1 panel coordinates; data-coord artists (`vline`, `circle`, …) use the axis data units.
`rotation` (degrees) is honored on text, annotation, title, suptitle and figure text.

### A figure-level artist
```jsonc
{ "type": "text",     "x": 0.5, "y": 0.02, "text": "x-axis", "ha": "center", "fontsize": 12, "color": "#000" }
{ "type": "suptitle", "text": "Title", "x": 0.5, "y": 1.0, "fontsize": 16, "color": "#000" }
{ "type": "legend",   "handles": [ { "color": "#b03a2e", "marker": "o", "lw": 1.8, "ms": 3 } ],
                      "labels": [ "STAMP" ], "loc": [0.5, 1.0], "ncol": 1, "frameon": false, "fontsize": 10 }
```

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

The exported `.py` is **machine-independent**: it takes `--data /path/to/your/dataset` and
`--format png|svg|pdf`, so you can run it on any machine that has the dataset.

## Tips for agents

- **Iterate on the spec, then render.** Change the JSON, `PUT` it, `POST /render` — the
  returned image is the ground truth. Render cheaply (draft) while adjusting, export once.
- **Rectangles are figure fractions** (0..1), bottom-origin. For a 2×2 grid in a
  3.28 × 2.2 in canvas, columns sit at `left ≈ 0.055` and `0.54`; rows at
  `bottom ≈ 0.16` and `0.57`.
- **Colors:** a highlighted "ours" series = `#b03a2e` (red), a baseline = `#8c8c8c`
  (gray); the colorblind-safe Okabe–Ito palette is at `/api/palettes`.
- **A bad plot blanks itself** (mismatched/missing data, an unknown type) — it never breaks
  the whole render, so you can build a figure incrementally and watch each panel appear.
- **Sizing for a paper:** a single CVPR/IEEE column is 3.28 in wide; a full-width `figure*`
  is 6.875 in. Set the canvas to the target width and the figure drops in at the right size.

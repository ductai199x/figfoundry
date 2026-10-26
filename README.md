# Fig Foundry

A WYSIWYG matplotlib figure composer. Load your data (`.npz` / `.pkl` / `.csv`),
drag signals onto a free canvas to build plots, arrange and style them, and export
clean, reproducible Python.

**The preview renders by executing the generated code**, so what you see is
byte-for-byte what the exported `.py` produces. There is no second renderer to
drift.

Fig Foundry is a standalone local app with no dependency on any particular
project's data. It ships with a small neutral `sample.npz` (1-D signals plus a
2-D field) so a fresh install has something to open; load your own dataset to work
with real data.

## Install

```bash
uv tool install ./path/to/fig_editor     # installs a `figfoundry` command
# or, from a git repo:
uv tool install figfoundry --from "git+https://github.com/<you>/<repo>.git"
```

Then run:

```bash
figfoundry             # starts the web app on http://127.0.0.1:8037 and opens the browser
figfoundry serve --port 9000 --no-browser
```

## Command line

The same engines are available as subcommands for scripting:

```bash
figfoundry list                              # list saved figures
figfoundry new --name my-fig                 # create a blank figure, print its slug
figfoundry export SLUG [--out PATH]          # write a figure's generated .py
figfoundry render SLUG --format svg|pdf|png  # render a figure to a file
figfoundry datasets                          # list available datasets
```

## Data

A *dataset* is a named collection of signals (arrays). Supported formats:

- **`.npz`** — numpy zip of named arrays. Safe by construction.
- **`.pkl`** — Python pickle, loaded through a **whitelist unpickler** that only
  permits numpy arrays and plain containers (blocks `os`, `subprocess`, `socket`,
  `ctypes`, …). A malicious pickle cannot execute code.
- **`.csv`** — a table with a header row; each numeric column becomes a signal.

1-D and 2-D arrays are both supported (2-D for image/contour/vector plots). Load a
dataset with the **Load data…** button in the toolbar; each canvas is tied to one
dataset, and its signals populate the library.

Your figures, datasets, and exports live in `~/.figfoundry/`.

## What you can do

- Drag a signal onto the canvas to make a plot (or onto an existing plot to
  overlay a line / σ-band / error bars / replace).
- **Plot types (20):** line, step, scatter, grouped scatter (clustering), bar,
  bar (horizontal), histogram, box plot, violin plot, 2-D histogram, hexbin, image,
  contour (filled / lines), pcolormesh, vector field, streamplot, decision boundary,
  pie, polar.
- Move, resize (with snapping), and nudge (arrow keys) plots and elements.
- Multi-select (Shift+click) to nudge / move / delete several at once.
- Per-plot: data, color, line/marker, σ-band, error bars, ticks, labels, limits,
  spines, title, colormap + colorbar, and artists (vline, hline, text, annotate,
  arrow, box, circle, ellipse, polygon, span, legend).
- **Suggest accessible colors** — one click applies the colorblind-safe Okabe-Ito
  palette to a plot's series.
- **LaTeX mathtext** in labels, titles, and text (e.g. `$x_0$`).
- Layout: grid, align, distribute, margins, sync axis ranges.
- Figure elements: axis labels, legend, suptitle, free text.
- Name / duplicate / delete figures. Export the `.py` (copy or download), and the
  figure itself as **PNG / SVG / PDF** (SVG and PDF are vector, for publication).

Everything is also available through a small JSON API (`/api/canvases`,
`/api/datasets`, `/api/palettes`, `/api/canvases/{slug}/render|code|figure|download`).

## Testing

The test suite lives in `tests/` (outside the package, so it is never shipped).
It proves the WYSIWYG invariant (for every plot type), the pickle-safety
whitelist, and dataset I/O (npz/pkl/csv), each against a throwaway data dir:

```bash
uv run --extra dev pytest -q
```

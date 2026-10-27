"""Fig Foundry -- canvas composition engine.

The atom is DATA. A figure is a free-form scene: a canvas (inches) holding any
number of plots, each an explicit rectangle ``fig.add_axes([left, bottom, width,
height])`` in figure-fraction coordinates. Because layout is explicit positions
(not a subplots grid), plots can be moved, resized and snapped freely, and the
generated code is a plain list of ``add_axes`` calls.

WYSIWYG BY CONSTRUCTION: the preview render *executes the generated code*
(``build_figure`` runs ``codegen_canvas(spec)``). There is exactly one drawing
path -- the emitted .py -- so what you see in the browser is byte-for-byte what
the exported file produces. There is no second, hand-written renderer to drift.

Layers:
    spec  -- the editable scene (canvas + plots + figure artists). What UI/API edits.
    codegen_canvas(spec) -- serialize the scene to a standalone .py (references the dataset).
    build_figure(spec)    -- exec that .py and return the Figure (the preview).
    hotspots_from_fig     -- walk the built figure for pixel bboxes (hit-testing).
"""
import base64
import io
import os

import numpy as np

from . import data_io

DATA_DIR = data_io.DATA_DIR
PREVIEW_DPI = 150
_RED = "#b03a2e"
_BLUE = "#2f6690"

# filename -> ((mtime_ns, size), arrays). The (mtime, size) key invalidates the
# entry when the file is replaced or shrunk/grown under the same name.
_ds_cache = {}


def get_dataset_arrays(filename):
    """Load (and cache) a dataset's {signal_name: ndarray} from datasets/<filename>.

    Cached per filename but invalidated by (mtime, size): a re-upload that
    replaces the file, or a deletion, must not keep serving stale arrays. The
    render path re-reads the file from disk every time, so a stale cache would
    make the signal library (names/lengths) disagree with what the figure
    actually draws, and references would break only at render time.
    """
    path = os.path.join(DATA_DIR, filename)
    st = os.stat(path)  # raises FileNotFoundError if the dataset was deleted
    key = (st.st_mtime_ns, st.st_size)
    hit = _ds_cache.get(filename)
    if hit is not None and hit[0] == key:
        return hit[1]
    arrays = data_io.load_arrays(path)[0]
    _ds_cache[filename] = (key, arrays)
    return arrays


# --------------------------------------------------------------------------- data refs
def ref_to_code(ref):
    """Emit the Python expression that reproduces a signal reference (indexes ``data``).

    Only "@name" strings are signal refs; anything else (None, a bare number, a
    dict) means "no signal" and emits None. Emitting repr(5) would generate
    code that crashes at exec time (e.g. len(5) inside the guards)."""
    if not (isinstance(ref, str) and ref.startswith("@")):
        return "None"
    return f'data[{ref[1:]!r}]'


def dataset_loader_code(filename):
    """Emit the Python that loads the dataset file into ``data`` (a dict of arrays)."""
    path = os.path.join(DATA_DIR, filename)
    if filename.endswith(".npz"):
        return [
            "import numpy as np",
            f"DATA_DEFAULT = {path!r}",
            "def _load_data(p):",
            "    _ds = np.load(p, allow_pickle=False)",
            "    return {k: _ds[k] for k in _ds.files}",
            'if __name__ == "__main__":',
            "    data = None  # loaded in the __main__ block below (via --data)",
            "else:",
            "    data = _load_data(DATA_DEFAULT)",
        ]
    if filename.endswith(".csv"):
        return [
            "import numpy as np, csv",
            f"DATA_DEFAULT = {path!r}",
            "def _load_data(p):",
            "    _f = open(p, newline='', encoding='utf-8-sig'); _rows = [r for r in csv.reader(_f) if any(c.strip() for c in r)]; _f.close()",
            "    _data = {}",
            "    for _i, _c in enumerate(_rows[0] if _rows else []):",
            "        _v = []",
            "        for _r in _rows[1:]:",
            "            if _i < len(_r):",
            "                try: _v.append(float(_r[_i]))",
            "                except ValueError: pass",
            "        if _v: _data[_c.strip() or f'col{_i}'] = np.array(_v)",
            "    return _data",
            'if __name__ == "__main__":',
            "    data = None  # loaded in the __main__ block below (via --data)",
            "else:",
            "    data = _load_data(DATA_DEFAULT)",
        ]
    # .pkl -- embedded whitelist unpickler (numpy + plain containers; blocks
    # os/subprocess/socket/ctypes and non-container builtins). The numpy
    # whitelist is the exact (module, name) set, matching data_io.SafeUnpickler:
    # a prefix match would let numpy.fromfile (arbitrary file read) through.
    # A single-array pickle (not a dict) is accepted too, as data_io does.
    return [
        "import numpy as np, pickle, importlib, builtins",
        "class _SafeUnpickler(pickle.Unpickler):",
        '    _B = {"tuple", "list", "dict", "set"}',
        '    _N = {("numpy", "ndarray"), ("numpy", "dtype"), ("numpy.core.multiarray", "_reconstruct"), ("numpy.core.multiarray", "scalar"), ("numpy._core.multiarray", "_reconstruct"), ("numpy._core.multiarray", "scalar")}',
        "    def find_class(self, m, n):",
        '        if m == "builtins":',
        '            if n not in self._B: raise pickle.UnpicklingError("blocked " + m + "." + n)',
        '            return getattr(builtins, n)',
        '        if (m, n) in self._N:',
        '            return getattr(importlib.import_module(m), n)',
        '        raise pickle.UnpicklingError("blocked " + m + "." + n)',
        "def _safe_load(p):",
        '    with open(p, "rb") as f: return _SafeUnpickler(f).load()',
        f"DATA_DEFAULT = {path!r}",
        "def _load_data(p):",
        "    _obj = _safe_load(p)",
        "    _items = _obj.items() if isinstance(_obj, dict) else [('data', _obj)]",
        "    return {str(k): np.asarray(v) for k, v in _items if np.asarray(v).dtype != object}",
        'if __name__ == "__main__":',
        "    data = None  # loaded in the __main__ block below (via --data)",
        "else:",
        "    data = _load_data(DATA_DEFAULT)",
    ]


# --------------------------------------------------------------------------- scene model
def blank_spec():
    return {
        "dataset": None,
        "canvas": {"width": 7.4, "height": 4.5, "dpi": 220, "bg": "#ffffff",
                   "font_family": "DejaVu Sans", "font_size": 14},
        "layout": {"margin": 0.05, "gap": 0.04},
        "plots": [],
        "figure_artists": [],
    }


def _new_plot(pid, rect, x_ref, y_ref, **kw):
    p = {
        "id": pid,
        "rect": [round(float(v), 4) for v in rect],
        "data": {"x": x_ref, "y": y_ref, "z": None, "field": None, "band": None, "errorbar": None},
        "type": "line",
        "style": {"color": _RED, "lw": 1.6, "marker": "o", "ms": 2.4, "alpha": 1.0, "band_alpha": 0.13,
                  "width": 0.6, "cmap": "viridis", "bins": 20, "colorbar": False, "elev": 30, "azim": -60, "dist": 10},
        "extra_lines": [],
        "axes": {
            "xticks": "auto", "xticklabels": None, "yticks": "auto", "yticklabels": None,
            "xlabel": "", "ylabel": "", "xlabel_visible": True, "ylabel_visible": True,
            "xlim": None, "ylim": None, "xscale": "linear", "yscale": "linear",
            "spines": {"top": False, "right": False, "bottom": True, "left": True},
            # 12 to match the frontend _blankPlot default (canvas.js); the two
            # factories must stay field-for-field equivalent.
            "tick_labelsize": 12, "labelbottom": True, "labelleft": True,
        },
        "title": {"text": "", "x": 0.0, "y": 1.0, "pad": 1.0, "fontsize": 11, "weight": "normal",
                  "color": "#000000", "visible": False},
        "artists": [],
    }
    p.update(kw)
    return p


def _num(v, default):
    """Coerce a client-supplied value to a finite float, else ``default``.

    The render API accepts a client-controlled spec, so every numeric field that
    lands in the generated code goes through this (or _numlist) first: junk in a
    saved/POSTed spec must blank a panel, never 500 the render or emit code that
    crashes at exec time."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if np.isfinite(f) else default


def _numlist(v):
    """Coerce a tick/limit list to a list of finite floats; None if nothing usable."""
    if not isinstance(v, (list, tuple)) or not v:
        return None
    out = []
    for x in v:
        try:
            f = float(x)
        except (TypeError, ValueError):
            continue
        if np.isfinite(f):
            out.append(f)
    return out or None


def _bins(st):
    """A positive integer bin count from style, defaulting to 20 on junk."""
    b = _num(st.get("bins"), 20)
    return int(b) if b > 0 else 20


# --------------------------------------------------------------------------- codegen (the single drawing path)
def codegen_canvas(spec, detail=True):
    # A spec can be client-controlled (POST /render) or hand-edited; missing or
    # junk top-level fields must degrade to blanks, never a 500.
    c = spec.get("canvas") or blank_spec()["canvas"]
    # clamp to sane positive values: figsize/dpi/font-size reject 0/negative at
    # exec time, and a client spec can carry anything
    width = max(0.1, _num(c.get("width"), 7.4))
    height = max(0.1, _num(c.get("height"), 4.5))
    dpi = max(1, int(_num(c.get("dpi"), PREVIEW_DPI)))
    font_size = max(1.0, _num(c.get("font_size"), 14))
    s = []
    ds = spec.get("dataset")
    ref = f"dataset {ds!r}" if ds else "no dataset (load data first)"
    s.append(f'"""Canvas figure -- generated by Fig Foundry. WYSIWYG: references {ref}."""')
    s.append('import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt')
    s.append("from matplotlib.lines import Line2D")
    s.append("from matplotlib.patches import Rectangle, Circle, Ellipse, Polygon")
    s.append("")
    s.append("# ---- data (dataset) ----")
    if ds:
        s.extend(dataset_loader_code(ds))
    else:
        s.append("data = {}  # no dataset loaded")
    s.append("")
    s.append("def build():")
    s.append(f'    plt.rcParams["font.family"] = {c.get("font_family", "DejaVu Sans")!r}; plt.rcParams["font.size"] = {font_size!r}')
    s.append(f"    fig = plt.figure(figsize=({width!r}, {height!r}), dpi={dpi})")
    s.append(f'    fig.patch.set_facecolor({c.get("bg", "#ffffff")!r})')
    for p in spec.get("plots") or []:
        i0 = len(s)
        try:
            s.append(f"    # ---- plot {p['id']} ----")
            t = p.get("type", "line")
            if t in ("scatter-3d", "surface", "wireframe"):
                proj = "projection='3d'"
            elif t == "polar":
                proj = "projection='polar'"
            else:
                proj = ""
            s.append(f"    ax = fig.add_axes({[float(v) for v in p['rect']]!r}" + (f", {proj})" if proj else ")"))
            if detail:
                _codegen_axes(s, p.get("axes") or {})
            d = p.get("data") or {}
            xcode = ref_to_code(d.get("x"))
            ycode = ref_to_code(d.get("y"))
            zcode = ref_to_code(d.get("z"))
            fcode = ref_to_code(d.get("field"))
            st = p.get("style") or {}
            _codegen_plot(s, p, detail, xcode, ycode, zcode, fcode, st)
            if t in ("line", "scatter", "bar"):
                for el in p.get("extra_lines") or []:
                    if not isinstance(el, dict):
                        continue
                    ex = ref_to_code(el.get("ref"))
                    emk = el.get("marker", "") if detail else ""
                    # x falls back to the index (like the main line call) so an x-less
                    # plot still draws its extra lines; skip if either signal is missing.
                    ex_x = f"np.arange({ycode}.shape[0])" if xcode == "None" else xcode
                    s.append(f"    if {ycode} is not None and {ex} is not None: ax.plot({ex_x}, {ex}, color={el.get('color', _BLUE)!r}, lw={_num(el.get('lw'), 1.5)!r}, marker={emk!r}, ms={_num(el.get('ms'), 0)!r}, alpha={_num(el.get('alpha'), 1.0)!r}, zorder=3, label={el.get('label', '')!r})")
            band = d.get("band")
            if detail and t in ("line", "scatter") and isinstance(band, str) and band.startswith("@") and ycode != "None":
                secode = ref_to_code(band)
                # guard: only draw if the band signal matches the x/y length (else skip,
                # don't break). Compare shapes only for the signals that exist (x may be
                # None).
                conds = [f"{ycode}.shape == _se.shape"]
                band_x = xcode
                if xcode != "None":
                    conds.append(f"{xcode}.shape == _se.shape")
                else:
                    band_x = f"np.arange({ycode}.shape[0])"  # index fallback (fill_between needs an x)
                s.append(f"    _se = {secode}")
                s.append(f"    if {' and '.join(conds)}: ax.fill_between({band_x}, {ycode} - _se, {ycode} + _se, color={st.get('color', _RED)!r}, alpha={_num(st.get('band_alpha'), 0.13)!r}, lw=0, zorder=2)")
            if detail and t in ("line", "scatter") and ycode != "None":
                eb = d.get("errorbar")
                er = eb.get("ref") if isinstance(eb, dict) else None
                if isinstance(er, str) and er.startswith("@"):
                    ecode = ref_to_code(er)
                    conds = [f"{ycode}.shape == _eb.shape"]
                    eb_x = xcode
                    if xcode != "None":
                        conds.append(f"{xcode}.shape == _eb.shape")
                    else:
                        eb_x = f"np.arange({ycode}.shape[0])"  # index fallback (errorbar needs an x)
                    s.append(f"    _eb = {ecode}")
                    s.append(f"    if {' and '.join(conds)}: ax.errorbar({eb_x}, {ycode}, yerr=_eb, fmt='none', ecolor={eb.get('color', st.get('color', _RED))!r}, elinewidth={_num(eb.get('lw'), 1.0)!r}, capsize=2, zorder=2)")
            for ai, art in enumerate((p.get("artists") if detail else []) or []):
                try:
                    s.append("    " + _codegen_artist(art))
                except (KeyError, TypeError, ValueError, IndexError, AttributeError):
                    s.append(f"    pass  # artist {ai} skipped (invalid)")
            if detail:
                tt = p.get("title") or {}
                if tt.get("visible"):
                    w = tt.get("weight", "normal")
                    w = w if w in ("normal", "bold", "light", "medium", "heavy", "semibold", "black") else "normal"
                    s.append(f'    ax.set_title({tt.get("text", "")!r}, x={_num(tt.get("x"), 0.0)!r}, y={_num(tt.get("y"), 1.0)!r}, pad={_num(tt.get("pad"), 1.0)!r}, fontsize={_num(tt.get("fontsize"), 11)!r}, fontweight={w!r}, color={tt.get("color", "#000000")!r})')
                    _trot = _num(tt.get("rotation"), 0)
                    if _trot:
                        s.append(f"    ax.title.set_rotation({_trot!r})")
        except (KeyError, TypeError, ValueError, IndexError, AttributeError):
            # One malformed plot blanks itself out; it must not sink the whole render.
            pid = p.get("id", "?") if isinstance(p, dict) else "?"
            s[i0:] = [f"    pass  # plot {pid!r} skipped (invalid spec)"]
    if detail:
        fas = spec.get("figure_artists") or []
        if fas:
            s.append("    # ---- figure-level artists ----")
            for j, fa in enumerate(fas):
                try:
                    s.append("    " + _codegen_figartist(fa))
                except (KeyError, TypeError, ValueError, IndexError, AttributeError):
                    s.append(f"    pass  # figure artist {j} skipped (invalid)")
    s.append("    return fig")
    s.append("")
    s.append('if __name__ == "__main__":')
    s.append('    import argparse')
    s.append('    ap = argparse.ArgumentParser(description=__doc__)')
    s.append('    ap.add_argument("--format", default="png", choices=["png", "svg", "pdf"])')
    s.append(f"    ap.add_argument(\"--dpi\", type=int, default={dpi})")
    if ds:
        s.append('    ap.add_argument("--data", default=DATA_DEFAULT, help="path to the dataset file (default is where this figure was made)")')
    s.append("    a = ap.parse_args()")
    if ds:
        s.append("    try: data = _load_data(a.data)")
        s.append('    except FileNotFoundError as _e: raise SystemExit(f"dataset not found: {_e}; pass --data /path/to/your/dataset")')
    s.append('    fig = build(); fig.savefig(f"fig.{a.format}", dpi=a.dpi)')
    return "\n".join(s) + "\n"


def _view3d(s, st):
    """Emit the 3-D camera: rotation (view_init) + zoom (dist). dist uses the
    (private) camera-distance attribute, guarded so a matplotlib change can't
    break the render."""
    s.append(f"    ax.view_init(elev={_num(st.get('elev'), 30)!r}, azim={_num(st.get('azim'), -60)!r})")
    s.append(f"    try: ax._dist = {_num(st.get('dist'), 10)!r}")
    s.append("    except Exception: pass")


def _codegen_plot(s, p, detail, xcode, ycode, zcode, fcode, st):
    """Emit the primary data call for a plot, dispatched on its type.
    xcode/ycode are 1-D (or 2-D for vector fields), zcode a group/class signal,
    fcode a 2-D field. Each type uses only the codes it needs. Guards keep the
    generated code from crashing on mismatched / empty / all-NaN signals (the UI
    lets any signal drop into any slot): a skipped call leaves a blank panel,
    never a broken render, and preview and export stay identical."""
    t = p.get("type", "line")
    color = st.get("color", _RED)
    lw = _num(st.get("lw"), 1.6)
    ms = _num(st.get("ms"), 0)
    alpha = _num(st.get("alpha"), 1.0)
    width = _num(st.get("width"), 0.6)
    cmap = st.get("cmap", "viridis")
    bins = _bins(st)
    cb = detail and st.get("colorbar")
    # pair guards: user-supplied x and y must agree in length (or shape, for
    # vector fields); z must agree with the plotted points.
    xy_ok = "" if xcode == "None" else f" and len({xcode}) == len({ycode})"
    z_ok = "" if zcode == "None" else f" and len({zcode}) == len({ycode})"
    field2d = f"{fcode} is not None and np.ndim({fcode}) == 2"
    if t == "line":
        mk = st.get("marker", "") if detail else ""
        xy = ycode if xcode == "None" else f"{xcode}, {ycode}"
        s.append(f"    if {ycode} is not None{xy_ok}: ax.plot({xy}, color={color!r}, lw={lw!r}, marker={mk!r}, ms={ms!r}, alpha={alpha!r}, zorder=3)")
    elif t == "step":
        # step() (unlike plot()) requires both x and y, so the index fallback must pair them
        xy = f"np.arange({ycode}.shape[0]), {ycode}" if xcode == "None" else f"{xcode}, {ycode}"
        s.append(f"    if {ycode} is not None{xy_ok}: ax.step({xy}, color={color!r}, lw={lw!r}, alpha={alpha!r}, where='pre', zorder=3)")
    elif t == "scatter":
        # scatter() requires both x and y as positional args
        xarg = f"np.arange({ycode}.shape[0]), {ycode}" if xcode == "None" else f"{xcode}, {ycode}"
        s.append(f"    if {ycode} is not None{xy_ok}: ax.scatter({xarg}, color={color!r}, s=max(1, {ms!r} ** 2), alpha={alpha!r}, zorder=3)")
    elif t == "scatter-grouped":
        xarg = f"np.arange({ycode}.shape[0])" if xcode == "None" else xcode
        g = f"{ycode} is not None{xy_ok}{z_ok}"
        s.append(f"    if {g}: _m = ax.scatter({xarg}, {ycode}, c={zcode}, cmap={cmap!r}, s=max(1, {ms!r} ** 2), edgecolors='none', alpha={alpha!r}, zorder=3)")
        if cb:
            s.append(f"    if {g}: fig.colorbar(_m, ax=ax, pad=0.02)")
    elif t == "bar":
        xarg = f"np.arange({ycode}.shape[0])" if xcode == "None" else xcode
        s.append(f"    if {ycode} is not None{xy_ok}: ax.bar({xarg}, {ycode}, color={color!r}, width={width!r}, alpha={alpha!r})")
    elif t == "barh":
        yarg = f"np.arange({xcode}.shape[0])" if ycode == "None" else ycode
        yx_ok = "" if ycode == "None" else f" and len({ycode}) == len({xcode})"
        s.append(f"    if {xcode} is not None{yx_ok}: ax.barh({yarg}, {xcode}, color={color!r}, height={width!r}, alpha={alpha!r})")
    elif t == "histogram":
        # hist() raises when the data range is not finite (all-NaN / +-inf)
        s.append(f"    if {ycode} is not None and np.isfinite({ycode}).any(): ax.hist({ycode}, bins={bins!r}, color={color!r}, alpha={alpha!r}, edgecolor='none')")
    elif t in ("boxplot", "violinplot"):
        kw = "patch_artist=True" if t == "boxplot" else "showmedians=True"
        if xcode == "None":
            s.append(f"    if {ycode} is not None: ax.{t}({ycode}, {kw})")
        else:
            # group by 1-D keys only (2-D group rows are unhashable; fall back to ungrouped)
            s.append(f"    if {ycode} is not None and np.ndim({xcode}) == 1:")
            s.append("        _g = {}")
            s.append(f"        for _gx, _gy in zip({xcode}, {ycode}): _g.setdefault(_gx, []).append(_gy)")
            s.append("        _ks = sorted(_g)")
            if t == "boxplot":
                s.append("        ax.boxplot([_g[k] for k in _ks], tick_labels=[str(k) for k in _ks], patch_artist=True)")
            else:
                s.append("        ax.violinplot([_g[k] for k in _ks], showmedians=True)")
            s.append(f"    elif {ycode} is not None: ax.{t}({ycode}, {kw})")
    elif t == "hist2d":
        xarg = f"np.arange({ycode}.shape[0])" if xcode == "None" else xcode
        g = (f"{ycode} is not None and np.ndim({xarg}) == 1 and len({xarg}) == len({ycode})"
             f" and np.isfinite({xarg}).any() and np.isfinite({ycode}).any()")
        s.append(f"    if {g}: _h = ax.hist2d({xarg}, {ycode}, bins={bins!r}, cmap={cmap!r})")
        if cb:
            s.append(f"    if {g}: fig.colorbar(_h[3], ax=ax, pad=0.02)")
    elif t == "hexbin":
        xarg = f"np.arange({ycode}.shape[0])" if xcode == "None" else xcode
        g = f"{ycode} is not None and np.ndim({xarg}) == 1 and len({xarg}) == len({ycode})"
        s.append(f"    if {g}: _m = ax.hexbin({xarg}, {ycode}, gridsize={bins!r}, cmap={cmap!r})")
        if cb:
            s.append(f"    if {g}: fig.colorbar(_m, ax=ax, pad=0.02)")
    elif t == "imshow":
        g = field2d
        s.append(f"    if {g}: _m = ax.imshow({fcode}, cmap={cmap!r}, origin='upper')")
        if cb:
            s.append(f"    if {g}: fig.colorbar(_m, ax=ax, pad=0.02)")
    elif t in ("contourf", "contour"):
        # contour() needs at least a (2, 2) field
        g = f"{field2d} and {fcode}.shape[0] >= 2 and {fcode}.shape[1] >= 2"
        s.append(f"    if {g}: _m = ax.{t}({fcode}, cmap={cmap!r})")
        if cb:
            s.append(f"    if {g}: fig.colorbar(_m, ax=ax, pad=0.02)")
    elif t == "pcolormesh":
        g = field2d
        s.append(f"    if {g}: _m = ax.pcolormesh({fcode}, cmap={cmap!r})")
        if cb:
            s.append(f"    if {g}: fig.colorbar(_m, ax=ax, pad=0.02)")
    elif t == "quiver":
        # 2-arg quiver(U, V): x slot = U field, y slot = V field (per the UI); U and V
        # must share a shape (1-D: equal length) or matplotlib raises.
        g = f"{xcode} is not None and {ycode} is not None and np.shape({xcode}) == np.shape({ycode})"
        s.append(f"    if {g}: ax.quiver({xcode}, {ycode}, color={color!r})")
    elif t == "streamplot":
        g = (f"{xcode} is not None and {ycode} is not None and np.ndim({xcode}) == 2 and np.ndim({ycode}) == 2"
             f" and np.shape({xcode}) == np.shape({ycode})")
        s.append(f"    if {g}: _gx, _gy = np.meshgrid(np.arange({xcode}.shape[1]), np.arange({xcode}.shape[0])); ax.streamplot(_gx, _gy, {xcode}, {ycode}, color={color!r})")
    elif t == "pie":
        vals = zcode if zcode != "None" else ycode
        # pie() rejects negative/zero-sum wedges, non-numeric and non-1-D values;
        # skip, don't break
        g = (f"{vals} is not None and len({vals}) > 0 and np.ndim({vals}) == 1"
             f" and np.issubdtype(np.asarray({vals}).dtype, np.number)"
             f" and np.all({vals} >= 0) and np.sum({vals}) > 0")
        s.append(f"    if {g}: ax.pie({vals}, autopct='%1.1f%%', startangle=90)")
    elif t == "polar":
        xy = ycode if xcode == "None" else f"{xcode}, {ycode}"
        s.append(f"    if {ycode} is not None{xy_ok}: ax.plot({xy}, color={color!r}, lw={lw!r})")
    elif t == "scatter-3d":
        g = f"{xcode} is not None and {ycode} is not None and {zcode} is not None and len({xcode}) == len({ycode}) == len({zcode})"
        s.append(f"    if {g}: ax.scatter({xcode}, {ycode}, {zcode}, color={color!r}, s=max(1, {ms!r} ** 2))")
        _view3d(s, st)
    elif t == "surface":
        s.append(f"    if {field2d}: _X, _Y = np.meshgrid(np.arange({fcode}.shape[1]), np.arange({fcode}.shape[0])); ax.plot_surface(_X, _Y, {fcode}, cmap={cmap!r})")
        _view3d(s, st)
    elif t == "wireframe":
        s.append(f"    if {field2d}: _X, _Y = np.meshgrid(np.arange({fcode}.shape[1]), np.arange({fcode}.shape[0])); ax.plot_wireframe(_X, _Y, {fcode}, color={color!r})")
        _view3d(s, st)
    elif t == "decision-boundary":
        g = f"{field2d} and {fcode}.shape[0] >= 2 and {fcode}.shape[1] >= 2"
        s.append(f"    if {g}: ax.contourf({fcode}, cmap='coolwarm', alpha=0.45, zorder=1)")
        sg = f"{xcode} is not None and {ycode} is not None{xy_ok}{z_ok}"
        s.append(f"    if {sg}: _m = ax.scatter({xcode}, {ycode}, c={zcode}, cmap='coolwarm', edgecolors='k', linewidths=0.5, s=max(1, {ms!r} ** 2), zorder=3)")
        if cb:
            s.append(f"    if {sg}: fig.colorbar(_m, ax=ax, pad=0.02)")
    else:
        # unknown type: fall back to a plain line (skip when there is no y)
        xy = ycode if xcode == "None" else f"{xcode}, {ycode}"
        s.append(f"    if {ycode} is not None{xy_ok}: ax.plot({xy})  # type {t!r} not emitted -> line fallback")


def _codegen_axes(s, A):
    if not isinstance(A, dict):
        A = {}
    # Ticks/limits are sanitized to finite floats at codegen time (not inside a
    # try in the emitted code): a junk value blanks the setting instead of
    # raising here (500) or at exec time (ConversionError).
    tl = _numlist(A.get("xticks"))
    if tl is not None:
        s.append(f"    ax.set_xticks({tl!r})")
    if A.get("xticklabels") is not None:
        s.append(f"    ax.set_xticklabels({A['xticklabels']!r})")
    yl = _numlist(A.get("yticks"))
    if yl is not None:
        s.append(f"    ax.set_yticks({yl!r})")
    if A.get("yticklabels") is not None:
        s.append(f"    ax.set_yticklabels({A['yticklabels']!r})")
    xl = _numlist(A.get("xlim"))
    if xl is not None and len(xl) >= 2:
        s.append(f"    ax.set_xlim({xl[0]!r}, {xl[1]!r})")
    yl2 = _numlist(A.get("ylim"))
    if yl2 is not None and len(yl2) >= 2:
        s.append(f"    ax.set_ylim({yl2[0]!r}, {yl2[1]!r})")
    if A.get("xscale") in ("log", "symlog", "logit"):
        s.append(f"    ax.set_xscale({A['xscale']!r})")
    if A.get("yscale") in ("log", "symlog", "logit"):
        s.append(f"    ax.set_yscale({A['yscale']!r})")
    s.append(f"    ax.set_xlabel({A.get('xlabel', '')!r}, visible={A.get('xlabel_visible', True)})")
    s.append(f"    ax.set_ylabel({A.get('ylabel', '')!r}, visible={A.get('ylabel_visible', True)})")
    sp = A.get("spines")
    if not isinstance(sp, dict):
        sp = {}
    vis = {k: bool(sp.get(k, True)) for k in ("top", "right", "bottom", "left")}
    s.append(f"    for _s, _v in {vis!r}.items():")
    s.append("        try: ax.spines[_s].set_visible(_v)")
    s.append("        except KeyError: pass")
    ax_ = [f"labelbottom={A.get('labelbottom', True)!r}", f"labelleft={A.get('labelleft', True)!r}"]
    tsize = _num(A.get("tick_labelsize"), 0)
    if tsize > 0:
        ax_.append(f"labelsize={tsize!r}")
    s.append("    ax.tick_params(" + ", ".join(ax_) + ")")


def _codegen_artist(art):
    t = art["type"]
    if t == "vline":
        return f'ax.axvline({float(art["x"])!r}, color={art.get("color", "#000000")!r}, ls={art.get("ls", "-")!r}, lw={_num(art.get("lw"), 1.0)!r}, alpha={_num(art.get("alpha"), 1.0)!r}, zorder={int(_num(art.get("zorder"), 2))!r})'
    if t == "hline":
        return f'ax.axhline({float(art["y"])!r}, color={art.get("color", "#000000")!r}, ls={art.get("ls", "-")!r}, lw={_num(art.get("lw"), 1.0)!r}, alpha={_num(art.get("alpha"), 1.0)!r}, zorder={int(_num(art.get("zorder"), 2))!r})'
    if t == "text":
        base = f'ax.text({float(art["x"])!r}, {float(art["y"])!r}, {art.get("text", "")!r}'
        if art.get("axes_frac"):
            base += ", transform=ax.transAxes"
        base += f', ha={art.get("ha", "left")!r}, va={art.get("va", "bottom")!r}, fontsize={_num(art.get("fontsize"), 10)!r}, color={art.get("color", "#000000")!r}, rotation={_num(art.get("rotation"), 0)!r})'
        return base
    if t == "legend":
        h = ", ".join(f'Line2D([], [], color={x.get("color", "#000")!r}, ls={x.get("ls", "-")!r}, lw={_num(x.get("lw"), 1.5)!r}, marker={x.get("marker", "")!r}, ms={_num(x.get("ms"), 0)!r})' for x in art.get("handles", []) if isinstance(x, dict))
        return f'ax.legend([{h}], {art.get("labels", [])!r}, loc={art.get("loc", "best")!r}, ncol={int(_num(art.get("ncol"), 1))!r}, frameon={art.get("frameon", True)!r}, fontsize={_num(art.get("fontsize"), 9)!r})'
    if t == "annotation":
        return (f'ax.annotate({art.get("text", "")!r}, xy=({float(art["xy"][0])!r}, {float(art["xy"][1])!r}), '
                f'xytext=({float(art["xytext"][0])!r}, {float(art["xytext"][1])!r}), textcoords="axes fraction", '
                f'arrowprops=dict(arrowstyle="->", color={art.get("color", "#000000")!r}, lw={_num(art.get("lw"), 1.2)!r}), '
                f'color={art.get("color", "#000000")!r}, fontsize={_num(art.get("fontsize"), 10)!r}, rotation={_num(art.get("rotation"), 0)!r})')
    if t == "box":
        return (f'ax.add_patch(Rectangle(({float(art["x0"])!r}, {float(art["y0"])!r}), {float(art["w"])!r}, {float(art["h"])!r}, '
                f'transform=ax.transAxes, fc={art.get("fc", "none")!r}, alpha={_num(art.get("alpha"), 0.15)!r}, '
                f'ec={art.get("ec", "#000000")!r}, lw={_num(art.get("lw"), 1.0)!r}))')
    if t == "vspan":
        return f'ax.axvspan({float(art["x0"])!r}, {float(art["x1"])!r}, color={art.get("color", "#b03a2e")!r}, alpha={_num(art.get("alpha"), 0.12)!r}, lw=0, zorder={int(_num(art.get("zorder"), 0))!r})'
    if t == "circle":
        return f'ax.add_patch(Circle(({float(art["x"])!r}, {float(art["y"])!r}), {float(art.get("r", 0.1))!r}, fill={art.get("fill", False)!r}, color={art.get("color", "#000000")!r}, lw={_num(art.get("lw"), 1.0)!r}, alpha={_num(art.get("alpha"), 1.0)!r}, zorder={int(_num(art.get("zorder"), 3))!r}))'
    if t == "ellipse":
        return f'ax.add_patch(Ellipse(({float(art["x"])!r}, {float(art["y"])!r}), {float(art.get("w", 0.2))!r}, {float(art.get("h", 0.1))!r}, angle={_num(art.get("angle"), 0)!r}, fill={art.get("fill", False)!r}, color={art.get("color", "#000000")!r}, lw={_num(art.get("lw"), 1.0)!r}, alpha={_num(art.get("alpha"), 1.0)!r}, zorder={int(_num(art.get("zorder"), 3))!r}))'
    if t == "polygon":
        pts = [[float(a), float(b)] for a, b in art.get("points", [[0, 0], [1, 0], [1, 1]])]
        return f'ax.add_patch(Polygon({pts!r}, closed=True, fill={art.get("fill", False)!r}, color={art.get("color", "#000000")!r}, lw={_num(art.get("lw"), 1.0)!r}, alpha={_num(art.get("alpha"), 1.0)!r}, zorder={int(_num(art.get("zorder"), 3))!r}))'
    if t == "arrow":
        return f'ax.annotate("", xy=({float(art.get("x1", 1))!r}, {float(art.get("y1", 1))!r}), xytext=({float(art.get("x0", 0))!r}, {float(art.get("y0", 0))!r}), arrowprops=dict(arrowstyle="-|>", color={art.get("color", "#000000")!r}, lw={_num(art.get("lw"), 1.5)!r}))'
    return f"pass  # artist type {t!r} not emitted"


def _codegen_figartist(fa):
    t = fa["type"]
    if t == "legend":
        h = ", ".join(f'Line2D([], [], color={x.get("color", "#000")!r}, ls={x.get("ls", "-")!r}, lw={_num(x.get("lw"), 1.5)!r}, marker={x.get("marker", "")!r}, ms={_num(x.get("ms"), 0)!r})' for x in fa.get("handles", []) if isinstance(x, dict))
        loc = fa.get("loc")
        if isinstance(loc, list):
            return f'fig.legend([{h}], {fa.get("labels", [])!r}, bbox_to_anchor=({float(loc[0])!r}, {float(loc[1])!r}), loc="center", ncol={int(_num(fa.get("ncol"), 1))!r}, frameon={fa.get("frameon", True)!r}, fontsize={_num(fa.get("fontsize"), 10)!r})'
        return f'fig.legend([{h}], {fa.get("labels", [])!r}, loc={loc!r}, ncol={int(_num(fa.get("ncol"), 1))!r}, frameon={fa.get("frameon", True)!r}, fontsize={_num(fa.get("fontsize"), 10)!r})'
    if t == "text":
        return f'fig.text({float(fa["x"])!r}, {float(fa["y"])!r}, {fa.get("text", "")!r}, ha={fa.get("ha", "left")!r}, va={fa.get("va", "bottom")!r}, fontsize={_num(fa.get("fontsize"), 12)!r}, color={fa.get("color", "#000000")!r}, rotation={_num(fa.get("rotation"), 0)!r})'
    if t == "suptitle":
        _srot = _num(fa.get("rotation"), 0)
        _st = f'fig.suptitle({fa.get("text", "")!r}, x={_num(fa.get("x"), 0.5)!r}, y={_num(fa.get("y"), 1.0)!r}, fontsize={_num(fa.get("fontsize"), 14)!r}, color={fa.get("color", "#000000")!r})'
        if _srot:
            _st = "_st = " + _st + f"; _st.set_rotation({_srot!r})"
        return _st
    return f"pass  # figure artist type {t!r} not emitted"


# --------------------------------------------------------------------------- build (exec the code) -- the preview
def build_figure(spec, detail=True):
    """Build the figure by executing the generated code. This IS the export, so the
    full-quality preview is byte-identical to the downloaded .py by construction.
    detail=False skips decorative artists (markers/bands/vlines) for a fast draft."""
    import matplotlib
    matplotlib.use("Agg")
    code = codegen_canvas(spec, detail=detail)
    ns = {"__name__": "figfoundry_generated", "__file__": os.path.abspath(__file__)}
    exec(compile(code, "<figfoundry>", "exec"), ns)
    return ns["build"]()


def _is_data_axes(ax):
    from matplotlib.axes import Axes
    from matplotlib.colorbar import Colorbar
    return isinstance(ax, Axes) and not isinstance(ax, Colorbar)


def _plot_creates_axes(p):
    """Whether codegen emits an add_axes for this plot entry: it must be a dict
    whose id and rect are well-formed. Those are the only fields read before
    add_axes; everything after it is non-raising by construction (sanitized
    numbers, .get everywhere, per-artist nets). hotspots_from_fig uses this to
    stay aligned with the axes build() actually creates."""
    if not isinstance(p, dict):
        return False
    try:
        _ = p["id"]
        [float(v) for v in p["rect"]]
        return True
    except (KeyError, TypeError, ValueError, IndexError, AttributeError):
        return False


def hotspots_from_fig(fig, spec):
    """Walk the built figure to get pixel bboxes (top-origin) for each editable element."""
    rend = fig.canvas.get_renderer()
    img_h = round(fig.get_figheight() * fig.dpi)
    hs = []

    def add(hid, art):
        if art is None:
            return
        try:
            b = art.get_window_extent(rend)
        except Exception:
            return
        hs.append({"id": hid, "x": round(b.x0, 1), "top": round(img_h - b.y1, 1),
                   "w": round(b.width, 1), "h": round(b.height, 1)})

    # axes_list aligns 1:1 with the well-formed plots in spec order (codegen
    # creates one add_axes per such plot), so skip entries codegen skips.
    axes_list = [ax for ax in fig.axes if _is_data_axes(ax)]
    ax_i = 0
    for p in spec.get("plots") or []:
        if not _plot_creates_axes(p):
            continue
        if ax_i >= len(axes_list):
            break
        ax = axes_list[ax_i]
        ax_i += 1
        pid = p.get("id")
        add(f"plot:{pid}", ax)
        if (p.get("title") or {}).get("visible"):
            add(f"title:{pid}", ax.title)
        # Per-plot TEXT artists: ax.texts holds exactly these, in spec order (tick
        # labels and the title are not in ax.texts), so they get canvas overlays
        # (draggable / rotatable / snappable) keyed by their spec index j.
        titer = iter(ax.texts)
        for j, art in enumerate(p.get("artists") or []):
            if isinstance(art, dict) and art.get("type") == "text":
                add(f"artist:{pid}:{j}", next(titer, None))
    # per-plot artists / overlaid lines are selected from the Layers list (robust),
    # not by canvas hit-testing (ax.lines is polluted by extra lines + error bars).
    # fig.texts also holds the suptitle (Figure.suptitle appends to it), so exclude it
    # or the text index mapping shifts by one whenever a suptitle is present.
    # (Axes-level legends from plot artists do NOT enter fig.legends in
    # matplotlib >= 3.x, so the figure-legend index below stays aligned.)
    fig_texts = [t for t in fig.texts if t is not getattr(fig, "_suptitle", None)]
    leg_i = txt_i = 0
    for j, fa in enumerate(spec.get("figure_artists") or []):
        t = fa.get("type") if isinstance(fa, dict) else None
        if t == "legend":
            a = fig.legends[leg_i] if leg_i < len(fig.legends) else None
            leg_i += 1
        elif t == "suptitle":
            a = getattr(fig, "_suptitle", None)
        else:
            a = fig_texts[txt_i] if txt_i < len(fig_texts) else None
            txt_i += 1
        add(f"figartist:{j}", a)
    return hs


# Draft = FULL detail (all markers/bands/vlines/titles/legend) at high-ish DPI + light JPEG,
# so the figure always looks complete during interaction -- only a touch softer than the full
# 150-DPI PNG. The matplotlib build is the cost (DPI is nearly free), so this stays fast.
DRAFT_DPI = 130
DRAFT_JPEG_Q = 85


def render_to_png(spec, draft=False):
    """Render the scene (by executing the generated code) to a preview + hotspots.
    draft=True -> lower-res JPEG (full detail) for live interaction; else full-res PNG."""
    import matplotlib.pyplot as plt
    dpi = DRAFT_DPI if draft else PREVIEW_DPI
    fig = build_figure(spec, detail=True)  # always full detail -- never strip the figure
    try:
        fig.set_dpi(dpi)
        # report size at the reference DPI so draft (low-res) and full display at the same
        # on-screen size: the draft is stretched (blurry), the full is sharp.
        img_w = round(fig.get_figwidth() * PREVIEW_DPI)
        img_h = round(fig.get_figheight() * PREVIEW_DPI)
        buf = io.BytesIO()
        if draft:
            # no explicit draw (print_figure draws); no hotspots -> fastest path
            fig.canvas.print_figure(buf, format="jpeg", dpi=dpi, facecolor="white", pil_kwargs={"quality": DRAFT_JPEG_Q})
            mime, hotspots = "image/jpeg", []
        else:
            fig.canvas.draw()
            hotspots = hotspots_from_fig(fig, spec)
            fig.canvas.print_figure(buf, format="png", dpi=dpi)
            mime = "image/png"
        img = base64.b64encode(buf.getvalue()).decode()
    finally:
        plt.close(fig)  # no figure leak even if a draw/save step raises
    return {"img": img, "mime": mime, "img_w": img_w, "img_h": img_h, "hotspots": hotspots, "draft": draft, "spec": spec}


def render_to_bytes(spec, fmt="png", dpi=None):
    """Render the figure (by executing the generated code) to raw bytes.
    fmt is one of png / svg / pdf -- svg and pdf are vector, for publication."""
    import matplotlib.pyplot as plt
    dpi = dpi or PREVIEW_DPI
    fig = build_figure(spec, detail=True)
    try:
        fig.set_dpi(dpi)
        fig.canvas.draw()
        buf = io.BytesIO()
        fig.canvas.print_figure(buf, format=fmt, dpi=dpi)
        out = buf.getvalue()
    finally:
        plt.close(fig)  # no figure leak even if a draw/save step raises
    return out


def get_palettes():
    """Curated + colormap-sampled palettes for suggesting accessible series colors.
    Okabe-Ito is the standard colorblind-safe palette; the rest are sampled from
    matplotlib's perceptually-uniform colormaps."""
    import matplotlib.pyplot as plt

    def stops(name, n=8):
        cm = plt.get_cmap(name)
        return [f"#{round(r * 255):02x}{round(g * 255):02x}{round(b * 255):02x}"
                for r, g, b in (cm(i / (n - 1))[:3] for i in range(n))]

    return {
        "Okabe-Ito (colorblind-safe)": ["#E69F00", "#56B4E9", "#009E73", "#F0E442", "#0072B2", "#D55E00", "#CC79A7", "#000000"],
        "Bright": ["#0072B2", "#D55E00", "#009E73", "#F0E442", "#E69F00", "#CC79A7", "#56B4E9"],
        "Viridis": stops("viridis"),
        "Plasma": stops("plasma"),
        "Cividis": stops("cividis"),
    }

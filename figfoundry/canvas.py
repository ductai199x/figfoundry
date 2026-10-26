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

from . import data_io

DATA_DIR = data_io.DATA_DIR
PREVIEW_DPI = 150
_RED = "#b03a2e"
_BLUE = "#2f6690"

_ds_cache = {}


def get_dataset_arrays(filename):
    """Load (and cache) a dataset's {signal_name: ndarray} from datasets/<filename>."""
    if filename not in _ds_cache:
        _ds_cache[filename] = data_io.load_arrays(os.path.join(DATA_DIR, filename))[0]
    return _ds_cache[filename]


# --------------------------------------------------------------------------- data refs
def ref_to_code(ref):
    """Emit the Python expression that reproduces a signal reference (indexes ``data``)."""
    if ref is None:
        return "None"
    if not (isinstance(ref, str) and ref.startswith("@")):
        return repr(ref)
    return f'data[{ref[1:]!r}]'


def dataset_loader_code(filename):
    """Emit the Python that loads the dataset file into ``data`` (a dict of arrays)."""
    path = os.path.join(DATA_DIR, filename)
    if filename.endswith(".npz"):
        return [
            "import numpy as np",
            f"_ds = np.load({path!r}, allow_pickle=False)",
            "data = {k: _ds[k] for k in _ds.files}",
        ]
    if filename.endswith(".csv"):
        return [
            "import numpy as np, csv",
            f"_f = open({path!r}, newline='', encoding='utf-8-sig'); _rows = [r for r in csv.reader(_f) if any(c.strip() for c in r)]; _f.close()",
            "_data = {}",
            "for _i, _c in enumerate(_rows[0] if _rows else []):",
            "    _v = []",
            "    for _r in _rows[1:]:",
            "        if _i < len(_r):",
            "            try: _v.append(float(_r[_i]))",
            "            except ValueError: pass",
            "    if _v: _data[_c.strip() or f'col{_i}'] = np.array(_v)",
            "data = _data",
        ]
    # .pkl -- embedded whitelist unpickler (numpy + plain containers; blocks
    # os/subprocess/socket/ctypes and non-container builtins)
    return [
        "import numpy as np, pickle, importlib, builtins",
        "class _SafeUnpickler(pickle.Unpickler):",
        '    _B = {"tuple", "list", "dict", "set"}',
        "    def find_class(self, m, n):",
        '        if m == "builtins":',
        '            if n not in self._B: raise pickle.UnpicklingError("blocked " + m + "." + n)',
        '            return getattr(builtins, n)',
        '        if m == "numpy" or m.startswith("numpy."):',
        '            return getattr(importlib.import_module(m), n)',
        '        raise pickle.UnpicklingError("blocked " + m + "." + n)',
        "def _safe_load(p):",
        '    with open(p, "rb") as f: return _SafeUnpickler(f).load()',
        f'data = {{str(k): np.asarray(v) for k, v in _safe_load({path!r}).items() if np.asarray(v).dtype != object}}',
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
            "tick_labelsize": 14, "labelbottom": True, "labelleft": True,
        },
        "title": {"text": "", "x": 0.0, "y": 1.0, "pad": 1.0, "fontsize": 11, "weight": "normal",
                  "color": "#000000", "visible": False},
        "artists": [],
    }
    p.update(kw)
    return p


# --------------------------------------------------------------------------- codegen (the single drawing path)
def codegen_canvas(spec, detail=True):
    c = spec["canvas"]
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
    s.append(f'    plt.rcParams["font.family"] = {c["font_family"]!r}; plt.rcParams["font.size"] = {c["font_size"]!r}')
    s.append(f'    fig = plt.figure(figsize=({c["width"]!r}, {c["height"]!r}), dpi={c["dpi"]})')
    s.append(f'    fig.patch.set_facecolor({c.get("bg", "#ffffff")!r})')
    for p in spec["plots"]:
        s.append(f"    # ---- plot {p['id']} ----")
        if p["type"] in ("scatter-3d", "surface", "wireframe"):
            proj = "projection='3d'"
        elif p["type"] == "polar":
            proj = "projection='polar'"
        else:
            proj = ""
        s.append(f"    ax = fig.add_axes({[float(v) for v in p['rect']]!r}" + (f", {proj})" if proj else ")"))
        if detail:
            _codegen_axes(s, p["axes"])
        xcode = ref_to_code(p["data"].get("x"))
        ycode = ref_to_code(p["data"].get("y"))
        zcode = ref_to_code(p["data"].get("z"))
        fcode = ref_to_code(p["data"].get("field"))
        st = p["style"]
        _codegen_plot(s, p, detail, xcode, ycode, zcode, fcode, st)
        if p["type"] in ("line", "scatter", "bar"):
            for el in p.get("extra_lines", []):
                ex = ref_to_code(el["ref"])
                emk = el.get("marker", "") if detail else ""
                ems = el.get("ms", 0) if detail else 0
                # x falls back to the index (like the main line call) so an x-less plot
                # still draws its extra lines; skip if either signal is missing.
                ex_x = f"np.arange({ycode}.shape[0])" if xcode == "None" else xcode
                s.append(f"    if {ycode} is not None and {ex} is not None: ax.plot({ex_x}, {ex}, color={el.get('color', _BLUE)!r}, lw={el.get('lw', 1.5)!r}, marker={emk!r}, ms={ems!r}, alpha={el.get('alpha', 1.0)!r}, zorder=3, label={el.get('label', '')!r})")
        if detail and p["type"] in ("line", "scatter") and p["data"].get("band") and ycode != "None":
            secode = ref_to_code(p["data"]["band"])
            # guard: only draw if the band signal matches the x/y length (else skip, don't
            # break). Compare shapes only for the signals that exist (x may be None).
            conds = [f"{ycode}.shape == _se.shape"]
            band_x = xcode
            if xcode != "None":
                conds.append(f"{xcode}.shape == _se.shape")
            else:
                band_x = f"np.arange({ycode}.shape[0])"  # index fallback (fill_between needs an x)
            s.append(f"    _se = {secode}")
            s.append(f"    if {' and '.join(conds)}: ax.fill_between({band_x}, {ycode} - _se, {ycode} + _se, color={st['color']!r}, alpha={st.get('band_alpha', 0.13)!r}, lw=0, zorder=2)")
        if detail and p["type"] in ("line", "scatter") and p["data"].get("errorbar") and ycode != "None":
            eb = p["data"]["errorbar"]
            ecode = ref_to_code(eb["ref"])
            conds = [f"{ycode}.shape == _eb.shape"]
            eb_x = xcode
            if xcode != "None":
                conds.append(f"{xcode}.shape == _eb.shape")
            else:
                eb_x = f"np.arange({ycode}.shape[0])"  # index fallback (errorbar needs an x)
            s.append(f"    _eb = {ecode}")
            s.append(f"    if {' and '.join(conds)}: ax.errorbar({eb_x}, {ycode}, yerr=_eb, fmt='none', ecolor={eb.get('color', st['color'])!r}, elinewidth={eb.get('lw', 1.0)!r}, capsize=2, zorder=2)")
        for art in (p["artists"] if detail else []):
            s.append("    " + _codegen_artist(art))
        if detail and p["title"].get("visible"):
            tt = p["title"]
            s.append(f'    ax.set_title({tt["text"]!r}, x={tt.get("x", 0.0)!r}, y={tt.get("y", 1.0)!r}, pad={tt.get("pad", 1.0)!r}, fontsize={tt["fontsize"]!r}, fontweight={tt["weight"]!r}, color={tt["color"]!r})')
    if detail and spec["figure_artists"]:
        s.append("    # ---- figure-level artists ----")
        for fa in spec["figure_artists"]:
            s.append("    " + _codegen_figartist(fa))
    s.append("    return fig")
    s.append("")
    s.append('if __name__ == "__main__":')
    s.append('    import argparse')
    s.append('    ap = argparse.ArgumentParser(description=__doc__)')
    s.append('    ap.add_argument("--format", default="png", choices=["png", "svg", "pdf"])')
    s.append(f'    ap.add_argument("--dpi", type=int, default={c["dpi"]})')
    s.append("    a = ap.parse_args()")
    s.append('    fig = build(); fig.savefig(f"fig.{a.format}", dpi=a.dpi)')
    return "\n".join(s) + "\n"


def _view3d(s, st):
    """Emit the 3-D camera: rotation (view_init) + zoom (dist). dist uses the
    (private) camera-distance attribute, guarded so a matplotlib change can't
    break the render."""
    s.append(f"    ax.view_init(elev={st.get('elev', 30)!r}, azim={st.get('azim', -60)!r})")
    s.append(f"    try: ax._dist = {float(st.get('dist', 10))!r}")
    s.append("    except Exception: pass")


def _codegen_plot(s, p, detail, xcode, ycode, zcode, fcode, st):
    """Emit the primary data call for a plot, dispatched on its type.
    xcode/ycode are 1-D (or 2-D for vector fields), zcode a group/class signal,
    fcode a 2-D field. Each type uses only the codes it needs."""
    t = p["type"]
    cmap = st.get("cmap", "viridis")
    cb = detail and st.get("colorbar")
    if t == "line":
        mk = st.get("marker", "") if detail else ""
        xy = ycode if xcode == "None" else f"{xcode}, {ycode}"
        s.append(f"    if {ycode} is not None: ax.plot({xy}, color={st['color']!r}, lw={st['lw']!r}, marker={mk!r}, ms={st.get('ms', 0)!r}, alpha={st['alpha']!r}, zorder=3)")
    elif t == "step":
        # step() (unlike plot()) requires both x and y, so the index fallback must pair them
        xy = f"np.arange({ycode}.shape[0]), {ycode}" if xcode == "None" else f"{xcode}, {ycode}"
        s.append(f"    if {ycode} is not None: ax.step({xy}, color={st['color']!r}, lw={st['lw']!r}, alpha={st['alpha']!r}, where='pre', zorder=3)")
    elif t == "scatter":
        # scatter() requires both x and y as positional args
        xarg = f"np.arange({ycode}.shape[0]), {ycode}" if xcode == "None" else f"{xcode}, {ycode}"
        s.append(f"    if {ycode} is not None: ax.scatter({xarg}, color={st['color']!r}, s=max(1, {st['ms']!r} ** 2), alpha={st['alpha']!r}, zorder=3)")
    elif t == "scatter-grouped":
        xarg = f"np.arange({ycode}.shape[0])" if xcode == "None" else xcode
        s.append(f"    if {ycode} is not None: _m = ax.scatter({xarg}, {ycode}, c={zcode}, cmap={cmap!r}, s=max(1, {st['ms']!r} ** 2), edgecolors='none', alpha={st['alpha']!r}, zorder=3)")
        if cb:
            s.append(f"    if {ycode} is not None: fig.colorbar(_m, ax=ax, pad=0.02)")
    elif t == "bar":
        xarg = f"np.arange({ycode}.shape[0])" if xcode == "None" else xcode
        s.append(f"    if {ycode} is not None: ax.bar({xarg}, {ycode}, color={st['color']!r}, width={st.get('width', 0.6)!r}, alpha={st['alpha']!r})")
    elif t == "barh":
        yarg = f"np.arange({xcode}.shape[0])" if ycode == "None" else ycode
        s.append(f"    if {xcode} is not None: ax.barh({yarg}, {xcode}, color={st['color']!r}, height={st.get('width', 0.6)!r}, alpha={st['alpha']!r})")
    elif t == "histogram":
        s.append(f"    if {ycode} is not None: ax.hist({ycode}, bins={st.get('bins', 20)!r}, color={st['color']!r}, alpha={st['alpha']!r}, edgecolor='none')")
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
        g = f"{ycode} is not None and np.ndim({xarg}) == 1"
        s.append(f"    if {g}: _h = ax.hist2d({xarg}, {ycode}, bins={st.get('bins', 20)!r}, cmap={cmap!r})")
        if cb:
            s.append(f"    if {g}: fig.colorbar(_h[3], ax=ax, pad=0.02)")
    elif t == "hexbin":
        xarg = f"np.arange({ycode}.shape[0])" if xcode == "None" else xcode
        g = f"{ycode} is not None and np.ndim({xarg}) == 1"
        s.append(f"    if {g}: _m = ax.hexbin({xarg}, {ycode}, gridsize={st.get('bins', 20)!r}, cmap={cmap!r})")
        if cb:
            s.append(f"    if {g}: fig.colorbar(_m, ax=ax, pad=0.02)")
    elif t == "imshow":
        g = f"{fcode} is not None and np.ndim({fcode}) == 2"
        s.append(f"    if {g}: _m = ax.imshow({fcode}, cmap={cmap!r}, origin='upper')")
        if cb:
            s.append(f"    if {g}: fig.colorbar(_m, ax=ax, pad=0.02)")
    elif t in ("contourf", "contour", "pcolormesh"):
        g = f"{fcode} is not None and np.ndim({fcode}) == 2"
        s.append(f"    if {g}: _m = ax.{t}({fcode}, cmap={cmap!r})")
        if cb:
            s.append(f"    if {g}: fig.colorbar(_m, ax=ax, pad=0.02)")
    elif t == "quiver":
        s.append(f"    if {xcode} is not None and {ycode} is not None: ax.quiver({xcode}, {ycode}, color={st['color']!r})")
    elif t == "streamplot":
        s.append(f"    if {xcode} is not None and {ycode} is not None and np.ndim({xcode}) == 2 and np.ndim({ycode}) == 2: _gx, _gy = np.meshgrid(np.arange({xcode}.shape[1]), np.arange({xcode}.shape[0])); ax.streamplot(_gx, _gy, {xcode}, {ycode}, color={st['color']!r})")
    elif t == "pie":
        vals = zcode if zcode != "None" else ycode
        # pie() rejects negative/zero-sum wedges and non-numeric values; skip, don't break
        g = (f"{vals} is not None and len({vals}) > 0 and np.issubdtype(np.asarray({vals}).dtype, np.number)"
             f" and np.all({vals} >= 0) and np.sum({vals}) > 0")
        s.append(f"    if {g}: ax.pie({vals}, autopct='%1.1f%%', startangle=90)")
    elif t == "polar":
        xy = ycode if xcode == "None" else f"{xcode}, {ycode}"
        s.append(f"    if {ycode} is not None: ax.plot({xy}, color={st['color']!r}, lw={st['lw']!r})")
    elif t == "scatter-3d":
        s.append(f"    if {xcode} is not None and {ycode} is not None: ax.scatter({xcode}, {ycode}, {zcode}, color={st['color']!r}, s=max(1, {st['ms']!r} ** 2))")
        _view3d(s, st)
    elif t == "surface":
        s.append(f"    if {fcode} is not None and np.ndim({fcode}) == 2: _X, _Y = np.meshgrid(np.arange({fcode}.shape[1]), np.arange({fcode}.shape[0])); ax.plot_surface(_X, _Y, {fcode}, cmap={cmap!r})")
        _view3d(s, st)
    elif t == "wireframe":
        s.append(f"    if {fcode} is not None and np.ndim({fcode}) == 2: _X, _Y = np.meshgrid(np.arange({fcode}.shape[1]), np.arange({fcode}.shape[0])); ax.plot_wireframe(_X, _Y, {fcode}, color={st['color']!r})")
        _view3d(s, st)
    elif t == "decision-boundary":
        s.append(f"    if {fcode} is not None and np.ndim({fcode}) == 2: ax.contourf({fcode}, cmap='coolwarm', alpha=0.45, zorder=1)")
        s.append(f"    if {xcode} is not None and {ycode} is not None: _m = ax.scatter({xcode}, {ycode}, c={zcode}, cmap='coolwarm', edgecolors='k', linewidths=0.5, s=max(1, {st['ms']!r} ** 2), zorder=3)")
        if cb:
            s.append(f"    if {xcode} is not None and {ycode} is not None: fig.colorbar(_m, ax=ax, pad=0.02)")
    else:
        s.append(f"    ax.plot({xcode}, {ycode})  # type {t!r} not emitted")


def _codegen_axes(s, A):
    if A.get("xticks") not in (None, "auto"):
        s.append(f"    ax.set_xticks({[float(v) for v in A['xticks']]!r})")
    if A.get("xticklabels") is not None:
        s.append(f"    ax.set_xticklabels({A['xticklabels']!r})")
    if A.get("yticks") not in (None, "auto"):
        s.append(f"    ax.set_yticks({[float(v) for v in A['yticks']]!r})")
    if A.get("yticklabels") is not None:
        s.append(f"    ax.set_yticklabels({A['yticklabels']!r})")
    if A.get("xlim"):
        s.append(f"    ax.set_xlim({float(A['xlim'][0])!r}, {float(A['xlim'][1])!r})")
    if A.get("ylim"):
        s.append(f"    ax.set_ylim({float(A['ylim'][0])!r}, {float(A['ylim'][1])!r})")
    if A.get("xscale") and A["xscale"] != "linear":
        s.append(f"    ax.set_xscale({A['xscale']!r})")
    if A.get("yscale") and A["yscale"] != "linear":
        s.append(f"    ax.set_yscale({A['yscale']!r})")
    s.append(f"    ax.set_xlabel({A.get('xlabel', '')!r}, visible={A.get('xlabel_visible', True)})")
    s.append(f"    ax.set_ylabel({A.get('ylabel', '')!r}, visible={A.get('ylabel_visible', True)})")
    sp = A.get("spines") or {}
    vis = {k: bool(sp.get(k, True)) for k in ("top", "right", "bottom", "left")}
    s.append(f"    for _s, _v in {vis!r}.items():")
    s.append("        try: ax.spines[_s].set_visible(_v)")
    s.append("        except KeyError: pass")
    ax_ = [f"labelbottom={A.get('labelbottom', True)!r}", f"labelleft={A.get('labelleft', True)!r}"]
    if A.get("tick_labelsize"):
        ax_.append(f"labelsize={A['tick_labelsize']!r}")
    s.append("    ax.tick_params(" + ", ".join(ax_) + ")")


def _codegen_artist(art):
    t = art["type"]
    if t == "vline":
        return f'ax.axvline({float(art["x"])!r}, color={art.get("color", "#000000")!r}, ls={art.get("ls", "-")!r}, lw={art.get("lw", 1.0)!r}, alpha={art.get("alpha", 1.0)!r}, zorder={art.get("zorder", 2)!r})'
    if t == "hline":
        return f'ax.axhline({float(art["y"])!r}, color={art.get("color", "#000000")!r}, ls={art.get("ls", "-")!r}, lw={art.get("lw", 1.0)!r}, alpha={art.get("alpha", 1.0)!r}, zorder={art.get("zorder", 2)!r})'
    if t == "text":
        base = f'ax.text({float(art["x"])!r}, {float(art["y"])!r}, {art.get("text", "")!r}'
        if art.get("axes_frac"):
            base += ", transform=ax.transAxes"
        base += f', ha={art.get("ha", "left")!r}, va={art.get("va", "bottom")!r}, fontsize={art.get("fontsize", 10)!r}, color={art.get("color", "#000000")!r})'
        return base
    if t == "legend":
        h = ", ".join(f'Line2D([], [], color={x.get("color", "#000")!r}, ls={x.get("ls", "-")!r}, lw={x.get("lw", 1.5)!r}, marker={x.get("marker", "")!r}, ms={x.get("ms", 0)!r})' for x in art.get("handles", []))
        return f'ax.legend([{h}], {art.get("labels", [])!r}, loc={art.get("loc", "best")!r}, ncol={art.get("ncol", 1)!r}, frameon={art.get("frameon", True)!r}, fontsize={art.get("fontsize", 9)!r})'
    if t == "annotation":
        return (f'ax.annotate({art.get("text", "")!r}, xy=({float(art["xy"][0])!r}, {float(art["xy"][1])!r}), '
                f'xytext=({float(art["xytext"][0])!r}, {float(art["xytext"][1])!r}), textcoords="axes fraction", '
                f'arrowprops=dict(arrowstyle="->", color={art.get("color", "#000000")!r}, lw={art.get("lw", 1.2)!r}), '
                f'color={art.get("color", "#000000")!r}, fontsize={art.get("fontsize", 10)!r})')
    if t == "box":
        return (f'ax.add_patch(Rectangle(({float(art["x0"])!r}, {float(art["y0"])!r}), {float(art["w"])!r}, {float(art["h"])!r}, '
                f'transform=ax.transAxes, fc={art.get("fc", "none")!r}, alpha={art.get("alpha", 0.15)!r}, '
                f'ec={art.get("ec", "#000000")!r}, lw={art.get("lw", 1.0)!r}))')
    if t == "vspan":
        return f'ax.axvspan({float(art["x0"])!r}, {float(art["x1"])!r}, color={art.get("color", "#b03a2e")!r}, alpha={art.get("alpha", 0.12)!r}, lw=0, zorder={art.get("zorder", 0)!r})'
    if t == "circle":
        return f'ax.add_patch(Circle(({float(art["x"])!r}, {float(art["y"])!r}), {float(art.get("r", 0.1))!r}, fill={art.get("fill", False)!r}, color={art.get("color", "#000000")!r}, lw={art.get("lw", 1.0)!r}, alpha={art.get("alpha", 1.0)!r}, zorder={art.get("zorder", 3)!r}))'
    if t == "ellipse":
        return f'ax.add_patch(Ellipse(({float(art["x"])!r}, {float(art["y"])!r}), {float(art.get("w", 0.2))!r}, {float(art.get("h", 0.1))!r}, angle={art.get("angle", 0)!r}, fill={art.get("fill", False)!r}, color={art.get("color", "#000000")!r}, lw={art.get("lw", 1.0)!r}, alpha={art.get("alpha", 1.0)!r}, zorder={art.get("zorder", 3)!r}))'
    if t == "polygon":
        pts = [[float(a), float(b)] for a, b in art.get("points", [[0, 0], [1, 0], [1, 1]])]
        return f'ax.add_patch(Polygon({pts!r}, closed=True, fill={art.get("fill", False)!r}, color={art.get("color", "#000000")!r}, lw={art.get("lw", 1.0)!r}, alpha={art.get("alpha", 1.0)!r}, zorder={art.get("zorder", 3)!r}))'
    if t == "arrow":
        return f'ax.annotate("", xy=({float(art.get("x1", 1))!r}, {float(art.get("y1", 1))!r}), xytext=({float(art.get("x0", 0))!r}, {float(art.get("y0", 0))!r}), arrowprops=dict(arrowstyle="-|>", color={art.get("color", "#000000")!r}, lw={art.get("lw", 1.5)!r}))'
    return f"pass  # artist type {t!r} not emitted"


def _codegen_figartist(fa):
    t = fa["type"]
    if t == "legend":
        h = ", ".join(f'Line2D([], [], color={x.get("color", "#000")!r}, ls={x.get("ls", "-")!r}, lw={x.get("lw", 1.5)!r}, marker={x.get("marker", "")!r}, ms={x.get("ms", 0)!r})' for x in fa.get("handles", []))
        loc = fa.get("loc")
        if isinstance(loc, list):
            return f'fig.legend([{h}], {fa.get("labels", [])!r}, bbox_to_anchor=({float(loc[0])!r}, {float(loc[1])!r}), loc="center", ncol={fa.get("ncol", 1)!r}, frameon={fa.get("frameon", True)!r}, fontsize={fa.get("fontsize", 10)!r})'
        return f'fig.legend([{h}], {fa.get("labels", [])!r}, loc={loc!r}, ncol={fa.get("ncol", 1)!r}, frameon={fa.get("frameon", True)!r}, fontsize={fa.get("fontsize", 10)!r})'
    if t == "text":
        return f'fig.text({float(fa["x"])!r}, {float(fa["y"])!r}, {fa.get("text", "")!r}, ha={fa.get("ha", "left")!r}, va={fa.get("va", "bottom")!r}, fontsize={fa.get("fontsize", 12)!r}, color={fa.get("color", "#000000")!r}, rotation={fa.get("rotation", 0)!r})'
    if t == "suptitle":
        return f'fig.suptitle({fa.get("text", "")!r}, x={fa.get("x", 0.5)!r}, y={fa.get("y", 1.0)!r}, fontsize={fa.get("fontsize", 14)!r}, color={fa.get("color", "#000000")!r})'
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

    axes_list = [ax for ax in fig.axes if _is_data_axes(ax)]
    for i, p in enumerate(spec["plots"]):
        if i >= len(axes_list):
            break
        ax = axes_list[i]
        add(f"plot:{p['id']}", ax)
        if p["title"].get("visible"):
            add(f"title:{p['id']}", ax.title)
    # per-plot artists / overlaid lines are selected from the Layers list (robust),
    # not by canvas hit-testing (ax.lines is polluted by extra lines + error bars).
    # fig.texts also holds the suptitle (Figure.suptitle appends to it), so exclude it
    # or the text index mapping shifts by one whenever a suptitle is present.
    fig_texts = [t for t in fig.texts if t is not getattr(fig, "_suptitle", None)]
    leg_i = txt_i = 0
    for j, fa in enumerate(spec["figure_artists"]):
        if fa["type"] == "legend":
            a = fig.legends[leg_i] if leg_i < len(fig.legends) else None
            leg_i += 1
        elif fa["type"] == "suptitle":
            a = fig._suptitle
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

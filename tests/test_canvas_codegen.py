"""Regression tests for codegen edge cases in figfoundry/canvas.py.

Every case here used to crash the *generated* code (or leaked a figure /
mis-mapped a hotspot) on a spec the UI can produce: missing signals, 1-D data
in 2-D slots, non-positive pie values, extra lines, and render failures.
"""
import hashlib
import io
import os

import matplotlib.pyplot as plt
import pytest

from figfoundry import canvas


def _md5(fig):
    fig.set_dpi(canvas.PREVIEW_DPI)
    fig.canvas.draw()
    buf = io.BytesIO()
    fig.canvas.print_figure(buf, dpi=canvas.PREVIEW_DPI)
    plt.close(fig)
    return hashlib.md5(buf.getvalue()).hexdigest()


def _spec(plots, figarts=None, dataset="sample.npz"):
    spec = canvas.blank_spec()
    spec["dataset"] = dataset
    spec["plots"] = plots
    spec["figure_artists"] = figarts or []
    return spec


def _plot(pid, ptype, **data):
    p = canvas._new_plot(pid, [0.1, 0.15, 0.8, 0.7], data.get("x"), data.get("y"))
    p["type"] = ptype
    for k in ("z", "field", "band", "errorbar"):
        if k in data:
            p["data"][k] = data[k]
    return p


def _export_md5(spec, tmp_path):
    """Render the standalone export the same way a user would (exec the .py)."""
    out = tmp_path / "export.py"
    out.write_text(canvas.codegen_canvas(spec), encoding="utf-8")
    ns = {"__file__": str(out)}
    exec(compile(out.read_text(encoding="utf-8"), str(out), "exec"), ns)
    return _md5(ns["build"]())


# ---------------------------------------------------------------- extra lines
def test_extra_lines_render(data_dir, tmp_path):
    """A line plot with extra lines: the emitted call is valid Python (used to be
    missing its closing paren -> SyntaxError in every build) and WYSIWYG holds."""
    p = _plot("p0", "line", x="@x", y="@y")
    p["extra_lines"] = [{"ref": "@s", "label": "se"}, {"ref": "@se", "color": "#0072B2"}]
    spec = _spec([p])
    compile(canvas.codegen_canvas(spec), "<gen>", "exec")  # no SyntaxError
    assert _md5(canvas.build_figure(spec)) == _export_md5(spec, tmp_path)


def test_extra_lines_x_none(data_dir):
    """Extra lines on an index-plotted (x=None) line: fall back to the index x."""
    p = _plot("p0", "line", y="@y")
    p["extra_lines"] = [{"ref": "@s"}]
    fig = canvas.build_figure(_spec([p]))
    plt.close(fig)


# ---------------------------------------------------------- band / errorbar
def test_band_guard_x_none(data_dir):
    """The band shape-guard must not evaluate `None.shape` when x is absent, and a
    matching band must still draw (fill_between needs the index x)."""
    p = _plot("p0", "line", y="@y", band="@se")  # se (120) matches y (120)
    fig = canvas.build_figure(_spec([p]))
    assert len(fig.axes[0].collections) == 1  # band drew
    plt.close(fig)

    p = _plot("p0", "line", y="@y", band="@slices")  # 5 != 120 -> skip, don't break
    fig = canvas.build_figure(_spec([p]))
    assert len(fig.axes[0].collections) == 0
    plt.close(fig)


def test_errorbar_guard_x_none(data_dir):
    p = _plot("p0", "line", y="@y", errorbar={"ref": "@se"})
    fig = canvas.build_figure(_spec([p]))  # matching: draws
    plt.close(fig)

    p = _plot("p0", "line", y="@y", errorbar={"ref": "@slices"})  # mismatched: skip
    fig = canvas.build_figure(_spec([p]))
    plt.close(fig)


# ----------------------------------------------------------------------- pie
def test_pie_skips_nonpositive(data_dir, tmp_path):
    """pie() raises on negative wedges; the guard skips instead of breaking, and
    positive pies still render (WYSIWYG)."""
    p = _plot("p0", "pie", z="@y")  # sin: has negatives
    spec = _spec([p])
    fig = canvas.build_figure(spec)
    assert len(fig.axes[0].patches) == 0  # skipped
    plt.close(fig)
    assert _md5(canvas.build_figure(spec)) == _export_md5(spec, tmp_path)

    p = _plot("p0", "pie", z="@slices")
    fig = canvas.build_figure(_spec([p]))
    assert len(fig.axes[0].patches) == 5
    plt.close(fig)


# ---------------------------------------------------------- figure leaks
def test_render_no_leak_when_build_fails(data_dir):
    spec = _spec([_plot("p0", "line", x="@x", y="@y")], dataset="missing.npz")
    before = len(plt.get_fignums())
    with pytest.raises(FileNotFoundError):
        canvas.render_to_png(spec)
    assert len(plt.get_fignums()) == before


def test_render_no_leak_when_save_fails(data_dir, tmp_path):
    spec = _spec([_plot("p0", "line", x="@x", y="@y")])
    before = len(plt.get_fignums())
    with pytest.raises(ValueError):
        canvas.render_to_bytes(spec, fmt="bmp")  # unsupported format
    assert len(plt.get_fignums()) == before


# ------------------------------------------------------------ hotspots
def test_hotspots_text_mapping_with_suptitle(data_dir):
    """fig.texts also holds the suptitle, so the text index used to shift by one:
    the text hotspot must match the text artist, not the suptitle."""
    spec = _spec([_plot("p0", "line", x="@x", y="@y")],
                 figarts=[{"type": "suptitle", "text": "SUP"},
                          {"type": "text", "x": 0.05, "y": 0.05, "text": "MYTEXT"}])
    fig = canvas.build_figure(spec)
    fig.canvas.draw()
    hs = canvas.hotspots_from_fig(fig, spec)
    rend = fig.canvas.get_renderer()
    img_h = round(fig.get_figheight() * fig.dpi)
    mytext = next(t for t in fig.texts if t.get_text() == "MYTEXT")
    b = mytext.get_window_extent(rend)
    expected = {"id": "figartist:1", "x": round(b.x0, 1), "top": round(img_h - b.y1, 1),
                "w": round(b.width, 1), "h": round(b.height, 1)}
    got = next(h for h in hs if h["id"] == "figartist:1")
    plt.close(fig)
    assert got == expected


def test_hotspots_exclude_colorbars_3d_polar(data_dir):
    """Axes index mapping stays aligned with colorbars, 3-D and polar axes present."""
    p_im = _plot("a", "imshow", field="@M")
    p_im["style"]["colorbar"] = True
    p_3d = _plot("b", "scatter-3d", x="@x", y="@y", z="@s")
    p_pol = _plot("c", "polar", x="@x", y="@y")
    p_2d = _plot("d", "line", x="@x", y="@y")
    fig = canvas.build_figure(_spec([p_im, p_3d, p_pol, p_2d]))
    fig.canvas.draw()
    ids = [h["id"] for h in canvas.hotspots_from_fig(fig, _spec([p_im, p_3d, p_pol, p_2d]))]
    plt.close(fig)
    assert ids == ["plot:a", "plot:b", "plot:c", "plot:d"]


# -------------------------------------------- 2-D slots with 1-D / missing data
@pytest.mark.parametrize("ptype,data", [
    ("surface", {"field": "@y"}),
    ("wireframe", {"field": "@y"}),
    ("imshow", {"field": "@y"}),
    ("contourf", {"field": "@y"}),
    ("pcolormesh", {"field": "@y"}),
    ("streamplot", {"x": "@x", "y": "@y"}),
    ("decision-boundary", {"field": "@y", "x": "@x", "y": "@y", "z": "@group"}),
])
def test_2d_types_skip_1d_field(data_dir, ptype, data):
    """A 1-D signal in a 2-D slot skips the data call instead of crashing
    (IndexError / 'Input z must be 2D')."""
    fig = canvas.build_figure(_spec([_plot("p0", ptype, **data)]))
    plt.close(fig)


def test_boxplot_2d_group_fallback(data_dir):
    """2-D group rows are unhashable; fall back to the ungrouped boxplot."""
    fig = canvas.build_figure(_spec([_plot("p0", "boxplot", x="@M", y="@y")]))
    plt.close(fig)


# ------------------------------------------------------ index fallbacks
@pytest.mark.parametrize("ptype,data", [
    ("step", {"y": "@y"}),          # step() needs an explicit x
    ("scatter", {"y": "@y"}),       # scatter() needs an explicit x
    ("bar", {"y": "@y"}),
    ("polar", {"y": "@y"}),
    ("hist2d", {"y": "@y"}),
    ("hexbin", {"y": "@y"}),
    ("scatter-grouped", {"y": "@y", "z": "@group"}),
])
def test_x_none_index_fallback(data_dir, ptype, data):
    """x=None plots fall back to the 0..n-1 index and render."""
    fig = canvas.build_figure(_spec([_plot("p0", ptype, **data)]))
    plt.close(fig)


# ------------------------------------------------------- csv loader
def test_empty_csv_loader(data_dir, tmp_path):
    """An empty (header-less) CSV is a valid 0-signal dataset; the emitted loader
    used to crash on `_rows[0]`."""
    path = os.path.join(data_dir, "empty.csv")
    with open(path, "w", encoding="utf-8") as f:
        f.write("")
    spec = _spec([], dataset="empty.csv")
    out = tmp_path / "export.py"
    out.write_text(canvas.codegen_canvas(spec), encoding="utf-8")
    ns = {"__file__": str(out)}
    exec(compile(out.read_text(encoding="utf-8"), str(out), "exec"), ns)
    assert ns["data"] == {}
    plt.close(ns["build"]())


# ------------------------------------------------------ WYSIWYG overall
def test_wysiwyg_guarded_mixed_scene(data_dir, tmp_path):
    """A scene exercising several guards at once: preview and export agree."""
    p1 = _plot("a", "line", x="@x", y="@y", band="@slices")  # mismatched band -> skipped
    p1["extra_lines"] = [{"ref": "@s"}]
    p2 = _plot("b", "pie", z="@y")                            # negatives -> skipped
    p3 = _plot("c", "surface", field="@y")                    # 1-D field -> skipped
    p4 = _plot("d", "step", y="@y")                           # x=None -> index
    spec = _spec([p1, p2, p3, p4])
    assert _md5(canvas.build_figure(spec)) == _export_md5(spec, tmp_path)

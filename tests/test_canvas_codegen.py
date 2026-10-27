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

# Low DPI for the WYSIWYG byte-comparison (holds at any DPI; see test_wysiwyg).
WYSIWYG_DPI = 72


def _md5(fig):
    fig.set_dpi(WYSIWYG_DPI)
    fig.canvas.draw()
    buf = io.BytesIO()
    fig.canvas.print_figure(buf, dpi=WYSIWYG_DPI)
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


# ---------------------------------------------------------------- mathtext + rotation
def test_mathtext_and_rotation_pass_through(data_dir):
    """Text fields carry LaTeX mathtext ($...$) and rotation through to the built
    figure; nothing strips or escapes them."""
    p = _plot("p0", "line", x="@x", y="@y")
    p["title"] = {"text": "$\\alpha$ title", "visible": True, "rotation": 30, "x": 0.5, "y": 1.0, "pad": 1.0,
                  "fontsize": 11, "weight": "normal", "color": "#000"}
    spec = _spec([p], figarts=[{"type": "text", "x": 0.5, "y": 0.5, "text": "$x = ax + b$",
                                "ha": "center", "va": "center", "fontsize": 12, "color": "#000", "rotation": 90}])
    code = canvas.codegen_canvas(spec)
    compile(code, "<gen>", "exec")  # valid Python
    fig = canvas.build_figure(spec)
    try:
        assert "$\\alpha$ title" in [ax.title.get_text() for ax in fig.axes]   # mathtext intact in the title
        assert "$x = ax + b$" in [t.get_text() for t in fig.texts]             # mathtext intact in a figure text
        assert 90 in [t.get_rotation() for t in fig.texts]                     # rotation applied
    finally:
        plt.close(fig)
    assert "set_rotation(30" in code and "rotation=90" in code                 # and in the exported source


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


# ------------------------------------------------------------ draft vs full
def test_render_to_png_draft_vs_full(data_dir):
    """draft=True: JPEG, no hotspots; draft=False: PNG + hit-test hotspots. Both
    report the display size at the reference PREVIEW_DPI (the draft is stretched)."""
    import base64

    spec = _spec([_plot("p0", "line", x="@x", y="@y")])
    full = canvas.render_to_png(spec)
    assert full["mime"] == "image/png" and full["draft"] is False
    assert base64.b64decode(full["img"])[:8] == b"\x89PNG\r\n\x1a\n"
    assert full["img_w"] == round(7.4 * canvas.PREVIEW_DPI)
    assert full["img_h"] == round(4.5 * canvas.PREVIEW_DPI)
    assert any(h["id"] == "plot:p0" for h in full["hotspots"])
    draft = canvas.render_to_png(spec, draft=True)
    assert draft["mime"] == "image/jpeg" and draft["draft"] is True
    assert draft["hotspots"] == []
    assert base64.b64decode(draft["img"])[:2] == b"\xff\xd8"
    assert (draft["img_w"], draft["img_h"]) == (full["img_w"], full["img_h"])


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


# ------------------------------------------------- dataset cache staleness
def test_dataset_cache_invalidated_on_reupload(data_dir):
    """Re-uploading a dataset under the same name must not keep serving the
    cached (stale) arrays: the render re-reads the file from disk, so the signal
    library has to track it. Keyed on (mtime, size), one entry per filename."""
    import numpy as np

    p = os.path.join(data_dir, "swap.npz")
    np.savez(p, a=np.arange(5.0))
    assert canvas.get_dataset_arrays("swap.npz")["a"].sum() == 10.0
    # replace the file with different data (same name); nudge mtime so the
    # invalidation key definitely changes
    np.savez(p, a=np.arange(100.0))
    now = os.stat(p).st_mtime + 5
    os.utime(p, (now, now))
    assert canvas.get_dataset_arrays("swap.npz")["a"].sum() == 4950.0
    # a deletion must not be masked by the cache either
    os.remove(p)
    with pytest.raises(FileNotFoundError):
        canvas.get_dataset_arrays("swap.npz")


# ------------------------------------- mismatched signal lengths (all types)
@pytest.mark.parametrize("ptype,data", [
    ("line", {"x": "@x", "y": "@slices"}),          # 120 vs 5
    ("step", {"x": "@x", "y": "@slices"}),
    ("scatter", {"x": "@x", "y": "@slices"}),
    ("bar", {"x": "@x", "y": "@slices"}),
    ("barh", {"x": "@slices", "y": "@x"}),          # values vs categories
    ("polar", {"x": "@x", "y": "@slices"}),
    ("hist2d", {"x": "@x", "y": "@slices"}),
    ("hexbin", {"x": "@x", "y": "@slices"}),
    ("quiver", {"x": "@x", "y": "@slices"}),        # 1-D U/V length mismatch
    ("scatter-grouped", {"x": "@x", "y": "@y", "z": "@slices"}),
    ("decision-boundary", {"field": "@M", "x": "@x", "y": "@y", "z": "@slices"}),
    ("scatter-3d", {"x": "@x", "y": "@y", "z": "@slices"}),
])
def test_mismatched_lengths_skip_not_crash(data_dir, ptype, data):
    """The UI lets any signal drop into any slot; mismatched x/y (or z) lengths
    used to raise inside the generated code (ValueError). Now the data call is
    skipped and the panel stays blank."""
    fig = canvas.build_figure(_spec([_plot("p0", ptype, **data)]))
    plt.close(fig)


def test_streamplot_shape_mismatch_skips(data_dir):
    """Two 2-D U/V fields of different shapes used to crash streamplot."""
    import numpy as np
    p = os.path.join(data_dir, "vec.npz")
    np.savez(p, U=np.ones((16, 16)), V=np.ones((16, 17)))
    spec = _spec([_plot("p0", "streamplot", x="@U", y="@V")], dataset="vec.npz")
    fig = canvas.build_figure(spec)
    plt.close(fig)


def test_quiver_2d_draws_vector_field(data_dir):
    """quiver uses the 2-arg form quiver(U, V): the x/y slots hold the U/V fields
    (per the UI labels) and must produce a real Quiver, not zero vectors."""
    from matplotlib.quiver import Quiver
    fig = canvas.build_figure(_spec([_plot("p0", "quiver", x="@M", y="@M")]))
    assert any(isinstance(c, Quiver) for c in fig.axes[0].collections)
    plt.close(fig)


# ------------------------------------------------ all-NaN / degenerate data
def test_hist_all_nan_skips(data_dir):
    """hist() raises 'range not finite' on all-NaN (or +-inf) data."""
    import numpy as np
    p = os.path.join(data_dir, "nan.npz")
    np.savez(p, nan=np.full(20, np.nan), inf=np.full(20, np.inf))
    for sig in ("@nan", "@inf"):
        spec = _spec([_plot("p0", "histogram", y=sig)], dataset="nan.npz")
        fig = canvas.build_figure(spec)
        plt.close(fig)
    spec = _spec([_plot("p0", "hist2d", x="@nan", y="@inf")], dataset="nan.npz")
    fig = canvas.build_figure(spec)
    plt.close(fig)


def test_contour_min_shape_skips(data_dir):
    """contour/contourf need at least a (2, 2) field; a (1, 5) row used to raise
    TypeError."""
    import numpy as np
    p = os.path.join(data_dir, "thin.npz")
    np.savez(p, row=np.ones((1, 5)), col=np.ones((5, 1)),
             x=np.arange(30.0), y=np.arange(30.0) * 0.1, group=np.repeat([0, 1], 15))
    for field in ("@row", "@col"):
        for ptype in ("contourf", "contour"):
            spec = _spec([_plot("p0", ptype, field=field)], dataset="thin.npz")
            fig = canvas.build_figure(spec)
            assert len(fig.axes[0].collections) == 0
            plt.close(fig)
    # decision-boundary's contourf layer takes the same guard
    spec = _spec([_plot("p0", "decision-boundary", field="@row", x="@x", y="@y", z="@group")],
                 dataset="thin.npz")
    fig = canvas.build_figure(spec)
    plt.close(fig)


def test_hotspots_align_with_skipped_plot(data_dir):
    """A malformed plot in the middle (bad rect -> codegen skips its add_axes)
    must not shift the axes index: plot c's bbox is c's, not b's."""
    p_b = _plot("b", "line", x="@x", y="@y")
    p_b["rect"] = "junk"  # codegen skips this plot entirely
    p_c = _plot("c", "line", x="@x", y="@y")
    p_c["rect"] = [0.55, 0.55, 0.3, 0.3]
    spec = _spec([_plot("a", "line", x="@x", y="@y"), p_b, p_c])
    fig = canvas.build_figure(spec)
    fig.canvas.draw()
    hs = canvas.hotspots_from_fig(fig, spec)
    plt.close(fig)
    got = {h["id"]: h for h in hs}
    assert set(got) == {"plot:a", "plot:c"}
    # plot:c must map to the second real axes, whose left edge is 0.55 in
    # figure fraction -- clearly right of plot:a's 0.1
    assert got["plot:c"]["x"] > got["plot:a"]["x"] + 0.2 * fig.get_figwidth() * fig.dpi


# --------------------------------------------- malformed axes / spec fields
def test_malformed_ticks_limits_do_not_crash(data_dir):
    """xticks/yticks/xlim/ylim with junk values used to raise at codegen time
    (500 on every endpoint) or exec time (ConversionError). They are sanitized to
    finite floats; unusable settings are dropped."""
    p = _plot("p0", "line", x="@x", y="@y")
    p["axes"].update({"xticks": ["a", "b"], "yticks": 42, "xlim": [0], "ylim": "junk",
                      "xscale": "weird", "tick_labelsize": "big"})
    spec = _spec([p])
    code = canvas.codegen_canvas(spec)  # no raise
    compile(code, "<gen>", "exec")
    assert "set_xticks" not in code and "set_xlim" not in code
    fig = canvas.build_figure(spec)
    plt.close(fig)


def test_malformed_client_spec_degrades_to_blanks(data_dir):
    """POST /render accepts a client-controlled spec: missing style/axes/title,
    junk canvas numbers, an unknown plot type, a bad rect, and half-formed
    artists must blank out, not 500 the whole render."""
    spec = canvas.blank_spec()
    spec["dataset"] = "sample.npz"
    spec["canvas"] = {"width": "abc", "height": None, "dpi": "junk", "font_size": "big"}
    spec["plots"] = [
        {"id": "p0", "rect": [0.1, 0.1, 0.8, 0.8], "type": "line",
         "data": {"x": "@x", "y": "@y"}},  # no style/axes/title -> defaults
        {"id": "p1", "rect": "junk", "type": "weird"},  # bad rect, unknown type
        {"id": "p2", "rect": [0.1, 0.6, 0.8, 0.3], "type": "line", "data": {"y": "@y"},
         "artists": [{"type": "vline"}, {"type": "text", "x": 0.5}]},  # missing fields
    ]
    spec["figure_artists"] = [{"type": "text"}, {"type": "suptitle", "text": "T"}]
    code = canvas.codegen_canvas(spec)
    compile(code, "<gen>", "exec")
    fig = canvas.build_figure(spec)
    # p0 and p2 render; p1 is skipped; the suptitle survives the bad fig text
    assert len(fig.axes) == 2
    assert fig._suptitle is not None
    plt.close(fig)


# ----------------------------------------------------- pkl single-array
def test_pkl_single_array_dataset(data_dir):
    """A pickle of a bare ndarray is accepted at upload (data_io maps it to
    {'data': ...}); the emitted loader used to crash on .items()."""
    import pickle

    import numpy as np
    p = os.path.join(data_dir, "bare.pkl")
    with open(p, "wb") as f:
        pickle.dump(np.arange(10.0), f)
    spec = _spec([_plot("p0", "line", y="@data")], dataset="bare.pkl")
    fig = canvas.build_figure(spec)
    assert len(fig.axes[0].lines) == 1
    plt.close(fig)


# ------------------------------------------------- WYSIWYG on new guards
def test_wysiwyg_mismatch_and_nan_guards(data_dir, tmp_path):
    """The new length/NaN/shape guards live in the generated code, so preview and
    export stay byte-identical on a scene that trips all of them."""
    import numpy as np
    p = os.path.join(data_dir, "guard.npz")
    np.savez(p, x=np.arange(120.0), y=np.sin(np.arange(120.0)),
             nan=np.full(120, np.nan), row=np.ones((1, 5)), slices=np.array([3.0, 1, 4, 1, 5]))
    p1 = _plot("a", "line", x="@x", y="@slices")   # mismatch -> skipped
    p2 = _plot("b", "histogram", y="@nan")          # all-NaN -> skipped
    p3 = _plot("c", "contourf", field="@row")       # thin field -> skipped
    p4 = _plot("d", "bar", x="@x", y="@y")          # fine
    spec = _spec([p1, p2, p3, p4], dataset="guard.npz")
    assert _md5(canvas.build_figure(spec)) == _export_md5(spec, tmp_path)

"""0.4.0 spec features and fixes: aspect on contour/pcolormesh, export dpi ==
script dpi, portable data path, warnings + schema_version, empty tick lists,
vmin/vmax/norm, id/tag, data-coordinate box/text, cell_marks, drawn plot
bboxes, tick_params, signal slicing, text stroke and layout warnings."""
import hashlib
import io
import os
import shutil
import subprocess
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pytest

from figfoundry import canvas


def _spec(plots, figarts=None):
    spec = canvas.blank_spec()
    spec["dataset"] = "sample.npz"
    spec["plots"] = plots
    spec["figure_artists"] = figarts or []
    return spec


def _plot(pid, ptype, rect=(0.1, 0.15, 0.8, 0.7), **data):
    p = canvas._new_plot(pid, list(rect), data.get("x"), data.get("y"))
    p["type"] = ptype
    for k in ("z", "field", "band", "errorbar"):
        if k in data:
            p["data"][k] = data[k]
    return p


def _md5_png(fig, dpi=72):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi)
    plt.close(fig)
    return hashlib.md5(buf.getvalue()).hexdigest()


def _exec_export(spec, where):
    """Write the generated .py into `where` and exec it like an import would."""
    out = Path(where) / "export.py"
    out.write_text(canvas.codegen_canvas(spec), encoding="utf-8")
    ns = {"__file__": str(out), "__name__": "exported"}
    exec(compile(out.read_text(encoding="utf-8"), str(out), "exec"), ns)
    return ns


# ------------------------------------------------------------------ aspect
@pytest.mark.parametrize("ptype", ["contour", "contourf", "pcolormesh"])
def test_aspect_uses_set_aspect_not_kwarg(data_dir, ptype):
    p = _plot("p0", ptype, field="@M")
    p["style"]["aspect"] = "equal"
    code = canvas.codegen_canvas(_spec([p]))
    assert "aspect=" not in code.split("ax.set_aspect")[0].split(f"ax.{ptype}(")[1].split("\n")[0]
    assert "ax.set_aspect('equal')" in code
    fig = canvas.build_figure(_spec([p]))
    assert fig.axes[0].get_aspect() == 1.0
    plt.close(fig)


@pytest.mark.parametrize("ptype", ["contour", "contourf", "pcolormesh"])
def test_aspect_unset_keeps_matplotlib_default(data_dir, ptype):
    """No aspect in the spec -> no set_aspect call (their default is 'auto'), so
    existing specs generate the same drawing as before."""
    code = canvas.codegen_canvas(_spec([_plot("p0", ptype, field="@M")]))
    assert "set_aspect" not in code


def test_imshow_aspect_auto_and_junk(data_dir):
    p = _plot("p0", "imshow", field="@M")
    p["style"]["aspect"] = "auto"
    assert "aspect='auto'" in canvas.codegen_canvas(_spec([p]))
    p["style"]["aspect"] = "bogus"  # junk must not break the render
    code, warns = canvas.codegen_canvas_full(_spec([p]))
    assert "aspect='equal'" in code and any("aspect" in w for w in warns)
    plt.close(canvas.build_figure(_spec([p])))


# ------------------------------------------------------------------ dpi
def test_export_dpi_matches_script_default(data_dir, tmp_path):
    """GET /figure (render_to_bytes) and the generated script's default output
    are the same file: both use the canvas dpi."""
    spec = _spec([_plot("p0", "imshow", field="@M")])
    spec["canvas"]["dpi"] = 97
    assert canvas.canvas_dpi(spec) == 97
    code = canvas.codegen_canvas(spec)
    assert 'ap.add_argument("--dpi", type=int, default=97)' in code
    server_png = canvas.render_to_bytes(spec, fmt="png")
    script = Path(data_dir) / "fig_script.py"
    script.write_text(code, encoding="utf-8")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    subprocess.run([sys.executable, str(script), "--format", "png"], cwd=run_dir, check=True)
    assert (run_dir / "fig.png").read_bytes() == server_png


def test_render_to_bytes_defaults_to_canvas_dpi(data_dir):
    spec = _spec([_plot("p0", "line", x="@x", y="@y")])
    spec["canvas"].update({"width": 2.0, "height": 1.0, "dpi": 120})
    png = canvas.render_to_bytes(spec, fmt="png")
    img = plt.imread(io.BytesIO(png))
    assert img.shape[:2] == (120, 240)


# ------------------------------------------------------------------ portable data path
def test_generated_script_has_no_absolute_data_path(data_dir):
    code = canvas.codegen_canvas(_spec([_plot("p0", "line", x="@x", y="@y")]))
    assert data_dir not in code and os.path.expanduser("~") not in code
    assert "DATA_FILE = 'sample.npz'" in code and "Path(__file__)" in code


def test_script_finds_dataset_next_to_itself(data_dir, tmp_path):
    """Copy script + dataset anywhere: it runs with no --data."""
    spec = _spec([_plot("p0", "line", x="@x", y="@y")])
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    shutil.copy(os.path.join(data_dir, "sample.npz"), bundle / "sample.npz")
    (bundle / "fig.py").write_text(canvas.codegen_canvas(spec), encoding="utf-8")
    subprocess.run([sys.executable, "fig.py", "--format", "pdf", "--out", "out.pdf"], cwd=bundle, check=True)
    assert (bundle / "out.pdf").read_bytes()[:4] == b"%PDF"


def test_script_data_override_and_missing_message(data_dir, tmp_path):
    spec = _spec([_plot("p0", "line", x="@x", y="@y")])
    lone = tmp_path / "lone"
    lone.mkdir()
    (lone / "fig.py").write_text(canvas.codegen_canvas(spec), encoding="utf-8")
    r = subprocess.run([sys.executable, "fig.py"], cwd=lone, capture_output=True, text=True)
    assert r.returncode != 0 and "--data" in (r.stderr + r.stdout)
    r = subprocess.run([sys.executable, "fig.py", "--data", os.path.join(data_dir, "sample.npz")], cwd=lone)
    assert r.returncode == 0 and (lone / "fig.png").exists()


def test_build_figure_with_external_data_dir(data_dir, tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    np.savez(other / "mine.npz", a=np.arange(5.0), b=np.arange(5.0) ** 2)
    spec = _spec([_plot("p0", "line", x="@a", y="@b")])
    spec["dataset"] = "mine.npz"
    fig = canvas.build_figure(spec, data_dir=str(other))
    assert len(fig.axes[0].lines) == 1
    plt.close(fig)


# ------------------------------------------------------------------ warnings + schema
def test_warnings_unknown_types_and_keys(data_dir):
    p = _plot("p0", "sparkline", x="@x", y="@y")
    p["style"]["colour"] = "red"
    p["artists"] = [{"type": "starburst", "x": 1}, {"type": "text", "x": 0.5, "y": 0.5, "text": "t", "font": "x"}]
    spec = _spec([p], [{"type": "watermark", "text": "x"}])
    spec["bogus_top"] = 1
    code, warns = canvas.codegen_canvas_full(spec)
    joined = "\n".join(warns)
    assert "unknown plot type 'sparkline'" in joined
    assert "unknown key 'colour'" in joined
    assert "unknown artist type 'starburst'" in joined
    assert "unknown key 'font'" in joined
    assert "unknown figure artist type 'watermark'" in joined
    assert "unknown key 'bogus_top'" in joined
    assert "# WARNING (figfoundry)" in code and "sparkline" in code.split("import matplotlib")[0]
    r = canvas.render_to_png(spec)
    assert any("starburst" in w for w in r["warnings"])


def test_clean_spec_has_no_warnings_and_unchanged_header(data_dir):
    spec = _spec([_plot("p0", "line", x="@x", y="@y")])
    code, warns = canvas.codegen_canvas_full(spec)
    assert warns == [] and "WARNING" not in code
    assert canvas.render_to_png(spec)["warnings"] == []


def test_schema_version_newer_warns(data_dir):
    spec = _spec([])
    assert spec["schema_version"] == canvas.SCHEMA_VERSION
    spec["schema_version"] = canvas.SCHEMA_VERSION + 1
    assert any("newer than this figfoundry" in w for w in canvas.spec_warnings(spec))
    spec["schema_version"] = canvas.SCHEMA_VERSION
    assert canvas.spec_warnings(spec) == []


def test_min_figfoundry_warns(data_dir, monkeypatch):
    monkeypatch.setattr(canvas, "__version__", "0.4.0")
    spec = _spec([])
    spec["min_figfoundry"] = "9.0"
    assert any("needs figfoundry >= 9.0" in w for w in canvas.spec_warnings(spec))
    spec["min_figfoundry"] = "0.3.1"
    assert canvas.spec_warnings(spec) == []


def test_invalid_artist_is_warned(data_dir):
    p = _plot("p0", "line", x="@x", y="@y")
    p["artists"] = [{"type": "vline"}]  # missing x
    _, warns = canvas.codegen_canvas_full(_spec([p]))
    assert any("artist 0 (vline): invalid, skipped" in w for w in warns)


def test_live_fixture_specs_have_no_warnings():
    """The editor's own blank plot uses only known keys."""
    p = canvas._new_plot("p0", [0.1, 0.1, 0.8, 0.8], "@x", "@y")
    spec = canvas.blank_spec()
    spec["plots"] = [p]
    assert canvas.spec_warnings(spec) == []


# ------------------------------------------------------------------ empty tick lists
def test_empty_tick_list_hides_ticks(data_dir):
    p = _plot("p0", "line", x="@x", y="@y")
    p["axes"]["xticks"] = []
    p["axes"]["yticks"] = []
    code = canvas.codegen_canvas(_spec([p]))
    assert "ax.set_xticks([])" in code and "ax.set_yticks([])" in code
    fig = canvas.build_figure(_spec([p]))
    assert list(fig.axes[0].get_xticks()) == [] and list(fig.axes[0].get_yticks()) == []
    plt.close(fig)


def test_auto_and_junk_ticks_still_auto(data_dir):
    p = _plot("p0", "line", x="@x", y="@y")
    for v in ("auto", None, ["a", "b"]):
        p["axes"]["xticks"] = v
        assert "set_xticks" not in canvas.codegen_canvas(_spec([p]))


# ------------------------------------------------------------------ vmin / vmax / norm
@pytest.mark.parametrize("ptype,data", [
    ("imshow", {"field": "@M"}), ("pcolormesh", {"field": "@M"}), ("contourf", {"field": "@M"}),
    ("contour", {"field": "@M"}), ("hexbin", {"x": "@x", "y": "@y"}), ("hist2d", {"x": "@x", "y": "@y"}),
])
def test_vmin_vmax(data_dir, ptype, data):
    p = _plot("p0", ptype, **data)
    p["style"].update({"vmin": -0.5, "vmax": 2.0})
    code = canvas.codegen_canvas(_spec([p]))
    assert "vmin=-0.5, vmax=2.0" in code
    fig = canvas.build_figure(_spec([p]))
    plt.close(fig)


def test_vmin_vmax_imshow_clim(data_dir):
    p = _plot("p0", "imshow", field="@M")
    p["style"].update({"vmin": 0.0, "vmax": 0.5})
    fig = canvas.build_figure(_spec([p]))
    assert fig.axes[0].images[0].get_clim() == (0.0, 0.5)
    plt.close(fig)


@pytest.mark.parametrize("norm", ["log", "symlog"])
def test_norm(data_dir, norm):
    p = _plot("p0", "imshow", field="@M")
    p["style"]["norm"] = norm
    if norm == "log":
        p["style"].update({"vmin": 0.01, "vmax": 1.0})
    assert f"norm='{norm}'" in canvas.codegen_canvas(_spec([p]))
    plt.close(canvas.build_figure(_spec([p])))


def test_color_scale_unset_emits_nothing(data_dir):
    code = canvas.codegen_canvas(_spec([_plot("p0", "imshow", field="@M")]))
    assert "vmin" not in code and "norm=" not in code


# ------------------------------------------------------------------ id / tag
def test_id_and_tag_accepted_and_commented(data_dir):
    p = _plot("p0", "line", x="@x", y="@y")
    p["tag"] = "main"
    p["artists"] = [{"type": "hline", "y": 0, "id": "zero", "tag": "guides"}]
    spec = _spec([p], [{"type": "text", "x": 0.5, "y": 0.5, "text": "t", "tag": "label"}])
    code, warns = canvas.codegen_canvas_full(spec)
    assert warns == []
    assert "# ---- plot p0 ----  tag='main'" in code
    assert "# id='zero', tag='guides'" in code and "# tag='label'" in code
    plt.close(canvas.build_figure(spec))


# ------------------------------------------------------------------ data-coordinate box / text
def test_box_data_coords(data_dir):
    p = _plot("p0", "imshow", field="@M")
    p["artists"] = [{"type": "box", "x0": 1.5, "y0": 2.5, "w": 1, "h": 1, "ec": "#ff0000", "lw": 1, "alpha": 1,
                     "coords": "data"}]
    code = canvas.codegen_canvas(_spec([p]))
    box_line = next(ln for ln in code.splitlines() if "Rectangle((1.5" in ln)
    assert "transAxes" not in box_line
    fig = canvas.build_figure(_spec([p]))
    rect = fig.axes[0].patches[0]
    assert rect.get_data_transform() == fig.axes[0].transData
    plt.close(fig)


def test_box_axes_default_unchanged(data_dir):
    p = _plot("p0", "line", x="@x", y="@y")
    p["artists"] = [{"type": "box", "x0": 0.1, "y0": 0.2, "w": 0.3, "h": 0.4}]
    code = canvas.codegen_canvas(_spec([p]))
    assert ("ax.add_patch(Rectangle((0.1, 0.2), 0.3, 0.4, transform=ax.transAxes, fc='none', alpha=0.15, "
            "ec='#000000', lw=1.0))") in code


def test_text_coords_overrides_axes_frac(data_dir):
    p = _plot("p0", "line", x="@x", y="@y")
    p["artists"] = [{"type": "text", "x": 3, "y": 0, "text": "d", "axes_frac": True, "coords": "data"},
                    {"type": "text", "x": 0.5, "y": 0.5, "text": "a", "coords": "axes"}]
    fig = canvas.build_figure(_spec([p]))
    t_data, t_axes = fig.axes[0].texts
    assert t_data.get_transform() == fig.axes[0].transData
    assert t_axes.get_transform() == fig.axes[0].transAxes
    plt.close(fig)


# ------------------------------------------------------------------ cell_marks
@pytest.mark.parametrize("marker", ["box", "x", "dot"])
def test_cell_marks_render(data_dir, marker):
    p = _plot("p0", "imshow", field="@M")
    p["artists"] = [{"type": "cell_marks", "cells": [[0, 1], [3, 4]], "marker": marker, "color": "#123456",
                     "lw": 1.5, "inset": 0.1}]
    spec = _spec([p])
    _code, warns = canvas.codegen_canvas_full(spec)
    assert warns == []
    fig = canvas.build_figure(spec)
    ax = fig.axes[0]
    if marker == "x":
        segs = [ln.get_xydata() for ln in ax.lines]
        assert len(segs) == 4
        assert np.allclose(segs[0], [[0.6, -0.4], [1.4, 0.4]])  # cell (0, 1) inset 0.1
    else:
        assert len(ax.patches) == 2
        bb = ax.patches[1].get_extents()  # in display coords; compare to the cell in data
        cell = ax.transData.transform([[3.6, 2.6], [4.4, 3.4]])
        assert np.allclose(sorted([bb.x0, bb.x1]), sorted(cell[:, 0]), atol=1.5)
    # the image extent (view) is untouched by the markers
    assert ax.get_xlim() == (-0.5, 15.5)
    plt.close(fig)


def test_cell_marks_pcolormesh_offset(data_dir):
    p = _plot("p0", "pcolormesh", field="@M")
    p["artists"] = [{"type": "cell_marks", "cells": [[2, 3]], "marker": "box", "inset": 0}]
    fig = canvas.build_figure(_spec([p]))
    r = fig.axes[0].patches[0]
    assert (r.get_x(), r.get_y(), r.get_width()) == (3.0, 2.0, 1.0)
    plt.close(fig)


def test_cell_marks_bad_marker_warns_and_skips(data_dir):
    p = _plot("p0", "imshow", field="@M")
    p["artists"] = [{"type": "cell_marks", "cells": [[0, 0]], "marker": "star"}]
    code, warns = canvas.codegen_canvas_full(_spec([p]))
    assert "artist 0 skipped" in code and any("star" in w for w in warns)
    plt.close(canvas.build_figure(_spec([p])))


# ------------------------------------------------------------------ drawn bboxes
def test_plot_bboxes_reflect_aspect_equal(data_dir):
    wide = _plot("wide", "imshow", rect=(0.05, 0.1, 0.9, 0.3), field="@M")  # square image in a wide rect
    spec = _spec([wide])
    r = canvas.render_to_png(spec)
    ax_box = r["plot_bboxes"]["wide"]["axes"]
    assert ax_box[2] < 0.9 - 0.1  # shrunk horizontally by aspect='equal'
    assert abs(ax_box[3] - 0.3) < 1e-3
    assert "tight" in r["plot_bboxes"]["wide"]
    draft = canvas.render_to_png(spec, draft=True)
    assert draft["plot_bboxes"]["wide"]["axes"] == ax_box


# ------------------------------------------------------------------ tick_params
def test_tick_params(data_dir):
    p = _plot("p0", "line", x="@x", y="@y")
    p["axes"]["tick_params"] = {"bottom": False, "length": 2.0, "direction": "in", "junk": 1}
    code, warns = canvas.codegen_canvas_full(_spec([p]))
    assert "ax.tick_params(bottom=False, length=2.0, direction='in')" in code
    assert any("junk" in w for w in warns)
    fig = canvas.build_figure(_spec([p]))
    t = fig.axes[0].xaxis.get_major_ticks()[0]
    assert not t.tick1line.get_visible()
    plt.close(fig)


def test_tick_params_empty_emits_nothing(data_dir):
    p = _plot("p0", "line", x="@x", y="@y")
    base = canvas.codegen_canvas(_spec([p]))
    p["axes"]["tick_params"] = {}
    assert canvas.codegen_canvas(_spec([p])) == base


# ------------------------------------------------------------------ signal slicing
def test_ref_slicing_codegen():
    assert canvas.ref_to_code("@M[0:8]") == "data['M'][0:8]"
    assert canvas.ref_to_code("@M[0:8, 2]") == "data['M'][0:8,2]"
    assert canvas.ref_to_code("@M[::-1]") == "data['M'][::-1]"
    # anything that is not a plain basic slice is a whole (oddly named) signal, never code
    assert canvas.ref_to_code("@M[,1]") == "data['M[,1]']"
    assert canvas.ref_to_code("@M[__import__('os')]") == "data[\"M[__import__('os')]\"]"
    assert canvas.ref_to_code("@x") == "data['x']"


def test_ref_slicing_renders(data_dir):
    top = _plot("top", "imshow", rect=(0.1, 0.55, 0.8, 0.4), field="@M[0:8]")
    bot = _plot("bot", "imshow", rect=(0.1, 0.05, 0.8, 0.4), field="@M[8:]")
    fig = canvas.build_figure(_spec([top, bot]))
    assert fig.axes[0].images[0].get_array().shape == (8, 16)
    assert fig.axes[1].images[0].get_array().shape == (8, 16)
    plt.close(fig)


# ------------------------------------------------------------------ text stroke
def test_text_stroke(data_dir):
    p = _plot("p0", "line", x="@x", y="@y")
    p["artists"] = [{"type": "text", "x": 0.5, "y": 0.5, "text": "x", "axes_frac": True, "color": "#fff",
                     "stroke": {"width": 2, "color": "#000"}}]
    code = canvas.codegen_canvas(_spec([p]))
    assert "import matplotlib.patheffects as patheffects" in code
    assert "path_effects=[patheffects.withStroke(linewidth=2.0, foreground='#000')]" in code
    fig = canvas.build_figure(_spec([p]))
    assert fig.axes[0].texts[0].get_path_effects()
    plt.close(fig)
    p["artists"][0].pop("stroke")
    assert "patheffects" not in canvas.codegen_canvas(_spec([p]))


# ------------------------------------------------------------------ layout warnings
def test_layout_warning_tick_label_overlap(data_dir):
    p = _plot("p0", "line", rect=(0.1, 0.2, 0.15, 0.7), x="@x", y="@y")
    p["axes"]["xticks"] = list(range(13))
    p["axes"]["tick_labelsize"] = 14
    r = canvas.render_to_png(_spec([p]))
    assert any("x tick labels overlap" in w for w in r["warnings"])


def test_layout_warning_text_off_canvas(data_dir):
    spec = _spec([], [{"type": "text", "x": 0.99, "y": 0.5, "text": "a long label that runs off", "fontsize": 12}])
    r = canvas.render_to_png(spec)
    assert any("extends past the canvas edge" in w for w in r["warnings"])

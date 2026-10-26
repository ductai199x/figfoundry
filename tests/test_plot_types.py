"""Every plot type renders and satisfies the WYSIWYG invariant (preview == export),
and vector formats (svg/pdf) come out with the right magic bytes."""
import hashlib
import io

import matplotlib.pyplot as plt
import pytest

from figfoundry import canvas

# Low DPI for the WYSIWYG byte-comparison (holds at any DPI; see test_wysiwyg).
WYSIWYG_DPI = 72

TYPES = {
    "line": {"x": "@x", "y": "@y"},
    "step": {"x": "@x", "y": "@y"},
    "scatter": {"x": "@x", "y": "@y"},
    "scatter-grouped": {"x": "@x", "y": "@y", "z": "@group"},
    "bar": {"x": "@x", "y": "@y"},
    "barh": {"x": "@x", "y": "@y"},
    "histogram": {"y": "@y"},
    "boxplot": {"x": "@group", "y": "@y"},
    "violinplot": {"x": "@group", "y": "@y"},
    "hist2d": {"x": "@x", "y": "@y"},
    "hexbin": {"x": "@x", "y": "@y"},
    "imshow": {"field": "@M"},
    "contourf": {"field": "@M"},
    "contour": {"field": "@M"},
    "pcolormesh": {"field": "@M"},
    "quiver": {"x": "@M", "y": "@M"},
    "streamplot": {"x": "@M", "y": "@M"},
    "pie": {"z": "@slices"},
    "polar": {"x": "@x", "y": "@y"},
    "decision-boundary": {"field": "@M", "x": "@x", "y": "@y", "z": "@group"},
    "scatter-3d": {"x": "@x", "y": "@y", "z": "@s"},
    "surface": {"field": "@M"},
    "wireframe": {"field": "@M"},
}


def _md5(fig):
    fig.set_dpi(WYSIWYG_DPI)
    fig.canvas.draw()
    buf = io.BytesIO()
    fig.canvas.print_figure(buf, dpi=WYSIWYG_DPI)
    plt.close(fig)
    return hashlib.md5(buf.getvalue()).hexdigest()


@pytest.mark.parametrize("ptype,refs", list(TYPES.items()))
def test_wysiwyg_per_type(data_dir, tmp_path, ptype, refs):
    spec = canvas.blank_spec()
    spec["dataset"] = "sample.npz"
    p = canvas._new_plot("p0", [0.1, 0.15, 0.8, 0.7], refs.get("x"), refs.get("y"))
    p["type"] = ptype
    if "z" in refs:
        p["data"]["z"] = refs["z"]
    if "field" in refs:
        p["data"]["field"] = refs["field"]
    spec["plots"] = [p]
    m_preview = _md5(canvas.build_figure(spec))
    out = tmp_path / "export.py"
    out.write_text(canvas.codegen_canvas(spec), encoding="utf-8")
    ns = {"__file__": str(out)}
    exec(compile(out.read_text(encoding="utf-8"), str(out), "exec"), ns)
    assert m_preview == _md5(ns["build"]())


def test_line_index_based(data_dir, tmp_path):
    """A line plot with no x signal plots against its own index (0..n-1)."""
    spec = canvas.blank_spec()
    spec["dataset"] = "sample.npz"
    p = canvas._new_plot("p0", [0.1, 0.15, 0.8, 0.7], None, "@y")
    p["type"] = "line"
    spec["plots"] = [p]
    m1 = _md5(canvas.build_figure(spec))
    out = tmp_path / "e.py"
    out.write_text(canvas.codegen_canvas(spec), encoding="utf-8")
    ns = {"__file__": str(out)}
    exec(compile(out.read_text(encoding="utf-8"), str(out), "exec"), ns)
    assert m1 == _md5(ns["build"]())


def test_vector_export(data_dir):
    spec = canvas.blank_spec()
    spec["dataset"] = "sample.npz"
    spec["plots"] = [canvas._new_plot("p0", [0.1, 0.15, 0.8, 0.7], "@x", "@y")]
    svg = canvas.render_to_bytes(spec, fmt="svg")
    pdf = canvas.render_to_bytes(spec, fmt="pdf")
    assert svg[:5] == b"<?xml" and len(svg) > 1000
    assert pdf[:4] == b"%PDF" and len(pdf) > 1000


def test_palettes_nonempty():
    pals = canvas.get_palettes()
    assert "Okabe-Ito (colorblind-safe)" in pals
    for colors in pals.values():
        assert all(c.startswith("#") and len(c) == 7 for c in colors)


def test_3d_and_polar_projection_in_codegen(data_dir):
    """3-D types emit a 3d projection axis; polar emits a polar one (round-trip
    through build_figure is already covered by test_wysiwyg_per_type)."""
    for ptype, proj in [("scatter-3d", "projection='3d'"), ("surface", "projection='3d'"),
                        ("wireframe", "projection='3d'"), ("polar", "projection='polar'")]:
        spec = canvas.blank_spec()
        spec["dataset"] = "sample.npz"
        p = canvas._new_plot("p0", [0.1, 0.15, 0.8, 0.7], "@x", "@y")
        p["type"] = ptype
        p["data"]["z"] = "@s"
        p["data"]["field"] = "@M"
        spec["plots"] = [p]
        assert proj in canvas.codegen_canvas(spec)

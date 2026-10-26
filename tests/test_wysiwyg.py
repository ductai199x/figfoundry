"""The core invariant: the preview and the exported .py are the same drawing path,
so they must produce byte-identical figures."""
import hashlib
import io

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from figfoundry import canvas


def _md5(fig):
    fig.set_dpi(canvas.PREVIEW_DPI)
    fig.canvas.draw()
    buf = io.BytesIO()
    fig.canvas.print_figure(buf, dpi=canvas.PREVIEW_DPI)
    plt.close(fig)
    return hashlib.md5(buf.getvalue()).hexdigest()


def _spec():
    spec = canvas.blank_spec()
    spec["dataset"] = "sample.npz"
    p = canvas._new_plot("p0", [0.1, 0.15, 0.8, 0.7], "@x", "@y")
    p["data"]["band"] = "@s"
    spec["plots"] = [p]
    return spec


def test_blank_no_dataset(data_dir):
    """A figure with no dataset emits data={} and still renders (empty)."""
    spec = canvas.blank_spec()
    assert spec["dataset"] is None
    code = canvas.codegen_canvas(spec)
    assert "data = {}" in code
    assert canvas.build_figure(spec) is not None


def test_preview_matches_export(data_dir, tmp_path):
    spec = _spec()
    m_preview = _md5(canvas.build_figure(spec))

    out = tmp_path / "export.py"
    out.write_text(canvas.codegen_canvas(spec), encoding="utf-8")
    ns = {"__file__": str(out)}
    exec(compile(out.read_text(encoding="utf-8"), str(out), "exec"), ns)
    m_export = _md5(ns["build"]())

    assert m_preview == m_export

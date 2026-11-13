"""Headless `figfoundry export` (0.4.0): slug or spec.json in, any of
.py/.pdf/.png/.svg out, an --out-dir reproducibility bundle, --data override,
and warnings on stderr. No server involved."""
import json
import os
import subprocess
import sys

import pytest

from figfoundry import canvas, cli


def _spec():
    spec = canvas.blank_spec()
    spec["dataset"] = "sample.npz"
    spec["plots"] = [canvas._new_plot("p0", [0.1, 0.15, 0.8, 0.7], "@x", "@y")]
    return spec


def _run(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["figfoundry", *argv])
    cli.main()


def test_export_slug_to_pdf_py_png(data_dir, tmp_path, monkeypatch, capsys):
    slug = "20260101-000000-abcd"
    with open(os.path.join(cli.CANVAS_DIR, f"{slug}.json"), "w", encoding="utf-8") as f:
        json.dump({"name": "Fig", "spec": _spec()}, f)
    out = tmp_path / "o"
    _run(monkeypatch, "export", slug, "--pdf", str(out / "f.pdf"), "--py", str(out / "f.py"),
         "--png", str(out / "f.png"), "--svg", str(out / "f.svg"))
    assert (out / "f.pdf").read_bytes()[:4] == b"%PDF"
    assert (out / "f.png").read_bytes()[:4] == b"\x89PNG"
    assert (out / "f.svg").read_bytes()[:5] == b"<?xml"
    assert "def build():" in (out / "f.py").read_text(encoding="utf-8")
    printed = capsys.readouterr().out
    assert str(out / "f.pdf") in printed and str(out / "f.py") in printed


def test_export_png_matches_server_render(data_dir, tmp_path, monkeypatch):
    spec_file = tmp_path / "s.json"
    spec_file.write_text(json.dumps(_spec()), encoding="utf-8")
    _run(monkeypatch, "export", str(spec_file), "--png", str(tmp_path / "a.png"))
    assert (tmp_path / "a.png").read_bytes() == canvas.render_to_bytes(_spec(), fmt="png")


def test_export_bare_spec_json_with_dataset_alongside(data_dir, tmp_path, monkeypatch):
    """A spec.json next to its dataset (a bundle) renders from that folder, not
    from ~/.figfoundry/datasets."""
    import shutil

    import numpy as np

    folder = tmp_path / "paper"
    folder.mkdir()
    np.savez(folder / "mine.npz", a=np.arange(4.0), b=np.arange(4.0))
    spec = _spec()
    spec["dataset"] = "mine.npz"
    spec["plots"][0]["data"].update({"x": "@a", "y": "@b"})
    (folder / "fig.json").write_text(json.dumps(spec), encoding="utf-8")
    _run(monkeypatch, "export", str(folder / "fig.json"), "--pdf", str(folder / "fig.pdf"))
    assert (folder / "fig.pdf").exists()
    assert not os.path.exists(os.path.join(data_dir, "mine.npz"))
    shutil.rmtree(folder)


def test_export_data_override(data_dir, tmp_path, monkeypatch):
    import numpy as np

    np.savez(tmp_path / "other.npz", x=np.arange(3.0), y=np.arange(3.0))
    spec_file = tmp_path / "s.json"
    spec_file.write_text(json.dumps({"name": "N", "spec": _spec()}), encoding="utf-8")
    _run(monkeypatch, "export", str(spec_file), "--data", str(tmp_path / "other.npz"),
         "--png", str(tmp_path / "o.png"), "--py", str(tmp_path / "o.py"))
    assert (tmp_path / "o.png").exists()
    assert "DATA_FILE = 'other.npz'" in (tmp_path / "o.py").read_text(encoding="utf-8")


def test_export_out_dir_bundle_runs_standalone(data_dir, tmp_path, monkeypatch):
    spec_file = tmp_path / "s.json"
    spec_file.write_text(json.dumps({"name": "My Fig", "spec": _spec()}), encoding="utf-8")
    bundle = tmp_path / "bundle"
    _run(monkeypatch, "export", str(spec_file), "--out-dir", str(bundle))
    names = sorted(os.listdir(bundle))
    assert names == ["My_Fig.json", "My_Fig.pdf", "My_Fig.png", "My_Fig.py", "sample.npz"]
    r = subprocess.run([sys.executable, "My_Fig.py", "--format", "png", "--out", "again.png"], cwd=bundle)
    assert r.returncode == 0
    assert (bundle / "again.png").read_bytes() == (bundle / "My_Fig.png").read_bytes()


def test_export_warnings_go_to_stderr(data_dir, tmp_path, monkeypatch, capsys):
    spec = _spec()
    spec["plots"][0]["artists"] = [{"type": "sparkle"}]
    spec_file = tmp_path / "s.json"
    spec_file.write_text(json.dumps(spec), encoding="utf-8")
    _run(monkeypatch, "export", str(spec_file), "--pdf", str(tmp_path / "w.pdf"))
    err = capsys.readouterr().err
    assert "warning:" in err and "sparkle" in err


def test_export_missing_spec_file(tmp_path, monkeypatch):
    with pytest.raises(SystemExit):
        _run(monkeypatch, "export", str(tmp_path / "nope.json"), "--pdf", str(tmp_path / "x.pdf"))


def test_render_accepts_spec_json_and_dpi(data_dir, tmp_path, monkeypatch):
    import io

    import matplotlib.pyplot as plt

    spec = _spec()
    spec["canvas"].update({"width": 2.0, "height": 1.0})
    spec_file = tmp_path / "s.json"
    spec_file.write_text(json.dumps(spec), encoding="utf-8")
    _run(monkeypatch, "render", str(spec_file), "--format", "png", "--dpi", "50", "--out", str(tmp_path / "r.png"))
    assert plt.imread(io.BytesIO((tmp_path / "r.png").read_bytes())).shape[:2] == (50, 100)

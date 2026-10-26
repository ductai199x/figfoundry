"""Shared fixtures.

Tests run against a throwaway data dir (tmp_path) so they never touch the real
``~/.figfoundry`` figures or datasets. The package root is put on ``sys.path`` so the
source tree is importable when testing in-repo (harmless when the package is
installed).
"""
import os
import sys

import matplotlib
import numpy as np
import pytest

matplotlib.use("Agg")  # headless backend for all tests (conftest imports before test modules)
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


@pytest.fixture(autouse=True)
def _fresh_dataset_cache(monkeypatch):
    """``canvas._ds_cache`` is keyed by filename only, so an entry left by one
    test's tmp dir would serve that dir's arrays to the next test. Reset per test."""
    import figfoundry.canvas as canvas

    monkeypatch.setattr(canvas, "_ds_cache", {})


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Point the app at a temp data dir and create a small neutral dataset there."""
    import figfoundry.canvas as canvas
    from figfoundry import data_io

    d = tmp_path / "datasets"
    d.mkdir()
    n = 120
    t = np.linspace(0.0, 4.0 * np.pi, n)
    M = np.outer(np.sin(t[:16]), np.cos(t[:16]))  # 16x16 2-D field for imshow/contourf/quiver
    group = np.repeat([0, 1, 2], n // 3)  # categorical, for grouped scatter / boxplot
    slices = np.array([3.0, 1, 4, 1, 5])  # positive values, for pie
    np.savez(d / "sample.npz", x=t, y=np.sin(t), s=np.full(n, 0.1), se=np.full(n, 0.08),
             M=M, group=group, slices=slices)
    monkeypatch.setattr(data_io, "DATA_DIR", str(d))
    monkeypatch.setattr(data_io, "SAMPLE_NPZ", str(d / "sample.npz"))
    monkeypatch.setattr(canvas, "DATA_DIR", str(d))
    return str(d)

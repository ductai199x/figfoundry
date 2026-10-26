"""Dataset I/O for Fig Foundry.

A *dataset* is a named collection of 1-D signals (arrays). The canvas references
signals by name (``@name``); the exported code loads the dataset file and indexes it.

Supported formats:
    .npz  -- numpy zip of named arrays. SAFE by construction (loaded with
             allow_pickle=False, so no code can execute).
    .pkl  -- Python pickle. CAN execute arbitrary code, so it is loaded through
             SafeUnpickler, a whitelist unpickler that only permits numpy arrays
             and plain containers (dict/list/tuple) -- blocking os, subprocess,
             socket, ctypes, etc. Local tool, defense-in-depth.
    .csv  -- a table with a header row; each numeric column becomes a signal
             (non-numeric columns are dropped, since a figure tool plots numbers).

A small neutral sample dataset (``datasets/sample.npz``) is bundled so a fresh
install has something to open; users load their own ``.npz``/``.pkl`` to work with
real data. Nothing in this module depends on any particular project's data.
"""
import builtins
import importlib
import os
import pickle

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
# Runtime data lives in a user-writable dir (works when the package is installed
# read-only in site-packages). The bundled sample.npz is copied here on first run.
USER_DIR = os.path.join(os.path.expanduser("~"), ".figfoundry")
DATA_DIR = os.path.join(USER_DIR, "datasets")
os.makedirs(DATA_DIR, exist_ok=True)
SAMPLE_NPZ = os.path.join(DATA_DIR, "sample.npz")
BUNDLED_DATA_DIR = os.path.join(HERE, "datasets")


class SafeUnpickler(pickle.Unpickler):
    """Whitelist unpickler: only numpy (its arrays and the internal multiarray/dtype
    reconstruction routines it pickles with) plus plain built-in containers. Anything
    from os, subprocess, socket, ctypes, or a non-container builtin (exec, __import__,
    ...) raises UnpicklingError, so a malicious pickle cannot execute code."""
    _SAFE_BUILTINS = {"tuple", "list", "dict", "set"}

    def find_class(self, module, name):
        if module == "builtins":
            if name not in self._SAFE_BUILTINS:
                raise pickle.UnpicklingError(f"blocked unsafe pickle class {module}.{name}")
            return getattr(builtins, name)
        if module == "numpy" or module.startswith("numpy."):
            return getattr(importlib.import_module(module), name)
        raise pickle.UnpicklingError(f"blocked unsafe pickle class {module}.{name}")


def safe_load_pkl(path):
    with open(path, "rb") as f:
        return SafeUnpickler(f).load()


def _numeric_dict(obj):
    """Normalize a loaded object to {name: 1-D+ ndarray}, dropping non-numeric entries."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if str(k).startswith("__"):  # metadata, not a signal
                continue
            a = np.asarray(v)
            if a.dtype == object or a.size == 0:
                continue
            out[str(k)] = a
        return out
    if isinstance(obj, np.ndarray):
        return {"data": obj}
    raise ValueError("dataset must be a dict of arrays or a single array")


def _load_csv(path):
    """Load a CSV with a header row into {column_name: 1-D float array}."""
    import csv
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = [r for r in csv.reader(f) if any(c.strip() for c in r)]
    out = {}
    for i, col in enumerate(rows[0] if rows else []):
        col = col.strip() or f"col{i}"
        vals = []
        for r in rows[1:]:
            if i < len(r):
                try:
                    vals.append(float(r[i]))
                except ValueError:
                    pass
        if vals:
            out[col] = np.asarray(vals, dtype=float)
    return out


def load_arrays(path):
    """Load a .npz/.pkl/.csv file into ({name: ndarray}, kind)."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".npz":
        raw = np.load(path, allow_pickle=False)  # safe: no pickle deserialization
        return _numeric_dict({k: raw[k] for k in raw.files}), "npz"
    if ext in (".pkl", ".pickle"):
        return _numeric_dict(safe_load_pkl(path)), "pkl"
    if ext == ".csv":
        return _load_csv(path), "csv"
    raise ValueError(f"unsupported dataset format: {ext}")


def list_signals(arrays):
    return [{"id": k, "name": k, "length": int(v.shape[0]), "shape": [int(d) for d in v.shape],
             "ndim": int(v.ndim), "dtype": str(v.dtype)} for k, v in arrays.items()]


def _synthetic_sample():
    """A small, neutral demo dataset (no project data) for a fresh install.
    Mirrors the bundled sample.npz: 1-D signals plus a 2-D field, a group, and slices."""
    n = 200
    t = np.linspace(0.0, 4.0 * np.pi, n)
    g = np.linspace(-2.0, 2.0, 24)
    X, Y = np.meshgrid(g, g)
    return {
        "x": t,
        "sine": np.sin(t),
        "cosine": np.cos(t),
        "damped": np.sin(t) * np.exp(-t / 8.0),
        "ramp": np.linspace(0.0, 1.0, n),
        "se": np.full(n, 0.08),
        "field": np.sin(X) * np.cos(Y) * np.exp(-(X ** 2 + Y ** 2) / 6.0),
        "group": np.arange(n) % 3,
        "slices": np.array([3.0, 1, 4, 1, 5]),
    }


def ensure_sample_npz():
    """Make the neutral sample dataset available in the user dir (copy from the
    bundled file, or synthesize it if the bundle is absent in a dev checkout)."""
    if os.path.exists(SAMPLE_NPZ):
        return SAMPLE_NPZ
    bundled = os.path.join(BUNDLED_DATA_DIR, "sample.npz")
    if os.path.exists(bundled):
        import shutil
        shutil.copy(bundled, SAMPLE_NPZ)
    else:
        np.savez(SAMPLE_NPZ, **_synthetic_sample())
    return SAMPLE_NPZ

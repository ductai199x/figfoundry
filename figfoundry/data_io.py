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
import contextlib
import csv
import importlib
import io
import os
import pickle
from typing import ClassVar

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
    """Whitelist unpickler: only the numpy reconstruction machinery (the exact
    (module, name) pairs numpy uses to pickle arrays and scalars) plus plain
    built-in containers. Anything else -- os, subprocess, socket, ctypes, non-container
    builtins (exec, __import__), and numpy's own callables (np.load, np.fromfile, ...)
    -- raises UnpicklingError, so a malicious pickle cannot execute code or read files.
    numpy is matched by exact name, not module prefix: a pickle can pass any allowed
    attribute as the callable its reducer invokes, so the prefix form let
    ``numpy.fromfile`` (arbitrary file read) through."""
    _SAFE_BUILTINS: ClassVar = {"tuple", "list", "dict", "set"}
    _SAFE_NUMPY: ClassVar = {
        ("numpy", "ndarray"),
        ("numpy", "dtype"),
        ("numpy.core.multiarray", "_reconstruct"),   # numpy < 2
        ("numpy.core.multiarray", "scalar"),
        ("numpy._core.multiarray", "_reconstruct"),  # numpy >= 2
        ("numpy._core.multiarray", "scalar"),
        # NOTE: this set must stay in sync with the embedded _SafeUnpickler that
        # canvas.dataset_loader_code emits into exported .py files (the preview
        # and export both execute that code). Adding an entry here without adding
        # it there would accept a file in-app that the export can't load.
    }

    def find_class(self, module, name):
        if module == "builtins":
            if name not in self._SAFE_BUILTINS:
                raise pickle.UnpicklingError(f"blocked unsafe pickle class {module}.{name}")
            return getattr(builtins, name)
        if (module, name) in self._SAFE_NUMPY:
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
            try:
                a = np.asarray(v)
            except (TypeError, ValueError):
                continue  # ragged / non-arrayable values are not signals
            if a.size == 0 or a.ndim == 0:
                continue
            if a.dtype == object or a.dtype.kind in ("U", "S"):
                continue  # text, not numbers
            out[str(k)] = a
        return out
    if isinstance(obj, np.ndarray):
        return {"data": obj}
    raise ValueError("dataset must be a dict of arrays or a single array")


def _csv_to_arrays(rows):
    """Header row + data rows -> {column_name: 1-D float array}. Non-numeric columns
    are dropped (a figure tool plots numbers); duplicate header names get a _2/_3
    suffix instead of silently overwriting each other."""
    out = {}
    for i, col in enumerate(rows[0] if rows else []):
        col = col.strip() or f"col{i}"
        vals = []
        for r in rows[1:]:
            if i < len(r):
                with contextlib.suppress(ValueError):
                    vals.append(float(r[i]))
        if vals:
            name, n = col, 2
            while name in out:
                name = f"{col}_{n}"
                n += 1
            out[name] = np.asarray(vals, dtype=float)
    return out


def _load_csv(path):
    """Load a CSV with a header row into {column_name: 1-D float array}."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = [r for r in csv.reader(f) if any(c.strip() for c in r)]
    return _csv_to_arrays(rows)


def load_arrays(path):
    """Load a .npz/.pkl/.csv file into ({name: ndarray}, kind)."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".npz":
        with np.load(path, allow_pickle=False) as raw:  # safe: no pickle deserialization
            return _numeric_dict({k: raw[k] for k in raw.files}), "npz"
    if ext in (".pkl", ".pickle"):
        return _numeric_dict(safe_load_pkl(path)), "pkl"
    if ext == ".csv":
        return _load_csv(path), "csv"
    raise ValueError(f"unsupported dataset format: {ext}")


def load_arrays_bytes(data, filename):
    """Validate/parse dataset bytes exactly like load_arrays, without a file on disk.
    The upload endpoint uses it to reject a bad file before it can replace an
    existing dataset of the same name."""
    ext = os.path.splitext(filename)[1].lower()
    if ext == ".npz":
        with np.load(io.BytesIO(data), allow_pickle=False) as raw:
            return _numeric_dict({k: raw[k] for k in raw.files}), "npz"
    if ext in (".pkl", ".pickle"):
        return _numeric_dict(SafeUnpickler(io.BytesIO(data)).load()), "pkl"
    if ext == ".csv":
        rows = [r for r in csv.reader(io.StringIO(data.decode("utf-8-sig"))) if any(c.strip() for c in r)]
        return _csv_to_arrays(rows), "csv"
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

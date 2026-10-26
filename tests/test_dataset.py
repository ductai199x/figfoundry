"""Dataset loading: .npz (safe) and .pkl (whitelisted) both yield {name: array}."""
import pickle

import numpy as np
import pytest

from figfoundry.data_io import load_arrays, list_signals


def test_load_npz(tmp_path):
    p = tmp_path / "d.npz"
    np.savez(p, a=np.arange(4.0), b=np.ones(3))
    arrays, kind = load_arrays(str(p))
    assert kind == "npz"
    assert set(arrays) == {"a", "b"}


def test_load_pkl(tmp_path):
    p = tmp_path / "d.pkl"
    with open(p, "wb") as f:
        pickle.dump({"a": np.arange(4.0)}, f)
    arrays, kind = load_arrays(str(p))
    assert kind == "pkl"
    assert "a" in arrays
    assert list_signals(arrays)[0]["name"] == "a"


def test_load_csv(tmp_path):
    p = tmp_path / "d.csv"
    p.write_text("a,b,label\n1.0,2.0,x\n2.0,4.0,y\n3.0,9.0,z\n", encoding="utf-8")
    arrays, kind = load_arrays(str(p))
    assert kind == "csv"
    assert set(arrays) == {"a", "b"}  # 'label' is non-numeric, so dropped
    assert arrays["a"].tolist() == [1.0, 2.0, 3.0]


def test_rejects_bad_extension(tmp_path):
    p = tmp_path / "d.txt"
    p.write_text("a,b\n1,2\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_arrays(str(p))

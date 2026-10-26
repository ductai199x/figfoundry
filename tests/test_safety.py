"""A pickle that tries to execute code must be blocked by the whitelist unpickler."""
import io
import pickle

import numpy as np
import pytest

from figfoundry.data_io import SafeUnpickler


def test_blocks_code_execution():
    evil = io.BytesIO()
    pickle.dump({"a": 1, "evil": __import__("os").system}, evil)
    evil.seek(0)
    with pytest.raises(pickle.UnpicklingError):
        SafeUnpickler(evil).load()


def test_allows_plain_arrays():
    good = io.BytesIO()
    pickle.dump({"x": np.arange(5.0)}, good)
    good.seek(0)
    data = SafeUnpickler(good).load()
    assert list(data) == ["x"]
    assert data["x"].shape == (5,)

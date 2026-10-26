"""A pickle that tries to execute code must be blocked by the whitelist unpickler."""
import io
import pickle

import numpy as np
import pytest

from figfoundry.data_io import SafeUnpickler


@pytest.mark.parametrize("evil", [
    __import__("os").system,
    __import__("subprocess").run,
    __import__,          # builtins.__import__
    eval,                # builtins.eval
    exec,                # builtins.exec
    open,                # builtins.open (file read)
])
def test_blocks_code_execution(evil):
    """Any code-execution primitive smuggled in as a pickle value is rejected."""
    evil_buf = io.BytesIO()
    pickle.dump({"a": 1, "evil": evil}, evil_buf)
    evil_buf.seek(0)
    with pytest.raises(pickle.UnpicklingError):
        SafeUnpickler(evil_buf).load()


def test_allows_plain_arrays():
    good = io.BytesIO()
    pickle.dump({"x": np.arange(5.0)}, good)
    good.seek(0)
    data = SafeUnpickler(good).load()
    assert list(data) == ["x"]
    assert data["x"].shape == (5,)


class _ForeignClass:
    """Module-level so pickle can reference it (a local class is not picklable)."""


def test_blocks_foreign_classes():
    # Anything not from numpy or the plain built-in containers is rejected,
    # including classes defined in arbitrary modules.
    evil = io.BytesIO()
    pickle.dump({"x": np.arange(3.0), "o": _ForeignClass()}, evil)
    evil.seek(0)
    with pytest.raises(pickle.UnpicklingError):
        SafeUnpickler(evil).load()

"""Fig Foundry -- a WYSIWYG matplotlib figure composer.

Load a dataset (.npz / .pkl), drag signals onto a canvas to make plots, arrange
and style them freely, and export clean, reproducible Python. The preview renders
by executing the generated code, so what you see is exactly what the .py produces.
"""
from importlib.metadata import PackageNotFoundError, version

try:
    # Read from the installed distribution so the version can never drift from
    # pyproject.toml (a hardcoded string here had to be bumped in two places).
    __version__ = version("figfoundry")
except PackageNotFoundError:  # imported from a source checkout, not installed
    __version__ = "0.0.0"

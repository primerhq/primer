"""primectl: a kubectl-style CLI for the Primer API."""
from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("primectl")
except PackageNotFoundError:  # not installed (e.g. raw source tree)
    __version__ = "0.0.0"

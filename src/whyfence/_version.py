"""The installed version, from the package metadata (so it can't drift from pyproject.toml)."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("whyfence")
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0+unknown"

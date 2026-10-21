"""maps-mcp: walking/driving travel times and places from the signed-out Google Maps web page."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("maps-mcp")
except PackageNotFoundError:  # pragma: no cover - source checkout without install
    __version__ = "0.0.0"

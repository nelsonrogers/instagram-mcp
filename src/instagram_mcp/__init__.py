"""Instagram MCP: a local, read-only Model Context Protocol server for a creator's Instagram stats."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("instagram-mcp")
except PackageNotFoundError:  # pragma: no cover - only when running from a source tree without install
    __version__ = "0.0.0"

__all__ = ["__version__"]

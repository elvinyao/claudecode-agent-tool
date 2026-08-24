"""Evidence-grounded Ankify domain plugin for agent-core."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("ankify-agent")
except PackageNotFoundError:  # pragma: no cover - source checkout without installation
    __version__ = "0.1.0"

__all__ = ["__version__"]

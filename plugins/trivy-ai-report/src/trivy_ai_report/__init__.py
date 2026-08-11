"""Trivy AI report generator."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("trivy-ai-report")
except PackageNotFoundError:  # pragma: no cover - source checkout without installation
    __version__ = "0.2.0"

__all__ = ["__version__"]

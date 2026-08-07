"""Fixed Gemini command-line entry point."""

from __future__ import annotations

from collections.abc import Sequence

from trivy_ai_report.cli import main_for_provider


def main(argv: Sequence[str] | None = None) -> int:
    return main_for_provider("gemini", argv, prog="trivy-report-gemini")


if __name__ == "__main__":  # pragma: no cover
    main()

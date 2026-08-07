"""Fixed Codex command-line entry point."""

from __future__ import annotations

from collections.abc import Sequence

from trivy_ai_report.cli import main_for_provider


def main(argv: Sequence[str] | None = None) -> int:
    return main_for_provider("codex", argv, prog="trivy-report-codex")


if __name__ == "__main__":  # pragma: no cover
    main()

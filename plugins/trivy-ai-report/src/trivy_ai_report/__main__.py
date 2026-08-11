"""Delegate module execution to the generic framework CLI."""

from agent_core.cli import main

if __name__ == "__main__":
    raise SystemExit(main())

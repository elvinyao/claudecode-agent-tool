"""Allow ``python -m agent_core`` to invoke the generic CLI."""

from agent_core.cli import main

raise SystemExit(main())

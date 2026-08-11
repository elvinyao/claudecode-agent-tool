# agent-core

Domain-neutral, local-first framework for combining deterministic Python steps with typed Codex or
Claude Agent SDK steps.

It provides strict artifact contracts, a validated typed DAG, per-run plugin/provider factories,
safe Skill staging, retry/deadline/cancellation policy, ActionNode authority gates, metadata-only
audit logging, a generic CLI, and an optional bounded FastAPI Job API.

Install only what is required:

```bash
pip install agent-core
pip install 'agent-core[codex]'
pip install 'agent-core[claude]'
pip install 'agent-core[web]'
```

Domain packages register factories through the `agent_core.domain_plugins` entry-point group. See
the workspace root README and `tests/fixtures/toy-plugin` for the complete contract.

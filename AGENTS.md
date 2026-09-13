# Repository Guidelines

## Project Structure & Module Organization

This uv workspace contains three Python distributions:
- `packages/agent-core/src/agent_core/`: shared contracts, workflow runtime, provider adapters, CLI, and web workbench.
- `plugins/trivy-ai-report/src/trivy_ai_report/`: Trivy report processing.
- `plugins/ankify/src/ankify/`: Ankify workflows, bundled skills, and evaluation resources.

Tests live in `tests/`, grouped by core, providers, plugins, web, integration, and packaging. `examples/` contains sample inputs; `scripts/render_examples.py` renders examples. Architecture proposals live in `docs/`. Keep domain-specific behavior in plugins and shared infrastructure in agent-core.

## Execution Policy

Use the host only for read-only inspection and source editing through file-editing tools. Run every dependency, build, test, lint, formatting, script, or application command through `bash .agent/run.sh <command>`. Put compound commands inside the container with `bash .agent/run.sh sh -lc '...'`. Do not execute project code on the host, use host shell redirection for project work, or invoke `docker run` directly.

Reuse the existing runner, which mounts the repository at `/workspace` and defaults to Python 3.12 with uv. If Docker or the runner fails, report the failure; never fall back to host execution.

## Build, Test, and Development Commands

- `bash .agent/run.sh uv sync --locked --all-extras`: install workspace dependencies.
- `bash .agent/run.sh uv run agent-core plugins list`: verify plugin discovery.
- `bash .agent/run.sh uv run ruff check packages/agent-core/src plugins/trivy-ai-report/src plugins/ankify/src tests`: lint sources and tests.
- `bash .agent/run.sh uv run pytest`: run tests.
- `bash .agent/run.sh uv run pytest --cov=agent_core --cov=trivy_ai_report --cov=ankify --cov-branch`: check coverage.
- `bash .agent/run.sh uv build --package agent-core`: build a distribution; repeat for `trivy-ai-report` and `ankify-agent`.

## Coding Style & Naming Conventions

Use four-space indentation, type annotations, `snake_case` functions/modules, and `PascalCase` classes. Ruff enforces import sorting and lint rules with a 100-character line limit. Preserve Python 3.10 compatibility; CI covers Python 3.10 and 3.12.

## Testing Guidelines

Use pytest and pytest-asyncio; name files `test_*.py` and functions `test_*`. Add regression tests near the affected subsystem. Coverage includes branches and must reach 80%. Default tests should remain offline; real-provider tests use the `live` marker and require explicit configuration.

## Commit & Pull Request Guidelines

Recent commits use prefixes such as `feat:` and `docs:`; follow that concise, imperative style. PRs should describe the behavior change, relevant issues, and validation performed. Include screenshots for workbench UI changes. Preserve package boundaries and update documentation when commands or public contracts change.

## Mandatory Pre-Commit Checks

Before every commit, run these commands in order through Docker:

1. `bash .agent/run.sh uv run ruff check .`
2. `bash .agent/run.sh uv run ruff format .`
3. `bash .agent/run.sh uv run ty check`
4. Only after all three succeed, run `bash .agent/run.sh uv run pytest --cov=agent_core --cov=trivy_ai_report --cov=ankify --cov-branch`.

Fix all failures before committing. If formatting or subsequent fixes change code, rerun the checks and tests on the final changes. Commit only when every check and the test suite pass; unavailable tools or Docker failures do not waive this requirement.

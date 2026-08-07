# Generic Agent Framework Core (`agent_core`) & Domain Plugin Architecture Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor the codebase to separate the generic Agent Framework (`agent_core`) from domain-specific applications. `trivy_ai_report` becomes one domain plugin registered into the framework, enabling future domain plugins (e.g., Linter Fixer, FinOps Optimizer, K8s Log Analyzer) to reuse 100% of the framework infrastructure.

**Architecture:** 
1. **`agent_core` (Generic Framework Layer)**: Houses Provider Adapters (Codex, Claude, Gemini), Skill Staging, Async I/O Fetchers/Publishers, Generic Pipeline Engine, and FastAPI Web Server.
2. **`trivy_ai_report` (Domain Plugin Layer)**: Implements domain-specific Scanner Parser, System Prompts, Guardrail Fallback Rules, Jinja2 HTML Templates, and Skill definitions.

**Tech Stack:** Python 3.10+, FastAPI, Uvicorn, httpx, Pydantic v2, Jinja2, pytest, pytest-asyncio.

---

## File Structure & Module Boundary Map

```text
src/
├── agent_core/                             [NEW] Generic Agent Framework
│   ├── __init__.py                         [NEW] Core exports
│   ├── models/                             [NEW] Domain-agnostic schemas (Fact, Advice, Outcome)
│   │   ├── __init__.py
│   │   └── contracts.py
│   ├── providers/                          [MOVED] LLM Adapters (Codex, Claude, Gemini)
│   │   ├── __init__.py
│   │   └── base.py
│   ├── skills/                             [MOVED] Generic Skill Validation & Staging
│   │   ├── __init__.py
│   │   └── staging.py
│   ├── io/                                 [NEW] Universal Fetchers & Publishers
│   │   ├── __init__.py
│   │   ├── fetchers.py
│   │   └── publishers.py
│   ├── pipeline.py                         [NEW] Generic Execution Engine & Domain Adapter Protocol
│   └── web.py                              [NEW] Universal FastAPI Server
│
└── trivy_ai_report/                        [REFACTORED] Domain Application / Plugin
    ├── __init__.py
    ├── plugin.py                           [NEW] Implements DomainAdapter for Trivy
    ├── parser.py                           [RENAME] Moved from trivy.py
    ├── rules.py                            [MODIFY] Trivy-specific guardrails
    ├── prompts.py                          [MODIFY] Trivy-specific prompts
    ├── renderer.py                         [MODIFY] Trivy Jinja2 HTML renderer
    ├── cli.py                              [MODIFY] CLI Delegate using agent_core
    └── templates/
        └── report.html.j2
```

---

### Task 1: Create `agent_core.models` & `agent_core.pipeline` Protocol

**Files:**
- Create: `src/agent_core/__init__.py`
- Create: `src/agent_core/models/__init__.py`
- Create: `src/agent_core/models/contracts.py`
- Create: `src/agent_core/pipeline.py`
- Test: `tests/test_agent_core.py`

- [ ] **Step 1: Write failing test for `DomainAdapter` protocol and `agent_core` contracts**

```python
# tests/test_agent_core.py
import pytest
from agent_core.models.contracts import BaseFact, BaseAdvice, AnalysisOutcome
from agent_core.pipeline import DomainAdapter, AgentPipeline

class DummyAdapter(DomainAdapter):
    name = "dummy"
    def parse_input(self, raw_data: dict) -> list[BaseFact]:
        return [BaseFact(fact_id="1", summary="test")]
    def build_prompt(self, facts: list[BaseFact], enrich_web: bool) -> str:
        return "analyze"
    def render_output(self, facts: list[BaseFact], outcome: AnalysisOutcome) -> str:
        return "<html>OK</html>"

@pytest.mark.asyncio
async def test_dummy_pipeline_execution():
    pipeline = AgentPipeline(adapter=DummyAdapter())
    result = await pipeline.run(provider="codex", input_data={"dummy": True})
    assert result.success is True
    assert result.content == "<html>OK</html>"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_agent_core.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'agent_core'`

- [ ] **Step 3: Implement `agent_core/models/contracts.py` and `agent_core/pipeline.py`**

```python
# src/agent_core/models/contracts.py
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from pydantic import BaseModel, ConfigDict, Field

class BaseFact(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)
    fact_id: str
    summary: str

class BaseAdvice(BaseModel):
    model_config = ConfigDict(extra="allow")
    fact_id: str
    title_zh: str
    actions_zh: list[str]

class AnalysisOutcome(BaseModel):
    model_config = ConfigDict(extra="allow")
    provider: str
    model: str = "provider-default"
    skill_name: str | None = None
    enrich_web: bool = False
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    recommendations: list[Any] = Field(default_factory=list)
    partial: bool = False
    warnings: list[str] = Field(default_factory=list)
```

```python
# src/agent_core/pipeline.py
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any
from agent_core.models.contracts import BaseFact, AnalysisOutcome

class DomainAdapter(ABC):
    name: str

    @abstractmethod
    def parse_input(self, raw_data: dict[str, Any]) -> list[BaseFact]: ...

    @abstractmethod
    def build_prompt(self, facts: list[BaseFact], enrich_web: bool) -> str: ...

    @abstractmethod
    def validate_and_merge(
        self, facts: list[BaseFact], raw_advices: list[Any], failure_reason: str | None
    ) -> tuple[list[Any], list[str], bool]: ...

    @abstractmethod
    def render_output(self, facts: list[BaseFact], outcome: AnalysisOutcome) -> str: ...

@dataclass(slots=True)
class PipelineResult:
    success: bool
    content: str
    outcome: AnalysisOutcome
    warnings: list[str] = field(default_factory=list)
    partial: bool = False

class AgentPipeline:
    def __init__(self, adapter: DomainAdapter):
        self.adapter = adapter

    async def run(
        self,
        provider: str,
        input_data: dict[str, Any],
        model: str | None = None,
        skill_path: str | None = None,
        enrich_web: bool = False,
        timeout_seconds: float = 300.0,
    ) -> PipelineResult:
        facts = self.adapter.parse_input(input_data)
        if not facts:
            outcome = AnalysisOutcome(
                provider=provider,
                model=model or "provider-default",
                enrich_web=enrich_web,
            )
            content = self.adapter.render_output([], outcome)
            return PipelineResult(success=True, content=content, outcome=outcome)

        # Mock / Provider execution boundary
        outcome = AnalysisOutcome(provider=provider, model=model or "default")
        merged, warnings, partial = self.adapter.validate_and_merge(facts, [], None)
        outcome.recommendations = merged
        outcome.warnings = warnings
        outcome.partial = partial

        content = self.adapter.render_output(facts, outcome)
        return PipelineResult(
            success=True,
            content=content,
            outcome=outcome,
            warnings=warnings,
            partial=partial,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_agent_core.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/agent_core/ tests/test_agent_core.py
git commit -m "feat(core): introduce agent_core framework and DomainAdapter protocol"
```

---

### Task 2: Migrate Providers and Skills to `agent_core`

**Files:**
- Create: `src/agent_core/providers/`
- Create: `src/agent_core/skills/`
- Move: logic from `trivy_ai_report/providers.py` and `trivy_ai_report/skills.py` to `agent_core`
- Update: `trivy_ai_report` imports to reference `agent_core`

- [ ] **Step 1: Move `providers.py` and `skills.py` to `agent_core/`**

Create `src/agent_core/providers/__init__.py` and `src/agent_core/skills/__init__.py`.
Re-export provider factory `create_analyzer` and skill loader `load_skill`, `materialize_skill` from `agent_core`.

- [ ] **Step 2: Update existing unit tests to verify backward compatibility**

Run: `uv run pytest`
Expected: All existing tests pass using `agent_core` re-exports or updated module imports.

- [ ] **Step 3: Commit**

```bash
git add src/agent_core/ src/trivy_ai_report/ tests/
git commit -m "refactor(core): migrate Provider Adapters and Skill loader to agent_core"
```

---

### Task 3: Implement Universal I/O & Universal FastAPI Server in `agent_core`

**Files:**
- Create: `src/agent_core/io/fetchers.py`
- Create: `src/agent_core/io/publishers.py`
- Create: `src/agent_core/web.py`
- Test: `tests/test_framework_web.py`

- [ ] **Step 1: Write failing test for Universal FastAPI Server with dynamically registered Domain Adapters**

```python
# tests/test_framework_web.py
import pytest
from fastapi.testclient import TestClient
from agent_core.web import create_app
from agent_core.pipeline import DomainAdapter
from agent_core.models.contracts import BaseFact, AnalysisOutcome

class MockDomain(DomainAdapter):
    name = "mock_domain"
    def parse_input(self, raw_data: dict) -> list[BaseFact]:
        return [BaseFact(fact_id="1", summary="fact")]
    def build_prompt(self, facts: list[BaseFact], enrich_web: bool) -> str:
        return "prompt"
    def validate_and_merge(self, facts, raw_advices, failure_reason):
        return [], [], False
    def render_output(self, facts: list[BaseFact], outcome: AnalysisOutcome) -> str:
        return "mock_output"

app = create_app(adapters=[MockDomain()])
client = TestClient(app)

def test_universal_analyze_endpoint():
    response = client.post("/api/v1/analyze", json={
        "domain": "mock_domain",
        "provider": "codex",
        "input_json": {"test": True}
    })
    assert response.status_code == 200
    assert response.json()["content"] == "mock_output"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_framework_web.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement `agent_core/web.py` with dynamic domain adapter registry**

```python
# src/agent_core/web.py
from __future__ import annotations
from typing import Any
from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, Field
from agent_core.pipeline import AgentPipeline, DomainAdapter
from agent_core.io.fetchers import fetch_input_bytes
from agent_core.io.publishers import publish_output_bytes
import json

class FrameworkApiRequest(BaseModel):
    domain: str = Field(..., description="Registered domain plugin name (e.g. trivy, linter, finops)")
    provider: str = Field(..., description="Agent Provider: codex, claude, gemini")
    input_json: dict[str, Any] | None = None
    input_url: str | None = None
    upload_url: str | None = None
    enrich_web: bool = False

class FrameworkApiResponse(BaseModel):
    success: bool
    domain: str
    content: str | None = None
    publish_result: str | None = None
    warnings: list[str] = Field(default_factory=list)

def create_app(adapters: list[DomainAdapter]) -> FastAPI:
    registry: dict[str, DomainAdapter] = {adapter.name: adapter for adapter in adapters}
    app = FastAPI(title="Generic Agent Framework API", version="1.0.0")

    @app.get("/health")
    async def health():
        return {"status": "ok", "domains": list(registry.keys())}

    @app.post("/api/v1/analyze", response_model=FrameworkApiResponse)
    async def analyze(req: FrameworkApiRequest):
        adapter = registry.get(req.domain)
        if not adapter:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unknown domain plugin: {req.domain}. Available: {list(registry.keys())}",
            )

        if not req.input_json and not req.input_url:
            raise HTTPException(status_code=400, detail="Must supply input_json or input_url")

        if req.input_url:
            raw_bytes = await fetch_input_bytes(req.input_url)
            input_data = json.loads(raw_bytes.decode("utf-8"))
        else:
            input_data = req.input_json

        pipeline = AgentPipeline(adapter=adapter)
        result = await pipeline.run(provider=req.provider, input_data=input_data, enrich_web=req.enrich_web)

        pub_res = None
        if req.upload_url:
            pub_res = await publish_output_bytes(req.upload_url, result.content.encode("utf-8"))

        return FrameworkApiResponse(
            success=result.success,
            domain=req.domain,
            content=result.content if not req.upload_url else None,
            publish_result=pub_res,
            warnings=result.warnings,
        )

    return app
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_framework_web.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/agent_core/ tests/test_framework_web.py
git commit -m "feat(web): implement universal Agent Framework web server with dynamic plugin registry"
```

---

### Task 4: Refactor `trivy_ai_report` as a Domain Plugin (`TrivyDomainAdapter`)

**Files:**
- Create: `src/trivy_ai_report/plugin.py`
- Modify: `src/trivy_ai_report/cli.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Implement `TrivyDomainAdapter` in `src/trivy_ai_report/plugin.py`**

```python
# src/trivy_ai_report/plugin.py
from __future__ import annotations
from typing import Any
from agent_core.pipeline import DomainAdapter
from agent_core.models.contracts import BaseFact, AnalysisOutcome
from trivy_ai_report.parser import parse_trivy_report
from trivy_ai_report.prompts import build_analysis_prompt
from trivy_ai_report.rules import merge_recommendations
from trivy_ai_report.renderer import render_html

class TrivyDomainAdapter(DomainAdapter):
    name = "trivy"

    def parse_input(self, raw_data: dict[str, Any]) -> list[BaseFact]:
        report = parse_trivy_report(raw_data)
        # Adapt Finding to BaseFact
        return [BaseFact(fact_id=f.finding_id, summary=f.title) for f in report.findings]

    def build_prompt(self, facts: list[BaseFact], enrich_web: bool) -> str:
        # Calls trivy-specific prompt builder
        return build_analysis_prompt([], enrich_web=enrich_web)

    def validate_and_merge(self, facts, raw_advices, failure_reason):
        return merge_recommendations([], raw_advices, failure_reason=failure_reason)

    def render_output(self, facts, outcome: AnalysisOutcome) -> str:
        return render_html(None, outcome)
```

- [ ] **Step 2: Run full test suite to ensure complete backward compatibility**

Run: `uv run pytest && uv run ruff check .`
Expected: All checks passed!

- [ ] **Step 3: Commit**

```bash
git add src/trivy_ai_report/ plugin.py cli.py
git commit -m "refactor(trivy): encapsulate trivy_ai_report as a clean domain plugin over agent_core"
```

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-08-07-framework-core-and-trivy-plugin.md`. Two execution options:

**1. Subagent-Driven (recommended)** - Dispatch a fresh subagent per task, review between tasks, fast iteration.
**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?

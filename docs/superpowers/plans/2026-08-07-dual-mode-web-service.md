# Dual-Mode Web API & Remote I/O Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend `trivy-ai-report` into a dual-mode system (CLI + Web API Service) with remote I/O capabilities for downloading input reports from external APIs/URLs and uploading generated reports to external Webhooks or object storage.

**Architecture:** Decouple the core ETL pipeline into a `ReportPipeline` service layer. Abstract input fetching (`InputFetcher`) and output publishing (`OutputPublisher`). Expose a FastAPI Web Server (`trivy-ai-report-serve`) with async REST API endpoints alongside the existing CLI entry points.

**Tech Stack:** Python 3.10+, FastAPI, Uvicorn, httpx, Pydantic v2, Jinja2, pytest, pytest-asyncio.

---

## Component & File Structure

```text
src/trivy_ai_report/
├── io/                             [NEW] Remote and Local I/O Abstraction Layer
│   ├── __init__.py                 [NEW] Exports Fetcher and Publisher factories
│   ├── fetchers.py                 [NEW] Local File & HTTP/S3 Input Fetchers
│   └── publishers.py               [NEW] Local File & Webhook/HTTP Upload Publishers
├── service.py                      [NEW] Core decoupled ReportPipeline orchestration
├── web.py                          [NEW] FastAPI Web Service & Uvicorn entry point
├── cli.py                          [MODIFY] Update CLI to use ReportPipeline & IO Layer
└── models.py                       [MODIFY] Add Web API request/response schemas
pyproject.toml                      [MODIFY] Add optional dependencies `web` and script `trivy-ai-report-serve`
tests/
├── test_io.py                      [NEW] Unit tests for Fetchers and Publishers
├── test_service.py                 [NEW] Unit tests for decoupled ReportPipeline
└── test_web.py                     [NEW] Integration tests for FastAPI endpoints
```

---

### Task 1: Remote I/O Abstraction Layer (Fetchers & Publishers)

**Files:**
- Create: `src/trivy_ai_report/io/__init__.py`
- Create: `src/trivy_ai_report/io/fetchers.py`
- Create: `src/trivy_ai_report/io/publishers.py`
- Test: `tests/test_io.py`

- [ ] **Step 1: Write failing test for InputFetchers and OutputPublishers**

```python
# tests/test_io.py
import pytest
from pathlib import Path
from trivy_ai_report.io.fetchers import fetch_input_bytes
from trivy_ai_report.io.publishers import publish_output_bytes

@pytest.mark.asyncio
async def test_fetch_local_file(tmp_path: Path):
    sample_file = tmp_path / "test.json"
    sample_file.write_text('{"test": True}', encoding="utf-8")
    
    content = await fetch_input_bytes(str(sample_file))
    assert content == b'{"test": True}'

@pytest.mark.asyncio
async def test_publish_local_file(tmp_path: Path):
    target_file = tmp_path / "output.html"
    await publish_output_bytes(str(target_file), b"<h1>Report</h1>", force=True)
    
    assert target_file.read_text(encoding="utf-8") == "<h1>Report</h1>"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_io.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'trivy_ai_report.io'`

- [ ] **Step 3: Implement `io/fetchers.py` and `io/publishers.py`**

```python
# src/trivy_ai_report/io/fetchers.py
from __future__ import annotations
from pathlib import Path
import httpx

async def fetch_input_bytes(source: str) -> bytes:
    if source.startswith(("http://", "https://")):
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(source)
            response.raise_for_status()
            return response.content
    
    path = Path(source)
    if not path.is_file():
        raise FileNotFoundError(f"Input file not found: {source}")
    return path.read_bytes()
```

```python
# src/trivy_ai_report/io/publishers.py
from __future__ import annotations
import os
from pathlib import Path
import tempfile
import httpx

async def publish_output_bytes(
    destination: str,
    content: bytes,
    *,
    force: bool = False,
    content_type: str = "text/html; charset=utf-8",
) -> str:
    if destination.startswith(("http://", "https://")):
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                destination,
                content=content,
                headers={"Content-Type": content_type},
            )
            response.raise_for_status()
            return f"Uploaded to {destination} (HTTP {response.status_code})"

    dest_path = Path(destination)
    if dest_path.exists() and not force:
        raise FileExistsError(f"Output exists: {dest_path}")
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(
        mode="wb",
        dir=dest_path.parent,
        prefix=f".{dest_path.name}.",
        suffix=".tmp",
        delete=False,
    ) as tmp:
        tmp.write(content)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, dest_path)
    return str(dest_path)
```

```python
# src/trivy_ai_report/io/__init__.py
from trivy_ai_report.io.fetchers import fetch_input_bytes
from trivy_ai_report.io.publishers import publish_output_bytes

__all__ = ["fetch_input_bytes", "publish_output_bytes"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_io.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/trivy_ai_report/io/ tests/test_io.py
git commit -m "feat(io): add async fetchers and publishers for local and remote HTTP I/O"
```

---

### Task 2: Decoupled Core Service Engine (`ReportPipeline`)

**Files:**
- Create: `src/trivy_ai_report/service.py`
- Test: `tests/test_service.py`

- [ ] **Step 1: Write failing test for `ReportPipeline`**

```python
# tests/test_service.py
import pytest
from trivy_ai_report.service import ReportPipeline, AnalysisRequest
from trivy_ai_report.trivy import parse_trivy_report

@pytest.mark.asyncio
async def test_pipeline_execution_without_findings():
    raw_trivy = {
        "SchemaVersion": 2,
        "ArtifactName": "test-app:v1",
        "ArtifactType": "container_image",
        "Results": []
    }
    pipeline = ReportPipeline()
    request = AnalysisRequest(
        provider="codex",
        input_data=raw_trivy,
    )
    result = await pipeline.run(request)
    assert result.success is True
    assert "未发现漏洞" in result.html_content or "Metadata" in result.html_content
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_service.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'trivy_ai_report.service'`

- [ ] **Step 3: Implement `src/trivy_ai_report/service.py`**

```python
# src/trivy_ai_report/service.py
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from trivy_ai_report.models import AnalysisOutcome
from trivy_ai_report.providers import ProviderError, create_analyzer
from trivy_ai_report.renderer import render_html
from trivy_ai_report.rules import merge_recommendations
from trivy_ai_report.skills import SkillSpec, load_skill
from trivy_ai_report.trivy import parse_trivy_report

@dataclass(slots=True)
class AnalysisRequest:
    provider: str
    input_data: dict[str, Any]
    model: str | None = None
    skill_path: str | Path | None = None
    enrich_web: bool = False
    timeout_seconds: float = 300.0

@dataclass(slots=True)
class PipelineResult:
    success: bool
    html_content: str
    outcome: AnalysisOutcome
    warnings: list[str] = field(default_factory=list)
    partial: bool = False

class ReportPipeline:
    """Decoupled analysis pipeline callable from CLI or Web API."""

    async def run(self, request: AnalysisRequest) -> PipelineResult:
        report = parse_trivy_report(request.input_data)
        skill: SkillSpec | None = None
        if request.skill_path is not None:
            skill = load_skill(request.skill_path)
        
        skill_name = skill.name if skill is not None else None

        if not report.findings:
            outcome = AnalysisOutcome(
                provider=request.provider,
                model=request.model or "provider-default",
                skill_name=skill_name,
                enrich_web=request.enrich_web,
                recommendations=[],
            )
            html = render_html(report, outcome)
            return PipelineResult(success=True, html_content=html, outcome=outcome)

        provider_failed = False
        failure_reason: str | None = None
        try:
            analyzer = create_analyzer(
                request.provider,
                model=request.model,
                enrich_web=request.enrich_web,
                batch_size=10 if request.enrich_web else 25,
                skill=skill,
            )
            outcome = await analyzer.analyze(
                report.findings,
                timeout_seconds=request.timeout_seconds,
            )
            outcome = outcome.model_copy(update={"skill_name": skill_name})
        except ProviderError as exc:
            provider_failed = True
            failure_reason = str(exc)
            outcome = AnalysisOutcome(
                provider=request.provider,
                model=request.model or "provider-default",
                skill_name=skill_name,
                enrich_web=request.enrich_web,
                partial=True,
                warnings=[f"Agent 分析失败：{failure_reason}"],
            )

        merged, validation_warnings, validation_partial = merge_recommendations(
            report.findings,
            outcome.recommendations,
            failure_reason=failure_reason,
        )
        final_outcome = outcome.model_copy(
            update={
                "recommendations": merged,
                "warnings": [*outcome.warnings, *validation_warnings],
                "partial": outcome.partial or validation_partial or provider_failed,
            }
        )
        html = render_html(report, final_outcome)
        return PipelineResult(
            success=True,
            html_content=html,
            outcome=final_outcome,
            warnings=final_outcome.warnings,
            partial=final_outcome.partial,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_service.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/trivy_ai_report/service.py tests/test_service.py
git commit -m "feat(service): add decoupled ReportPipeline service layer"
```

---

### Task 3: FastAPI Web Service & REST API Endpoints

**Files:**
- Create: `src/trivy_ai_report/web.py`
- Modify: `pyproject.toml:16-39` (Add `web` extra and `trivy-ai-report-serve` entrypoint)
- Test: `tests/test_web.py`

- [ ] **Step 1: Write failing test for Web API endpoints**

```python
# tests/test_web.py
import pytest
from fastapi.testclient import TestClient
from trivy_ai_report.web import app

client = TestClient(app)

def test_health_endpoint():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"

def test_analyze_endpoint_no_findings():
    trivy_data = {
        "SchemaVersion": 2,
        "ArtifactName": "api-demo:v1",
        "ArtifactType": "container_image",
        "Results": []
    }
    response = client.post("/api/v1/analyze", json={
        "provider": "codex",
        "trivy_json": trivy_data
    })
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert "html_report" in data
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_web.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'trivy_ai_report.web'`

- [ ] **Step 3: Update `pyproject.toml` and implement `src/trivy_ai_report/web.py`**

In `pyproject.toml`:
```toml
[project.optional-dependencies]
codex = ["openai-codex>=0.144.4"]
claude = ["claude-agent-sdk>=0.2.130,<1"]
gemini = ["google-adk>=2.6,<3"]
web = ["fastapi>=0.110", "uvicorn>=0.28", "httpx>=0.27"]
all = [
  "openai-codex>=0.144.4",
  "claude-agent-sdk>=0.2.130,<1",
  "google-adk>=2.6,<3",
  "fastapi>=0.110",
  "uvicorn>=0.28",
  "httpx>=0.27",
]

[project.scripts]
trivy-ai-report = "trivy_ai_report.cli:main"
trivy-report-codex = "trivy_ai_report.codex_cli:main"
trivy-report-claude = "trivy_ai_report.claude_cli:main"
trivy-report-gemini = "trivy_ai_report.gemini_cli:main"
trivy-ai-report-serve = "trivy_ai_report.web:main"
```

Implement `src/trivy_ai_report/web.py`:
```python
# src/trivy_ai_report/web.py
from __future__ import annotations

import argparse
import json
import sys
from typing import Any
from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, Field

from trivy_ai_report.io.fetchers import fetch_input_bytes
from trivy_ai_report.io.publishers import publish_output_bytes
from trivy_ai_report.service import AnalysisRequest, ReportPipeline

app = FastAPI(
    title="Trivy AI Report Service API",
    description="REST API for generating Chinese HTML remediation reports from Trivy JSON v2 using Codex, Claude, or Gemini Agents.",
    version="0.1.0",
)

class AnalyzeApiRequest(BaseModel):
    provider: str = Field(..., description="Agent Provider: codex, claude, or gemini")
    trivy_json: dict[str, Any] | None = Field(default=None, description="Direct Trivy JSON v2 payload")
    input_url: str | None = Field(default=None, description="Remote URL to fetch Trivy JSON from")
    upload_url: str | None = Field(default=None, description="Remote Webhook/HTTP URL to POST the HTML report to")
    model: str | None = None
    enrich_web: bool = False
    timeout_seconds: float = 300.0

class AnalyzeApiResponse(BaseModel):
    success: bool
    partial: bool
    warnings: list[str]
    html_report: str | None = None
    publish_result: str | None = None

@app.get("/health")
async def health_check():
    return {"status": "ok", "service": "trivy-ai-report"}

@app.post("/api/v1/analyze", response_model=AnalyzeApiResponse)
async def analyze_report(req: AnalyzeApiRequest):
    if not req.trivy_json and not req.input_url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Either trivy_json or input_url must be provided",
        )

    try:
        if req.input_url:
            raw_bytes = await fetch_input_bytes(req.input_url)
            trivy_data = json.loads(raw_bytes.decode("utf-8"))
        else:
            trivy_data = req.trivy_json
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to fetch or parse input Trivy JSON: {exc}",
        )

    pipeline = ReportPipeline()
    request = AnalysisRequest(
        provider=req.provider,
        input_data=trivy_data,
        model=req.model,
        enrich_web=req.enrich_web,
        timeout_seconds=req.timeout_seconds,
    )
    try:
        result = await pipeline.run(request)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Analysis pipeline error: {exc}",
        )

    publish_res: str | None = None
    if req.upload_url:
        try:
            publish_res = await publish_output_bytes(
                req.upload_url,
                result.html_content.encode("utf-8"),
            )
        except Exception as exc:
            result.warnings.append(f"Failed to upload report to webhook: {exc}")

    return AnalyzeApiResponse(
        success=result.success,
        partial=result.partial,
        warnings=result.warnings,
        html_report=result.html_content if not req.upload_url else None,
        publish_result=publish_res,
    )

def main():
    import uvicorn
    parser = argparse.ArgumentParser(description="Trivy AI Report Web API Server")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8000, help="Port to bind (default: 8000)")
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port)

if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_web.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/trivy_ai_report/web.py pyproject.toml tests/test_web.py
git commit -m "feat(web): add FastAPI web service mode and REST API endpoints"
```

---

### Task 4: Integration Verification & Complete Test Suite

- [ ] **Step 1: Run full unit test suite**

Run: `uv run pytest`
Expected: ALL PASS (including new `test_io.py`, `test_service.py`, `test_web.py`)

- [ ] **Step 2: Run linter**

Run: `uv run ruff check .`
Expected: All checks passed!

- [ ] **Step 3: Final Commit**

```bash
git add .
git commit -m "chore: complete dual-mode Web API service implementation plan"
```

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-08-07-dual-mode-web-service.md`. Two execution options:

**1. Subagent-Driven (recommended)** - Dispatch a fresh subagent per task, review between tasks, fast iteration.
**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?

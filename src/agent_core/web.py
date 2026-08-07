from __future__ import annotations

import argparse
import json
from typing import Any

from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, Field

from agent_core.io.fetchers import fetch_input_bytes
from agent_core.io.publishers import publish_output_bytes
from agent_core.pipeline import AgentPipeline, DomainAdapter


class FrameworkApiRequest(BaseModel):
    domain: str = Field(
        ..., description="Registered domain plugin name (e.g. trivy, linter, finops)"
    )
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
        result = await pipeline.run(
            provider=req.provider, input_data=input_data, enrich_web=req.enrich_web
        )

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


def main():
    import uvicorn

    parser = argparse.ArgumentParser(description="Agent Framework Web API Server")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8000, help="Port to bind (default: 8000)")
    args = parser.parse_args()
    app = create_app(adapters=[])
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()

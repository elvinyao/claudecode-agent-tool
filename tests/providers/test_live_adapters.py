from __future__ import annotations

import os
import shutil

import pytest
from pydantic import ConfigDict

from agent_core.contracts import AgentRequest, StrictFrozenModel
from agent_core.providers import create_provider

LIVE = os.environ.get("RUN_LIVE_AGENT_TESTS") == "1"


class LiveOutput(StrictFrozenModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    answer: str


@pytest.mark.live
@pytest.mark.skipif(not LIVE, reason="set RUN_LIVE_AGENT_TESTS=1 to enable paid live calls")
@pytest.mark.parametrize("provider_name", ["antigravity", "codex", "claude"])
@pytest.mark.asyncio
async def test_live_generic_structured_provider_smoke(provider_name: str) -> None:
    if (
        provider_name == "claude"
        and not os.environ.get("ANTHROPIC_API_KEY")
        and shutil.which("claude") is None
    ):
        # ty cannot resolve pytest 8's decorated skip signature.
        reason = "neither a local Claude CLI nor ANTHROPIC_API_KEY is configured"
        pytest.skip(reason)  # ty: ignore[too-many-positional-arguments]
    if (
        provider_name == "antigravity"
        and not os.environ.get("GEMINI_API_KEY")
        and os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").lower() != "true"
    ):
        # ty cannot resolve pytest 8's decorated skip signature.
        reason = "neither GEMINI_API_KEY nor Vertex AI authentication is configured"
        pytest.skip(reason)  # ty: ignore[too-many-positional-arguments]
    provider = create_provider(provider_name)
    request = AgentRequest[LiveOutput](
        request_id=f"live-{provider_name}",
        system_prompt="Return only the requested structured object.",
        prompt="Set answer to exactly: agent-core-live-ok",
        response_model=LiveOutput,
    )

    result = await provider.execute(request, timeout_seconds=300)

    assert result.output.answer == "agent-core-live-ok"

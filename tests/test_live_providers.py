from __future__ import annotations

import os
from pathlib import Path

import pytest

from trivy_ai_report.providers import create_analyzer
from trivy_ai_report.trivy import load_trivy_report

LIVE = os.getenv("RUN_LIVE_AGENT_TESTS") == "1"


@pytest.mark.skipif(not LIVE, reason="set RUN_LIVE_AGENT_TESTS=1 to enable paid live calls")
@pytest.mark.parametrize("provider", ["codex", "claude", "gemini"])
@pytest.mark.asyncio
async def test_live_provider_smoke(provider: str) -> None:
    if provider == "claude" and not os.getenv("ANTHROPIC_API_KEY"):
        pytest.skip("ANTHROPIC_API_KEY is not configured")
    if provider == "codex" and not os.getenv("CODEX_API_KEY"):
        pytest.skip("CODEX_API_KEY is not configured for an unattended test")
    if provider == "gemini" and not os.getenv("GOOGLE_API_KEY"):
        pytest.skip("GOOGLE_API_KEY is not configured")

    root = Path(__file__).resolve().parents[1]
    report = load_trivy_report(root / "examples" / "trivy-spring-boot.json")
    analyzer = create_analyzer(provider, enrich_web=False, batch_size=1)
    outcome = await analyzer.analyze(report.findings[:1], timeout_seconds=300)

    assert outcome.recommendations
    assert outcome.recommendations[0].finding_id == report.findings[0].finding_id

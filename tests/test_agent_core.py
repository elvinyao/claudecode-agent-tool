import pytest

from agent_core.models.contracts import AnalysisOutcome, BaseAdvice, BaseFact
from agent_core.pipeline import AgentPipeline, DomainAdapter


class DummyAdapter(DomainAdapter):
    name = "dummy"

    def parse_input(self, raw_data: dict) -> list[BaseFact]:
        return [BaseFact(fact_id="1", summary="test")]

    def build_prompt(self, facts: list[BaseFact], enrich_web: bool) -> str:
        return "analyze"

    def validate_and_merge(
        self, facts: list[BaseFact], raw_advices: list, failure_reason: str | None
    ):
        advice = BaseAdvice(fact_id="1", title_zh="test", actions_zh=["act"])
        return [advice], [], False

    def render_output(self, facts: list[BaseFact], outcome: AnalysisOutcome) -> str:
        return "<html>OK</html>"


@pytest.mark.asyncio
async def test_dummy_pipeline_execution():
    pipeline = AgentPipeline(adapter=DummyAdapter())
    result = await pipeline.run(provider="codex", input_data={"dummy": True})
    assert result.success is True
    assert result.content == "<html>OK</html>"

import pytest
from pydantic import ValidationError

from agent_core.models.contracts import AnalysisOutcome, BaseAdvice, BaseFact
from agent_core.pipeline import AgentPipeline, DomainAdapter


class DummyAdapter(DomainAdapter):
    name = "dummy"

    def __init__(self, return_empty: bool = False):
        self.return_empty = return_empty
        self.build_prompt_called_with_enrich_web: bool | None = None

    def parse_input(self, raw_data: dict) -> list[BaseFact]:
        if self.return_empty:
            return []
        return [BaseFact(fact_id="1", summary="test")]

    def build_prompt(self, facts: list[BaseFact], enrich_web: bool) -> str:
        self.build_prompt_called_with_enrich_web = enrich_web
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


@pytest.mark.asyncio
async def test_empty_input_pipeline_execution():
    adapter = DummyAdapter(return_empty=True)
    pipeline = AgentPipeline(adapter=adapter)
    result = await pipeline.run(provider="codex", input_data={})

    assert result.success is True
    assert result.outcome.recommendations == []
    assert result.outcome.provider == "codex"
    assert result.outcome.model == "provider-default"
    assert result.outcome.enrich_web is False


@pytest.mark.asyncio
async def test_parameter_propagation():
    adapter = DummyAdapter()
    pipeline = AgentPipeline(adapter=adapter)
    result = await pipeline.run(
        provider="openai",
        input_data={"dummy": True},
        model="gpt-4o",
        skill_path="/path/to/skill",
        enrich_web=True,
    )

    assert result.outcome.provider == "openai"
    assert result.outcome.model == "gpt-4o"
    assert result.outcome.skill_name == "/path/to/skill"
    assert result.outcome.enrich_web is True
    assert adapter.build_prompt_called_with_enrich_web is True

    # Test default model fallback
    result_default = await pipeline.run(provider="claude", input_data={"dummy": True})
    assert result_default.outcome.model == "provider-default"
    assert result_default.outcome.skill_name is None


def test_base_fact_immutability():
    fact = BaseFact(fact_id="f1", summary="Initial summary")
    assert fact.fact_id == "f1"
    assert fact.summary == "Initial summary"

    with pytest.raises(ValidationError):
        fact.summary = "Updated summary"  # type: ignore[misc]


def test_base_advice_validation():
    advice = BaseAdvice(fact_id="f1", title_zh="测试标题", actions_zh=["动作1"])
    assert advice.fact_id == "f1"
    assert advice.title_zh == "测试标题"
    assert advice.actions_zh == ["动作1"]

    with pytest.raises(ValidationError):
        BaseAdvice(fact_id="f1", title_zh="Missing actions")

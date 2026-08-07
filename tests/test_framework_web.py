from fastapi.testclient import TestClient

from agent_core.models.contracts import AnalysisOutcome, BaseFact
from agent_core.pipeline import DomainAdapter
from agent_core.web import create_app


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

def test_health_endpoint():
    response = client.get("/health")
    assert response.status_code == 200
    assert "mock_domain" in response.json()["domains"]

def test_universal_analyze_endpoint():
    response = client.post("/api/v1/analyze", json={
        "domain": "mock_domain",
        "provider": "codex",
        "input_json": {"test": True}
    })
    assert response.status_code == 200
    assert response.json()["content"] == "mock_output"

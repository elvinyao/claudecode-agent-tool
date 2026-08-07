from __future__ import annotations

from trivy_ai_report.plugin import TrivyDomainAdapter


def test_trivy_domain_adapter_name():
    adapter = TrivyDomainAdapter()
    assert adapter.name == "trivy"


def test_trivy_domain_adapter_parse():
    adapter = TrivyDomainAdapter()
    raw = {
        "SchemaVersion": 2,
        "ArtifactName": "test:v1",
        "ArtifactType": "container_image",
        "Results": [],
    }
    facts = adapter.parse_input(raw)
    assert isinstance(facts, list)


def test_trivy_domain_adapter_build_prompt():
    adapter = TrivyDomainAdapter()
    raw = {
        "SchemaVersion": 2,
        "ArtifactName": "test:v1",
        "ArtifactType": "container_image",
        "Results": [],
    }
    facts = adapter.parse_input(raw)
    prompt = adapter.build_prompt(facts, enrich_web=False)
    assert isinstance(prompt, str)


def test_trivy_domain_adapter_validate_and_merge():
    adapter = TrivyDomainAdapter()
    facts = []
    merged, warnings, partial = adapter.validate_and_merge(facts, [], None)
    assert isinstance(merged, list)
    assert isinstance(warnings, list)
    assert isinstance(partial, bool)


def test_trivy_domain_adapter_render_output():
    adapter = TrivyDomainAdapter()
    raw = {
        "SchemaVersion": 2,
        "ArtifactName": "test:v1",
        "ArtifactType": "container_image",
        "Results": [],
    }
    facts = adapter.parse_input(raw)
    from agent_core.models.contracts import AnalysisOutcome

    outcome = AnalysisOutcome(provider="codex", model="default")
    html = adapter.render_output(facts, outcome)
    assert isinstance(html, str)
    assert "<html" in html.lower() or "<!doctype html>" in html.lower()

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from trivy_ai_report import claude_cli, cli, codex_cli, gemini_cli
from trivy_ai_report.models import (
    AnalysisOutcome,
    Recommendation,
    RecommendationCategory,
    ResearchStatus,
    VersionSource,
)
from trivy_ai_report.providers import ProviderError
from trivy_ai_report.skills import SkillError


def write_report(path: Path, *, findings: bool = True) -> None:
    vulnerabilities = []
    if findings:
        vulnerabilities.append(
            {
                "VulnerabilityID": "CVE-2099-0001",
                "PkgName": "demo-lib",
                "InstalledVersion": "1.0.0",
                "FixedVersion": "1.0.1",
                "Status": "fixed",
                "Severity": "HIGH",
                "Title": "Demo vulnerability",
            }
        )
    path.write_text(
        json.dumps(
            {
                "SchemaVersion": 2,
                "ArtifactName": "demo:1.0",
                "ArtifactType": "container_image",
                "Results": [
                    {
                        "Target": "app.jar",
                        "Class": "lang-pkgs",
                        "Type": "jar",
                        "Vulnerabilities": vulnerabilities,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


class SuccessAnalyzer:
    async def analyze(self, findings, *, timeout_seconds):
        finding = findings[0]
        return AnalysisOutcome(
            provider="codex",
            recommendations=[
                Recommendation(
                    finding_id=finding.finding_id,
                    category=RecommendationCategory.DEPENDENCY_UPGRADE,
                    title_zh="升级 demo-lib",
                    rationale_zh="Trivy 已给出修复版本。",
                    actions_zh=["将 demo-lib 升级到 1.0.1。"],
                    validation_zh=["重新运行 Trivy。"],
                    recommended_version="1.0.1",
                    version_source=VersionSource.TRIVY_FIXED_VERSION,
                    confidence="high",
                    research_status=ResearchStatus.NOT_REQUESTED,
                )
            ],
        )


class FailingAnalyzer:
    async def analyze(self, findings, *, timeout_seconds):
        raise ProviderError("测试认证失败")


def test_cli_success(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "report.json"
    output = tmp_path / "report.html"
    write_report(source)
    monkeypatch.setattr(cli, "create_analyzer", lambda *args, **kwargs: SuccessAnalyzer())

    code = cli.main(
        [
            "--provider",
            "codex",
            "--input",
            str(source),
            "--output",
            str(output),
        ]
    )

    assert code == cli.EXIT_OK
    assert output.exists()
    assert "升级 demo-lib" in output.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("command", "provider", "program"),
    [
        (codex_cli.main, "codex", "trivy-report-codex"),
        (claude_cli.main, "claude", "trivy-report-claude"),
        (gemini_cli.main, "gemini", "trivy-report-gemini"),
    ],
)
def test_fixed_provider_commands_hide_provider_flag_and_select_provider(
    monkeypatch,
    tmp_path: Path,
    command,
    provider: str,
    program: str,
) -> None:
    source = tmp_path / f"{provider}.json"
    output = tmp_path / f"{provider}.html"
    write_report(source)
    calls = []

    def create(selected_provider, **kwargs):
        calls.append((selected_provider, kwargs))
        return SuccessAnalyzer()

    monkeypatch.setattr(cli, "create_analyzer", create)

    code = command(["--input", str(source), "--output", str(output)])

    assert code == cli.EXIT_OK
    assert calls[0][0] == provider
    assert calls[0][1]["skill"] is None
    help_text = cli.build_parser(fixed_provider=provider, prog=program).format_help()
    assert help_text.startswith(f"usage: {program}")
    assert "--provider" not in help_text
    assert "--skill PATH" in help_text


@pytest.mark.parametrize(
    ("command", "other_provider"),
    [
        (codex_cli.main, "claude"),
        (claude_cli.main, "gemini"),
        (gemini_cli.main, "codex"),
    ],
)
def test_fixed_provider_command_rejects_provider_argument(
    command,
    other_provider: str,
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        command(["--provider", other_provider])

    assert exc_info.value.code == cli.EXIT_INPUT_ERROR


def test_skill_is_loaded_forwarded_and_recorded(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "report.json"
    output = tmp_path / "report.html"
    skill_path = tmp_path / "remediation-skill"
    write_report(source)
    skill = SimpleNamespace(name="corp-trivy-remediation")
    calls = []

    def fake_load(path):
        assert path == skill_path
        return skill

    def create(provider, **kwargs):
        calls.append((provider, kwargs))
        return SuccessAnalyzer()

    monkeypatch.setattr(cli, "load_skill", fake_load)
    monkeypatch.setattr(cli, "create_analyzer", create)

    code = codex_cli.main(
        [
            "--input",
            str(source),
            "--output",
            str(output),
            "--skill",
            str(skill_path),
        ]
    )

    assert code == cli.EXIT_OK
    assert calls == [
        ("codex", {"model": None, "enrich_web": False, "batch_size": 25, "skill": skill})
    ]
    assert "corp-trivy-remediation" in output.read_text(encoding="utf-8")


def test_invalid_skill_returns_input_error_without_calling_provider(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    source = tmp_path / "report.json"
    output = tmp_path / "report.html"
    write_report(source)

    def fail_load(path):
        raise SkillError("缺少 YAML frontmatter")

    def should_not_run(*args, **kwargs):  # pragma: no cover - assertion helper
        raise AssertionError("provider should not run for an invalid skill")

    monkeypatch.setattr(cli, "load_skill", fail_load)
    monkeypatch.setattr(cli, "create_analyzer", should_not_run)

    code = claude_cli.main(
        [
            "--input",
            str(source),
            "--output",
            str(output),
            "--skill",
            str(tmp_path / "bad-skill"),
        ]
    )

    assert code == cli.EXIT_INPUT_ERROR
    assert not output.exists()
    assert "Skill 输入错误" in capsys.readouterr().err


def test_cli_provider_failure_writes_degraded_report(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "report.json"
    output = tmp_path / "report.html"
    write_report(source)
    monkeypatch.setattr(cli, "create_analyzer", lambda *args, **kwargs: FailingAnalyzer())

    code = cli.main(
        [
            "--provider",
            "claude",
            "--input",
            str(source),
            "--output",
            str(output),
        ]
    )

    assert code == cli.EXIT_DEGRADED
    assert output.exists()
    html = output.read_text(encoding="utf-8")
    assert "测试认证失败" in html
    assert "人工" in html


def test_cli_empty_report_skips_provider(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "empty.json"
    output = tmp_path / "empty.html"
    write_report(source, findings=False)

    def should_not_run(*args, **kwargs):  # pragma: no cover - assertion helper
        raise AssertionError("provider should not run for an empty report")

    monkeypatch.setattr(cli, "create_analyzer", should_not_run)
    code = cli.main(
        [
            "--provider",
            "codex",
            "--input",
            str(source),
            "--output",
            str(output),
        ]
    )

    assert code == cli.EXIT_OK
    assert output.exists()


def test_cli_refuses_to_overwrite_without_force(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "report.json"
    output = tmp_path / "existing.html"
    write_report(source)
    output.write_text("keep me", encoding="utf-8")

    def should_not_run(*args, **kwargs):  # pragma: no cover - assertion helper
        raise AssertionError("provider should not run when output is rejected")

    monkeypatch.setattr(cli, "create_analyzer", should_not_run)
    code = cli.main(
        [
            "--provider",
            "codex",
            "--input",
            str(source),
            "--output",
            str(output),
        ]
    )

    assert code == cli.EXIT_INPUT_ERROR
    assert output.read_text(encoding="utf-8") == "keep me"


def test_cli_invalid_input_does_not_create_output(tmp_path: Path) -> None:
    source = tmp_path / "invalid.json"
    output = tmp_path / "report.html"
    source.write_text("not json", encoding="utf-8")

    code = cli.main(
        [
            "--provider",
            "codex",
            "--input",
            str(source),
            "--output",
            str(output),
        ]
    )

    assert code == cli.EXIT_INPUT_ERROR
    assert not output.exists()

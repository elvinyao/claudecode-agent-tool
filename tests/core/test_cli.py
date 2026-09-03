from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from agent_core import cli
from agent_core.contracts import RunStatus
from agent_core.providers.diagnostics import ProviderDiagnostic, ProviderReadiness
from agent_core.providers.registry import ProviderRegistry
from agent_core.registry import PluginRegistry
from agent_core.runtime import RuntimeArtifact, RuntimeResult


class StubRuntime:
    def __init__(self, registry: PluginRegistry, *, partial: bool = False) -> None:
        self.plugin_registry = registry
        self.partial = partial
        self.calls: list[dict[str, Any]] = []

    async def run(self, **values: Any) -> RuntimeResult:
        self.calls.append(values)
        return RuntimeResult(
            run_id="cli-run",
            plugin_id=values["plugin_id"],
            provider=values["provider"],
            model=values.get("model") or "fake-model",
            status=RunStatus.DEGRADED if self.partial else RunStatus.SUCCEEDED,
            artifact=RuntimeArtifact(
                content=b"complete artifact\n",
                media_type="text/plain",
                filename="result.txt",
            ),
            warnings=("deterministic fallback used",) if self.partial else (),
            partial=self.partial,
        )


def descriptor_registry() -> PluginRegistry:
    manifest = SimpleNamespace(
        plugin_id="demo",
        api_version="1.0",
        version="1.2.3",
        display_name="Demo",
        input_model=None,
        options_model=None,
        output_model=None,
        required_capabilities=(),
    )
    descriptor = SimpleNamespace(
        plugin_id="demo",
        version="1.2.3",
        api_version="1.0",
        display_name="Demo",
        source="entrypoint:demo.plugin:create_plugin",
        required_capabilities=("structured_output",),
        input_schema={"type": "object", "required": ["content"]},
        options_schema={"type": "object", "properties": {"flag": {"type": "boolean"}}},
        output_schema={"type": "object", "required": ["result"]},
    )

    class Registry(PluginRegistry):
        def __init__(self) -> None:
            pass

        def list(self):
            return (descriptor,)

        def get(self, plugin_id: str):
            assert plugin_id == "demo"
            return descriptor

    del manifest
    return Registry()


def test_plugins_list_does_not_construct_a_runtime(capsys) -> None:
    registry = descriptor_registry()

    assert cli.main(["plugins", "list"], plugin_registry=registry) == cli.EXIT_OK

    output = capsys.readouterr().out
    assert "PLUGIN\tVERSION\tAPI\tNAME" in output
    assert "demo\t1.2.3\t1.0\tDemo" in output


def test_plugins_describe_emits_public_descriptor_as_json(capsys) -> None:
    registry = descriptor_registry()

    code = cli.main(
        ["plugins", "describe", "demo", "--json"],
        plugin_registry=registry,
    )

    assert code == cli.EXIT_OK
    assert json.loads(capsys.readouterr().out) == {
        "plugin_id": "demo",
        "display_name": "Demo",
        "version": "1.2.3",
        "api_version": "1.0",
        "source": "entrypoint:demo.plugin:create_plugin",
        "required_capabilities": ["structured_output"],
        "input_schema": {"type": "object", "required": ["content"]},
        "options_schema": {
            "type": "object",
            "properties": {"flag": {"type": "boolean"}},
        },
        "output_schema": {"type": "object", "required": ["result"]},
    }


def test_plugins_describe_text_includes_all_schema_sections(capsys) -> None:
    code = cli.main(
        ["plugins", "describe", "demo"],
        plugin_registry=descriptor_registry(),
    )

    assert code == cli.EXIT_OK
    output = capsys.readouterr().out
    assert "PLUGIN\tdemo" in output
    assert "SOURCE\tentrypoint:demo.plugin:create_plugin" in output
    assert "CAPABILITIES\tstructured_output" in output
    assert "INPUT_SCHEMA\n" in output
    assert "OPTIONS_SCHEMA\n" in output
    assert "OUTPUT_SCHEMA\n" in output


def test_plugins_describe_rejects_unknown_plugin(capsys) -> None:
    code = cli.main(
        ["plugins", "describe", "missing"],
        plugin_registry=PluginRegistry(),
    )

    assert code == cli.EXIT_INPUT_ERROR
    assert "unknown plugin: 'missing'" in capsys.readouterr().err


def test_doctor_reports_offline_provider_and_plugin_readiness_as_json(
    monkeypatch,
    capsys,
) -> None:
    providers = ProviderRegistry()
    providers.register("fake", lambda **_values: object())  # type: ignore[arg-type]
    monkeypatch.setattr(
        cli,
        "diagnose_provider_runtime",
        lambda name: ProviderDiagnostic(
            name=name,
            status=ProviderReadiness.READY,
            runtime="sdk",
            detail="fake-sdk 1.0 is installed.",
        ),
    )
    monkeypatch.setattr(
        cli,
        "_default_runtime",
        lambda *_args: (_ for _ in ()).throw(AssertionError("runtime constructed")),
    )

    code = cli.main(
        ["doctor", "--provider", "FAKE", "--json"],
        plugin_registry=descriptor_registry(),
        provider_registry=providers,
    )

    assert code == cli.EXIT_OK
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "ready"
    assert report["scope"] == {
        "offline": True,
        "authentication_checked": False,
        "network_checked": False,
    }
    assert report["providers"] == [
        {
            "name": "fake",
            "status": "ready",
            "runtime": "sdk",
            "detail": "fake-sdk 1.0 is installed.",
        }
    ]
    assert report["plugins"] == [
        {
            "id": "demo",
            "version": "1.2.3",
            "api_version": "1.0",
            "name": "Demo",
        }
    ]


def test_doctor_returns_degraded_for_explicit_unavailable_provider(
    monkeypatch,
    capsys,
) -> None:
    providers = ProviderRegistry()
    providers.register("fake", lambda **_values: object())  # type: ignore[arg-type]
    monkeypatch.setattr(
        cli,
        "diagnose_provider_runtime",
        lambda name: ProviderDiagnostic(
            name=name,
            status=ProviderReadiness.UNAVAILABLE,
            runtime="none",
            detail="Install the provider runtime.",
        ),
    )

    code = cli.main(
        ["doctor", "--provider", "fake"],
        plugin_registry=descriptor_registry(),
        provider_registry=providers,
    )

    assert code == cli.EXIT_DEGRADED
    output = capsys.readouterr().out
    assert "DOCTOR\tDEGRADED" in output
    assert "offline only; authentication and network access were not checked" in output
    assert "fake\tunavailable\tnone\tInstall the provider runtime." in output


def test_doctor_rejects_an_unknown_provider(capsys) -> None:
    providers = ProviderRegistry()
    providers.register("fake", lambda **_values: object())  # type: ignore[arg-type]

    code = cli.main(
        ["doctor", "--provider", "missing"],
        plugin_registry=descriptor_registry(),
        provider_registry=providers,
    )

    assert code == cli.EXIT_INPUT_ERROR
    assert "unknown provider 'missing'; available providers: fake" in capsys.readouterr().err


def test_run_writes_atomically_and_returns_degraded_exit_code(
    tmp_path: Path,
    capsys,
) -> None:
    source = tmp_path / "input.json"
    destination = tmp_path / "nested" / "result.txt"
    source.write_bytes(b'{"safe":true}')
    runtime = StubRuntime(descriptor_registry(), partial=True)

    code = cli.main(
        [
            "run",
            "--plugin",
            "demo",
            "--provider",
            "fake",
            "--input",
            str(source),
            "--output",
            str(destination),
            "--options-json",
            '{"flag":true}',
            "--timeout-seconds",
            "12.5",
        ],
        runtime=runtime,  # type: ignore[arg-type]
    )

    assert code == cli.EXIT_DEGRADED
    assert destination.read_bytes() == b"complete artifact\n"
    assert runtime.calls[0]["input_bytes"] == b'{"safe":true}'
    assert runtime.calls[0]["options"] == {"flag": True}
    assert runtime.calls[0]["deadline_seconds"] == 12.5
    assert "deterministic fallback used" in capsys.readouterr().err


def test_invalid_options_or_existing_output_does_not_mutate_destination(
    tmp_path: Path,
) -> None:
    source = tmp_path / "input.json"
    destination = tmp_path / "result.txt"
    source.write_bytes(b"{}")
    destination.write_text("keep", encoding="utf-8")
    runtime = StubRuntime(descriptor_registry())

    existing_code = cli.main(
        [
            "run",
            "--plugin",
            "demo",
            "--provider",
            "fake",
            "--input",
            str(source),
            "--output",
            str(destination),
        ],
        runtime=runtime,  # type: ignore[arg-type]
    )
    invalid_options_code = cli.main(
        [
            "run",
            "--plugin",
            "demo",
            "--provider",
            "fake",
            "--input",
            str(source),
            "--output",
            str(tmp_path / "new.txt"),
            "--options-json",
            "[]",
        ],
        runtime=runtime,  # type: ignore[arg-type]
    )

    assert existing_code == cli.EXIT_INPUT_ERROR
    assert invalid_options_code == cli.EXIT_INPUT_ERROR
    assert destination.read_text(encoding="utf-8") == "keep"
    assert not (tmp_path / "new.txt").exists()
    assert runtime.calls == []

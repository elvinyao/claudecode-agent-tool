from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from agent_core import cli
from agent_core.contracts import RunStatus
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
        options_schema={"properties": {}},
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

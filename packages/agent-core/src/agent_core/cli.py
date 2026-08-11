"""Command-line composition root for the generic Agent Framework."""

from __future__ import annotations

import argparse
import asyncio
import errno
import json
import os
import sys
import tempfile
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from agent_core.audit import AuditLogger, JsonlAuditSink
from agent_core.contracts import ActionMode, RunStatus
from agent_core.providers import DEFAULT_PROVIDER_REGISTRY, ProviderRegistry
from agent_core.providers.errors import (
    ProviderCapabilityError,
    ProviderConfigurationError,
    ProviderUnavailableError,
)
from agent_core.registry import PluginRegistry, PluginRegistryError
from agent_core.runtime import (
    AgentRuntime,
    RuntimeArtifact,
    RuntimeConfigurationError,
    RuntimeInputError,
)
from agent_core.skills import SkillError

EXIT_OK = 0
EXIT_RUNTIME_ERROR = 1
EXIT_INPUT_ERROR = 2
EXIT_DEGRADED = 3
EXIT_INTERRUPTED = 130


class LocalFileError(RuntimeInputError):
    """A CLI input/output path is invalid or unsafe to publish."""


def build_parser(*, prog: str = "agent-core") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Run deterministic + AI domain workflows through local providers.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    plugins = commands.add_parser("plugins", help="Inspect installed domain plugins.")
    plugin_commands = plugins.add_subparsers(dest="plugins_command", required=True)
    plugin_commands.add_parser("list", help="List registered domain plugins.")

    run = commands.add_parser("run", help="Run one domain plugin on a local file.")
    run.add_argument("--plugin", required=True, metavar="ID")
    run.add_argument("--provider", required=True, metavar="NAME")
    run.add_argument("--input", required=True, type=Path, metavar="PATH")
    run.add_argument("--output", required=True, type=Path, metavar="PATH")
    run.add_argument("--model", metavar="NAME")
    run.add_argument("--skill", type=Path, metavar="PATH")
    run.add_argument(
        "--options-json",
        default="{}",
        metavar="OBJECT",
        help="Plugin options as one JSON object (default: {}).",
    )
    run.add_argument(
        "--action-mode",
        choices=tuple(mode.value for mode in ActionMode),
        default=ActionMode.DISABLED.value,
    )
    run.add_argument(
        "--timeout-seconds",
        type=_positive_seconds,
        metavar="SECONDS",
        help="Total workflow deadline (default: framework policy).",
    )
    run.add_argument(
        "--force",
        action="store_true",
        help="Atomically replace an existing output file.",
    )

    serve = commands.add_parser("serve", help="Run the optional FastAPI service.")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", default=8000, type=int)
    serve.add_argument(
        "--allow-https-server",
        action="append",
        default=[],
        metavar="HOST[:PORT]",
        help="Exact HTTPS source/sink server; repeat to add more.",
    )
    serve.add_argument(
        "--allow-web-enrichment",
        action="store_true",
        help="Permit requests to authorize provider web tools.",
    )
    serve.add_argument(
        "--max-action-mode",
        choices=tuple(mode.value for mode in ActionMode),
        default=ActionMode.DISABLED.value,
        help="Maximum ActionNode authority exposed by this server.",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    plugin_registry: PluginRegistry | None = None,
    provider_registry: ProviderRegistry = DEFAULT_PROVIDER_REGISTRY,
    runtime: AgentRuntime | None = None,
) -> int:
    """Run the CLI and return its documented process exit code."""

    args = build_parser().parse_args(argv)
    try:
        registry = plugin_registry or (
            runtime.plugin_registry
            if runtime is not None
            else PluginRegistry.from_entry_points()
        )
        if args.command == "plugins":
            return _list_plugins(registry)
        effective_runtime = runtime or _default_runtime(registry, provider_registry)
        if args.command == "run":
            return asyncio.run(_run_local(args, effective_runtime))
        if args.command == "serve":
            return _serve(args, registry, provider_registry, effective_runtime)
        raise RuntimeConfigurationError(f"unsupported command: {args.command}")
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("interrupted", file=sys.stderr)
        return EXIT_INTERRUPTED
    except Exception as exc:
        code = EXIT_INPUT_ERROR if _is_input_or_configuration_error(exc) else EXIT_RUNTIME_ERROR
        print(f"error: {_safe_error_message(exc)}", file=sys.stderr)
        return code


def _list_plugins(registry: PluginRegistry) -> int:
    descriptors = registry.list()
    print("PLUGIN\tVERSION\tAPI\tNAME")
    for descriptor in descriptors:
        print(
            f"{descriptor.plugin_id}\t{descriptor.version}\t"
            f"{descriptor.api_version}\t{descriptor.display_name}"
        )
    return EXIT_OK


async def _run_local(args: argparse.Namespace, runtime: AgentRuntime) -> int:
    input_path = args.input
    output_path = args.output
    _preflight_output(output_path, force=args.force)
    content = read_local_input(input_path)
    options = parse_options_json(args.options_json)
    result = await runtime.run(
        plugin_id=args.plugin,
        provider=args.provider,
        input_bytes=content,
        input_filename=input_path.name,
        options=options,
        model=args.model,
        skill=args.skill,
        action_mode=ActionMode(args.action_mode),
        deadline_seconds=args.timeout_seconds,
    )
    write_artifact_atomic(result.artifact, output_path, force=args.force)
    for warning in result.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    print(str(output_path))
    return EXIT_DEGRADED if result.status is RunStatus.DEGRADED else EXIT_OK


def parse_options_json(value: str) -> dict[str, Any]:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise RuntimeInputError(
            f"--options-json is invalid JSON at line {exc.lineno}, column {exc.colno}"
        ) from exc
    if not isinstance(decoded, dict):
        raise RuntimeInputError("--options-json must decode to a JSON object")
    return decoded


def read_local_input(path: Path) -> bytes:
    """Read exactly one local regular file after an explicit path check."""

    try:
        metadata = path.stat()
    except OSError as exc:
        raise LocalFileError(f"cannot inspect input file {path}: {exc.strerror}") from exc
    if not path.is_file():
        raise LocalFileError(f"input path is not a regular file: {path}")
    if metadata.st_size < 0:  # defensive for unusual virtual files
        raise LocalFileError(f"input file has an invalid size: {path}")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise LocalFileError(f"cannot read input file {path}: {exc.strerror}") from exc


def _preflight_output(path: Path, *, force: bool) -> None:
    if path.exists() and not force:
        raise LocalFileError(f"output already exists (use --force to replace it): {path}")
    if path.exists() and path.is_dir():
        raise LocalFileError(f"output path is a directory: {path}")
    parent = path.parent
    if parent.exists() and not parent.is_dir():
        raise LocalFileError(f"output parent is not a directory: {parent}")


def write_artifact_atomic(
    artifact: RuntimeArtifact,
    destination: Path,
    *,
    force: bool = False,
) -> None:
    """Publish a complete artifact atomically, without overwriting by default.

    The no-overwrite path uses a same-filesystem hard link from a fully flushed
    temporary file. Unlike an existence check followed by ``replace``, this
    remains race-safe when another process creates the destination concurrently.
    """

    _preflight_output(destination, force=force)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise LocalFileError(
            f"cannot create output directory {destination.parent}: {exc.strerror}"
        ) from exc

    file_descriptor = -1
    temporary_name: str | None = None
    try:
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
        )
        with os.fdopen(file_descriptor, "wb") as stream:
            file_descriptor = -1
            stream.write(artifact.content)
            stream.flush()
            os.fsync(stream.fileno())
        if force:
            os.replace(temporary_name, destination)
            temporary_name = None
        else:
            try:
                os.link(temporary_name, destination)
            except FileExistsError as exc:
                raise LocalFileError(
                    f"output already exists (use --force to replace it): {destination}"
                ) from exc
    except LocalFileError:
        raise
    except OSError as exc:
        detail = exc.strerror or str(exc)
        if exc.errno == errno.EISDIR:
            detail = "destination is a directory"
        raise LocalFileError(f"cannot write output file {destination}: {detail}") from exc
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)
        if temporary_name is not None:
            with suppress(FileNotFoundError):
                os.unlink(temporary_name)


def _positive_seconds(value: str) -> float:
    try:
        result = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if result <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return result


def _default_runtime(
    registry: PluginRegistry,
    provider_registry: ProviderRegistry,
) -> AgentRuntime:
    configured_path = os.environ.get("AGENT_CORE_AUDIT_PATH")
    sink = JsonlAuditSink(Path(configured_path)) if configured_path else JsonlAuditSink()
    return AgentRuntime(
        registry,
        provider_registry=provider_registry,
        audit_logger=AuditLogger(sink),
    )


def _serve(
    args: argparse.Namespace,
    registry: PluginRegistry,
    provider_registry: ProviderRegistry,
    runtime: AgentRuntime,
) -> int:
    """Import Web-only dependencies after the serve command is selected."""

    from agent_core.io import HttpsIoPolicy
    from agent_core.jobs import JobExecutionResult, RunArtifact
    from agent_core.web import WebSettings, create_app, uvicorn_settings

    try:
        import uvicorn
    except ImportError as exc:
        raise RuntimeConfigurationError(
            "serve requires the agent-core[web] optional dependencies"
        ) from exc

    settings = WebSettings(
        host=args.host,
        port=args.port,
        api_token=os.environ.get("AGENT_CORE_API_TOKEN"),
        max_action_mode=ActionMode(args.max_action_mode),
        allow_web_enrichment=args.allow_web_enrichment,
        https_policy=HttpsIoPolicy(allowed_servers=frozenset(args.allow_https_server)),
    )

    async def execute(request: Any, job_context: Any) -> JobExecutionResult:
        job_context.raise_if_cancelled()
        options: dict[str, Any] = dict(request.parameters)
        descriptor = registry.get(request.plugin_id)
        option_fields = descriptor.options_schema.get("properties", {})
        if "enrich_web" in option_fields:
            # The dedicated, policy-checked Web option is authoritative.  A
            # caller cannot smuggle a different value through parameters.
            options["enrich_web"] = request.enrich_web
        result = await runtime.run(
            plugin_id=request.plugin_id,
            provider=request.provider,
            input_bytes=request.input_bytes,
            input_filename=request.input_filename,
            options=options,
            model=request.model,
            action_mode=request.action_mode,
            allow_web_access=(
                settings.allow_web_enrichment and request.enrich_web
            ),
            deadline_seconds=request.timeout_seconds,
            cancel_event=job_context.cancel_event,
            run_id=job_context.run_id,
            metadata={"input_media_type": request.input_media_type},
        )
        return JobExecutionResult(
            artifact=RunArtifact(
                content=result.artifact.content,
                media_type=result.artifact.media_type,
                filename=result.artifact.filename,
            ),
            warnings=result.warnings,
            partial=result.partial,
            metadata={
                "plugin_id": result.plugin_id,
                "provider": result.provider,
                "model": result.model,
            },
        )

    app = create_app(
        registry=registry,
        provider_names=provider_registry.names,
        run_executor=execute,
        settings=settings,
    )
    uvicorn.run(app, **uvicorn_settings(settings))
    return EXIT_OK


def _is_input_or_configuration_error(exc: BaseException) -> bool:
    current: BaseException | None = exc
    while current is not None:
        if isinstance(
            current,
            (
                RuntimeInputError,
                RuntimeConfigurationError,
                PluginRegistryError,
                ProviderConfigurationError,
                ProviderCapabilityError,
                ProviderUnavailableError,
                SkillError,
                ValidationError,
            ),
        ):
            return True
        current = current.__cause__ or current.__context__
    return False


def _safe_error_message(exc: BaseException) -> str:
    current: BaseException | None = exc
    while current is not None:
        public_message = getattr(current, "public_message", None)
        if isinstance(public_message, str) and public_message.strip():
            return public_message.strip()
        current = current.__cause__ or current.__context__
    message = str(exc).strip()
    return message if message else exc.__class__.__name__


__all__ = [
    "EXIT_DEGRADED",
    "EXIT_INPUT_ERROR",
    "EXIT_INTERRUPTED",
    "EXIT_OK",
    "EXIT_RUNTIME_ERROR",
    "LocalFileError",
    "build_parser",
    "main",
    "parse_options_json",
    "read_local_input",
    "write_artifact_atomic",
]

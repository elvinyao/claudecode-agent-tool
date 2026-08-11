"""Public API for the domain-neutral Agent Core framework."""

from importlib.metadata import PackageNotFoundError, version

from agent_core.contracts import (
    ActionMode,
    AgentCoreError,
    AgentRequest,
    ArtifactInput,
    ArtifactOutput,
    ProviderResult,
    RunStatus,
    ToolPolicy,
)
from agent_core.registry import PluginManifest, PluginRegistry
from agent_core.runtime import AgentRuntime, RuntimeArtifact, RuntimeResult
from agent_core.workflow import ActionNode, AgentNode, TransformNode, Workflow

try:
    __version__ = version("agent-core")
except PackageNotFoundError:  # pragma: no cover - source checkout without installation
    __version__ = "0.1.0"

__all__ = [
    "ActionMode",
    "ActionNode",
    "AgentCoreError",
    "AgentNode",
    "AgentRequest",
    "AgentRuntime",
    "ArtifactInput",
    "ArtifactOutput",
    "PluginManifest",
    "PluginRegistry",
    "ProviderResult",
    "RunStatus",
    "RuntimeArtifact",
    "RuntimeResult",
    "ToolPolicy",
    "TransformNode",
    "Workflow",
    "__version__",
]

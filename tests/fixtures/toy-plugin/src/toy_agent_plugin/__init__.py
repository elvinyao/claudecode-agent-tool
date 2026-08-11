"""A deliberately non-security domain plugin used by integration tests."""

from toy_agent_plugin.plugin import (
    ToyAgentAnswer,
    ToyArtifact,
    ToyInput,
    ToyOptions,
    ToyPlugin,
    ToyRuntime,
    create_plugin,
)

__all__ = [
    "ToyAgentAnswer",
    "ToyArtifact",
    "ToyInput",
    "ToyOptions",
    "ToyPlugin",
    "ToyRuntime",
    "create_plugin",
]

from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel, ConfigDict

from agent_core.contracts import AgentRequest, ProviderResult, ToolPolicy
from agent_core.providers import (
    BaseProvider,
    ProviderAuthenticationError,
    ProviderCapabilities,
    ProviderCapabilityError,
    ProviderConfigurationError,
    ProviderPermissionError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderTransportError,
)
from agent_core.providers.base import OutputT
from agent_core.providers.errors import classify_provider_exception


class DemoOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str


def request(*, policy: ToolPolicy | None = None) -> AgentRequest[DemoOutput]:
    return AgentRequest[DemoOutput](
        request_id="request-1",
        system_prompt="Return a typed answer.",
        prompt="Analyze this input.",
        response_model=DemoOutput,
        tool_policy=policy or ToolPolicy(),
    )


class SlowProvider(BaseProvider):
    name = "slow"
    capabilities = ProviderCapabilities()

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0
        self.started = asyncio.Event()
        self.cleaned_up = asyncio.Event()

    async def _execute(self, request: AgentRequest[OutputT]) -> ProviderResult[OutputT]:
        self.calls += 1
        self.started.set()
        try:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")
        finally:
            self.cleaned_up.set()


@pytest.mark.asyncio
async def test_timeout_cancels_once_and_waits_for_cleanup() -> None:
    provider = SlowProvider()

    with pytest.raises(ProviderTimeoutError) as exc_info:
        await provider.execute(request(), timeout_seconds=0.001)

    assert exc_info.value.retryable is True
    assert provider.calls == 1
    assert provider.cleaned_up.is_set()


@pytest.mark.asyncio
async def test_caller_cancellation_propagates_after_cleanup() -> None:
    provider = SlowProvider()
    task = asyncio.create_task(provider.execute(request()))
    await provider.started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert provider.calls == 1
    assert provider.cleaned_up.is_set()


@pytest.mark.asyncio
async def test_unsupported_tool_is_rejected_before_execution() -> None:
    provider = SlowProvider()
    item = request(policy=ToolPolicy(allowed_tools=("Bash",)))

    with pytest.raises(ProviderCapabilityError, match="Bash"):
        await provider.execute(item)

    assert provider.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [0.0, -1.0, float("inf"), float("nan")])
async def test_timeout_must_be_finite_and_positive(timeout: float) -> None:
    provider = SlowProvider()

    with pytest.raises(ProviderConfigurationError, match="timeout_seconds"):
        await provider.execute(request(), timeout_seconds=timeout)

    assert provider.calls == 0


def test_sdk_error_classifier_preserves_retry_metadata() -> None:
    class RateLimited(Exception):
        status_code = 429
        retry_after = 2

    error = classify_provider_exception(RateLimited("slow down"), provider="demo")

    assert isinstance(error, ProviderRateLimitError)
    assert error.retryable is True
    assert error.retry_after_seconds == 2
    assert error.provider == "demo"
    assert error.public_message == "Provider rate limit was exceeded"


def test_invalid_json_schema_is_a_sanitized_configuration_error() -> None:
    failure = RuntimeError("400 invalid_json_schema: missing secret_field from a private schema")

    error = classify_provider_exception(failure, provider="codex")

    assert isinstance(error, ProviderConfigurationError)
    assert error.code == "provider_configuration"
    assert error.provider == "codex"
    assert error.retryable is False
    assert error.public_message == "Provider configuration is invalid"
    assert "secret_field" not in error.public_message
    assert "secret_field" not in str(error)


@pytest.mark.parametrize(
    ("status_code", "expected_type", "retryable"),
    [
        (401, ProviderAuthenticationError, False),
        (403, ProviderPermissionError, False),
        (503, ProviderTransportError, True),
    ],
)
def test_sdk_status_codes_map_to_typed_errors(
    status_code: int,
    expected_type: type[Exception],
    retryable: bool,
) -> None:
    class SdkFailure(Exception):
        status_code: int

    failure = SdkFailure("sdk failed")
    failure.status_code = status_code
    error = classify_provider_exception(failure, provider="demo")

    assert isinstance(error, expected_type)
    assert error.retryable is retryable
    assert "sdk failed" not in error.public_message


@pytest.mark.asyncio
async def test_provider_does_not_retry_transport_failures() -> None:
    class FailingProvider(BaseProvider):
        name = "failing"
        capabilities = ProviderCapabilities()

        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def _execute(self, request: AgentRequest[OutputT]) -> ProviderResult[OutputT]:
            self.calls += 1
            raise ProviderTransportError("temporary", provider=self.name)

    provider = FailingProvider()
    with pytest.raises(ProviderTransportError):
        await provider.execute(request())
    assert provider.calls == 1

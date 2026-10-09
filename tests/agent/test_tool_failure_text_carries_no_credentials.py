"""The text of a failed tool call carries no credential, in the result the model reads (security ticket 01a11fbc-d6de).

The text goes back into the parent model's context as the tool result, is recorded, and (for an exposed tool) is returned over MCP. httpx prints the
request URL whole, a child agent's or graph's failure body is interpolated into the parent's result, and every one of those is built with ``str(exc)``.
Every error result is now redacted where it is made (URL credentials, Bearer and Basic tokens); a successful result is data and is returned as it was.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

import primer.workspace  # noqa: F401  (resolves ToolCallContext's forward reference)
from primer.agent.loop import _dispatch_tool_calls
from primer.agent.tool_manager import ToolExecutionManager
from primer.graph.invoke_graph import ChildGraphFailed
from primer.model.chat import Error, ToolCallPart, ToolCallResult, ToolResultPart, TurnStreamFailure
from primer.model.except_ import ProviderError
from primer.model.principal import PrincipalRef
from tests.agent.test_tool_manager import _FakeToolsetProvider, _tool

LEAKY = (
    "ConnectError: All connection attempts failed for url 'https://svc-user:hunter2pw@gateway.internal/v1/x?api_key=SKSECRET123456' "
    "(retry with Bearer sk-abcdefgh12345678 or Basic dXNlcjpwYXNzd29yZA==)"
)
SECRETS = ("hunter2pw", "SKSECRET123456", "sk-abcdefgh12345678", "dXNlcjpwYXNzd29yZA==")


def _assert_clean(text: str) -> None:
    for secret in SECRETS:
        assert secret not in text, f"{secret!r} leaked in {text!r}"
    assert "gateway.internal" in text, f"the rest of the message must survive: {text!r}"


def _manager(provider) -> ToolExecutionManager:
    return ToolExecutionManager(toolset_providers={"a": provider}, initiated_by=PrincipalRef.system())  # type: ignore[arg-type]


class _Raises(_FakeToolsetProvider):
    async def call(self, *, tool_name: str, arguments: dict[str, Any], principal=None, ctx=None):
        raise ProviderError(LEAKY)


class _Returns(_FakeToolsetProvider):
    def __init__(self, result: ToolCallResult) -> None:
        super().__init__(toolset_id="a", tools=[_tool("foo", toolset_id="a")])
        self._result = result

    async def call(self, *, tool_name: str, arguments: dict[str, Any], principal=None, ctx=None):
        return self._result


@pytest.mark.asyncio
async def test_a_tool_that_raises_is_answered_without_the_credentials():
    manager = _manager(_Raises(toolset_id="a", tools=[_tool("foo", toolset_id="a")]))

    result = await manager.execute(ToolCallPart(id="c", name="a__foo", arguments={}))

    assert result.error is True
    _assert_clean(result.output)


@pytest.mark.asyncio
async def test_a_tool_that_returns_an_error_result_is_answered_without_the_credentials():
    """Most tools do not raise: they return ``is_error=True`` with the exception's text in the output (http_request, web_fetch, download, ...)."""
    manager = _manager(_Returns(ToolCallResult(output=f"http-request failed: {LEAKY}", is_error=True)))

    result = await manager.execute(ToolCallPart(id="c", name="a__foo", arguments={}))

    assert result.error is True
    _assert_clean(result.output)


@pytest.mark.asyncio
async def test_a_successful_result_is_returned_as_it_was():
    manager = _manager(_Returns(ToolCallResult(output=LEAKY, is_error=False)))

    result = await manager.execute(ToolCallPart(id="c", name="a__foo", arguments={}))

    assert result.error is False and result.output == LEAKY


@pytest.mark.asyncio
async def test_an_error_result_without_a_credential_is_returned_as_it_was():
    plain = "http-request failed: ConnectError: All connection attempts failed for url 'https://example.com/v1/x?page=2'"
    manager = _manager(_Returns(ToolCallResult(output=plain, is_error=True)))

    result = await manager.execute(ToolCallPart(id="c", name="a__foo", arguments={}))

    assert result.output == plain


@pytest.mark.asyncio
async def test_the_loops_belt_and_braces_catch_does_not_hand_the_model_the_credentials():
    """``_dispatch_tool_calls`` catches a PrimerError the manager did not convert (an adapter bug) and answers the call with ``str(exc)``."""
    class _Manager:
        def is_notifying(self, name: str) -> bool:
            return False

        async def execute(self, call, *, principal=None):
            raise ProviderError(LEAKY)

    messages = await _dispatch_tool_calls(
        [ToolCallPart(id="c", name="a__foo", arguments={})], tool_manager=_Manager(), principal=None, actions_out=[],  # type: ignore[arg-type]
    )

    (part,) = messages[0].parts
    assert isinstance(part, ToolResultPart) and part.error is True
    _assert_clean(part.output)


@pytest.mark.asyncio
async def test_a_child_agent_failure_is_answered_without_the_credentials(monkeypatch):
    """``invoke_agent``: the child's stream error is interpolated into the parent's tool result."""
    from primer.api.registries import ProviderRegistry
    from primer.toolset.system import build_system_toolset
    from tests.toolset.test_system_invoke_agent import _SP

    async def _boom(**kwargs):
        raise TurnStreamFailure(Error(code="llm_stream_error", message=LEAKY, fatal=True), partial_messages=[], rounds_completed=0)

    monkeypatch.setattr("primer.toolset.system.run_subagent", _boom)
    sp = _SP()
    registry = ProviderRegistry(
        sp, llm_factory=lambda p: object(), embedder_factory=lambda p: object(),  # type: ignore[arg-type]
        cross_encoder_factory=lambda p: object(), toolset_factory=lambda t: object(),
    )
    provider = build_system_toolset(storage_provider=sp, provider_registry=registry)  # type: ignore[arg-type]

    res = await provider.call(tool_name="invoke_agent", arguments={"agent_id": "a", "prompt": "p"}, principal=None, ctx=None)

    assert res.is_error is True
    assert "LLM stream failed" in res.output
    _assert_clean(res.output)


def test_a_child_graph_failure_body_carries_no_credentials():
    """``invoke_graph``: every delivery site sends the agent ``ChildGraphFailed.result_json()``."""
    failed = ChildGraphFailed(code="tool_execution_failed", message=LEAKY, node_id="worker")

    body = json.loads(failed.result_json())

    assert body["error"] == "tool_execution_failed" and body["node_id"] == "worker"
    _assert_clean(body["message"])


def test_a_refusal_body_keeps_the_operators_reason_as_typed():
    """An approval rejection's reason is what a person typed, not an exception's text."""
    failed = ChildGraphFailed(code="tool_approval_rejected", message="no thanks, see https://wiki.internal/policy", node_id="n")

    assert json.loads(failed.result_json())["reason"] == "no thanks, see https://wiki.internal/policy"

"""Notifying-class routing and dispatch through ToolExecutionManager.

S3 spec section 3: a notifying call is answered by the runner itself with
a successful synthetic tool_result; the park machinery is never entered.
"""

from __future__ import annotations

from typing import Any

from primer.agent.tool_manager import ToolExecutionManager
from primer.model.chat import NOTIFYING_TOOL_RESULT, Tool, ToolCallPart, ToolCallResult
from primer.model.principal import PrincipalRef
from primer.model.yield_ import ToolContext, Yielded, YieldToWorker
from primer.toolset.internal import InternalToolsetProvider


_SYSTEM = PrincipalRef(type="system", id="test", display="test", source="local")

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": True,
}


def _tool(name: str, *, tool_class: str = "standard") -> Tool:
    return Tool(
        id=name,
        toolset_id="t1",
        description="d",
        args_schema=_SCHEMA,
        tool_class=tool_class,
        required_role="user",
    )


async def _plain_handler(arguments: dict[str, Any]) -> ToolCallResult:
    del arguments
    return ToolCallResult(output="ran", is_error=False)


async def _parking_handler(
    arguments: dict[str, Any], *, ctx: ToolContext | None = None
) -> ToolCallResult:
    del arguments
    assert ctx is not None
    raise YieldToWorker(
        Yielded(tool_name="notify_me", event_key="k", resume_metadata={}),
        tool_call_id=ctx.tool_call_id,
    )


class _FakeAgentSession:
    """Bare-minimum AgentSession stand-in (tests/agent/test_tool_manager.py:262).

    Pinned decision 12: chat_id is retired by S1 P7, and
    ToolExecutionManager has no session_id parameter, so the session shape
    is a workspace_session object. It is what builds the ToolContext the
    parking fixtures below assert on.
    """

    workspace_id = "ws-1"
    session_id = "sess-1"
    agent_id = "agent-1"
    workspace_tools: list = []


def _manager(registry: dict) -> ToolExecutionManager:
    return ToolExecutionManager(
        toolset_providers={"t1": InternalToolsetProvider("t1", registry)},
        workspace_session=_FakeAgentSession(),  # type: ignore[arg-type]
        initiated_by=_SYSTEM,
    )


async def test_is_notifying_keys_on_the_scoped_id() -> None:
    mgr = _manager(
        {
            "notify_me": (_tool("notify_me", tool_class="notifying"), _plain_handler),
            "do_it": (_tool("do_it"), _plain_handler),
        }
    )
    await mgr.list_tools()
    assert mgr.is_notifying("t1__notify_me") is True
    assert mgr.is_notifying("t1__do_it") is False
    assert mgr.is_notifying("t1__unknown") is False


async def test_a_tool_hidden_by_the_agent_allowlist_is_not_notifying() -> None:
    """The notifying index is a SUBSET of what the agent may call.

    The routing table is populated for every tool a bound provider
    exposes, allowlisted or not. Indexing beside it would let the runner's
    notifying branch answer a call the agent never registered with a
    synthetic success (and emit a delivery record for it) instead of the
    usual not-registered rejection, which would make the notifying class a
    hole in the agent tool surface. Index beside the visible catalogue.
    """
    import pytest

    from primer.model.except_ import UnsupportedContentError

    mgr = ToolExecutionManager(
        toolset_providers={
            "t1": InternalToolsetProvider(
                "t1",
                {
                    "notify_me": (
                        _tool("notify_me", tool_class="notifying"),
                        _plain_handler,
                    )
                },
            )
        },
        tools=["t1__something_else"],  # an allowlist that hides ours
        workspace_session=_FakeAgentSession(),  # type: ignore[arg-type]
        initiated_by=_SYSTEM,
    )
    assert await mgr.list_tools() == []
    assert mgr.is_notifying("t1__notify_me") is False
    with pytest.raises(UnsupportedContentError):
        await mgr.execute(
            ToolCallPart(id="tc-0", name="t1__notify_me", arguments={})
        )


async def test_notifying_dispatch_never_parks_and_answers_success() -> None:
    mgr = _manager(
        {"notify_me": (_tool("notify_me", tool_class="notifying"), _parking_handler)}
    )
    await mgr.list_tools()
    call = ToolCallPart(id="tc-1", name="t1__notify_me", arguments={"a": 1})
    rp = await mgr.deliver_notifying(call)
    assert rp.id == "tc-1"
    assert rp.error is False
    assert rp.output == NOTIFYING_TOOL_RESULT


async def test_standard_yielding_tool_still_parks() -> None:
    import pytest

    mgr = _manager({"wait": (_tool("wait"), _parking_handler)})
    await mgr.list_tools()
    call = ToolCallPart(id="tc-2", name="t1__wait", arguments={})
    with pytest.raises(YieldToWorker):
        await mgr.execute(call)


async def test_notifying_non_client_tool_obeys_the_same_rule() -> None:
    """Spec section 7: a NOTIFYING non-client tool follows the same rule.

    inform_user is server-side, not client-registered, so its delivery is
    its own channel sink. The class is one concept: the handler still runs
    (the sink still fires) and the caller still gets the synthetic success
    instead of the handler's ``{"delivered_to": N}``.
    """
    from primer.toolset.misc import MISC_TOOLSET_ID, build_misc_toolset

    mgr = ToolExecutionManager(
        toolset_providers={MISC_TOOLSET_ID: build_misc_toolset()},
        workspace_session=_FakeAgentSession(),  # type: ignore[arg-type]
        initiated_by=_SYSTEM,
    )
    delivered: list[str] = []

    async def _sink(message: str) -> int:
        delivered.append(message)
        return 1

    mgr.set_inform_sink(_sink)
    await mgr.list_tools()
    assert mgr.is_notifying("misc__inform_user") is True
    assert mgr.is_notifying("misc__get_datetime") is False
    rp = await mgr.deliver_notifying(
        ToolCallPart(
            id="tc-i", name="misc__inform_user", arguments={"message": "hi"}
        )
    )
    assert delivered == ["hi"], "the notifying handler must still run"
    assert rp.id == "tc-i"
    assert rp.error is False
    assert rp.output == NOTIFYING_TOOL_RESULT


async def test_run_agent_turn_continues_past_a_notifying_call() -> None:
    import asyncio
    from collections.abc import AsyncIterator

    from primer.agent.loop import run_agent_turn
    from primer.model.agent import Agent, AgentModel
    from primer.model.chat import (
        Done,
        Message,
        StreamEvent,
        TextDelta,
        TextPart,
        ToolCallEnd,
        ToolCallStart,
        ToolResultPart,
    )
    from primer.model.model_profile import ModelProfileConfig
    from primer.model_profile import ResolvedModel

    class _OneNotifyThenStopLLM:
        def __init__(self) -> None:
            self.calls = 0

        def stream(self, *, model, messages, **kwargs):
            self.calls += 1
            first = self.calls == 1

            async def _gen() -> AsyncIterator[StreamEvent]:
                if first:
                    yield ToolCallStart(id="tc-9", name="t1__notify_me", index=0)
                    yield ToolCallEnd(id="tc-9", arguments={"m": "hi"}, index=0)
                    yield Done(stop_reason="tool_use", raw_reason="tool_use")
                else:
                    yield TextDelta(text="done", index=0)
                    yield Done(stop_reason="stop", raw_reason="stop")

            return _gen()

    mgr = _manager(
        {"notify_me": (_tool("notify_me", tool_class="notifying"), _parking_handler)}
    )
    agent = Agent(id="ag", description="x", model=AgentModel(profile_id="p--m"))
    llm = _OneNotifyThenStopLLM()
    messages_out: list[Message] = []

    async def _drive() -> None:
        async for _ in run_agent_turn(
            agent=agent,
            llm=llm,
            llm_model=ResolvedModel(
                profile_id="p",
                provider_id="pr",
                model_name="m",
                context_length=4096,
                config=ModelProfileConfig(),
            ),
            tool_manager=mgr,
            prompt=[Message(role="user", parts=[TextPart(text="go")])],
            messages_out=messages_out,
        ):
            pass

    await asyncio.wait_for(_drive(), timeout=5.0)

    assert llm.calls == 2, "the turn continued instead of parking"
    results = [
        p
        for m in messages_out
        for p in m.parts
        if isinstance(p, ToolResultPart)
    ]
    assert len(results) == 1
    assert results[0].error is False
    assert results[0].output == NOTIFYING_TOOL_RESULT

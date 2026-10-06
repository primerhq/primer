"""Every record a delegated (subagent) run writes carries the id of the RUN, its parent run and its depth.

The recorder stamped only ``delegate_tool_call_id``, the delegating call's RAW provider id, and raw ids are not unique: providers
that synthesise them (Gemini, Ollama: ``call_{idx}``) restart the numbering every stream, so a child's own call and its
parent's are the same string, and a grandchild run is stamped with the child's call id, which can equal the child's own delegate
id. The timeline nested by raw id (the last entry wins) and the analysis script keyed a run by it, so nested runs with colliding
ids were indistinguishable. ``run_subagent`` now mints a run id (kept in the resume context across a park), reads its parent's
from the run it is made inside, and the recorder stamps ``delegate_run_id``, ``delegate_parent_run_id`` and ``delegate_depth``.

These drive ``run_subagent`` / ``resume_subagent`` through the real tool manager with the fakes of ``test_run_subagent_yield``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from primer.agent.invoke import invocation_depth_guard, resume_subagent, run_subagent
from primer.model.chat import (
    Done,
    StreamStart,
    TextDelta,
    Tool,
    ToolCallEnd,
    ToolCallResult,
    ToolCallStart,
    ToolResultPart,
)
from primer.session.delegation import DelegationRecorder, reset_delegation_sink, set_delegation_sink
from primer.session.timeline import build_turn_timeline
from primer.worker.frames import AgentResumeContext
from tests.agent.test_run_subagent_yield import _agent, _provider_row, _ProviderRegistry, _StorageProvider

T0 = "2026-10-06T12:00:00+00:00"


class _Writer:
    def __init__(self) -> None:
        self.records: list = []

    async def append(self, rec) -> int:
        self.records.append(rec)
        return len(self.records)


class _Bus:
    async def publish(self, key, payload) -> None:
        return None


class _ScriptedLLM:
    """One scripted stream per ``stream`` call, in order (the child, the grandchild, the child again)."""

    def __init__(self, scripts: list[list]) -> None:
        self._scripts = list(scripts)

    def stream(self, *, model, messages, **kwargs):  # noqa: ANN001
        script = self._scripts.pop(0)

        async def _gen() -> AsyncIterator:
            for ev in script:
                yield ev

        return _gen()


def _text(answer: str) -> list:
    return [StreamStart(model="m1"), TextDelta(index=0, text=answer), Done(stop_reason="stop", raw_reason="stop")]


def _tool_call(call_id: str) -> list:
    return [
        StreamStart(model="m1"),
        ToolCallStart(id=call_id, name="t1__delegate", index=0),
        ToolCallEnd(id=call_id, arguments={}, index=0),
        Done(stop_reason="tool_use", raw_reason="tool_use"),
    ]


class _DelegatingToolset:
    """Tool ``t1__delegate``: runs a nested subagent the way ``system__invoke_agent`` does, under the SAME raw call id."""

    def __init__(self) -> None:
        self.storage: Any = None
        self.registry: Any = None

    async def list_tools(self, *, principal: str | None = None) -> AsyncIterator[Tool]:
        yield Tool(id="delegate", description="delegates", toolset_id="t1",
                   args_schema={"type": "object", "properties": {}, "additionalProperties": True})

    def is_yielding(self, tool_name: str) -> bool:
        return False

    def required_role(self, tool_name: str) -> str:
        return "admin"

    async def call(self, *, tool_name, arguments, principal=None, ctx=None) -> ToolCallResult:  # noqa: ANN001
        with invocation_depth_guard():
            text = await run_subagent(
                agent_id="agent-sub", prompt="inner", storage_provider=self.storage, provider_registry=self.registry,
                principal=principal, session_id="sess-1", workspace_id="ws-1", invoke_tool_call_id="call_0", turn_no=1,
            )
        return ToolCallResult(output=text, is_error=False)


def _world(scripts: list[list]):
    toolset = _DelegatingToolset()
    storage = _StorageProvider(agent=_agent(tools=["t1__delegate"]), provider_row=_provider_row())
    registry = _ProviderRegistry(llm=_ScriptedLLM(scripts), toolset=toolset)
    toolset.storage, toolset.registry = storage, registry
    return storage, registry


def _payloads(writer: _Writer) -> list[dict]:
    return [r.payload for r in writer.records]


def _lines(records: list[dict]) -> list[str]:
    import json

    return [json.dumps(r) for r in records]


async def test_nested_runs_that_reuse_one_raw_call_id_are_told_apart_by_run_id_parent_and_depth():
    storage, registry = _world([_tool_call("call_0"), _text("inner"), _text("outer")])
    writer = _Writer()
    token = set_delegation_sink(DelegationRecorder(writer=writer, event_bus=_Bus(), session_id="sess-1"))
    try:
        with invocation_depth_guard():  # what the invoke_agent handler wraps a run in
            await run_subagent(
                agent_id="agent-sub", prompt="outer", storage_provider=storage, provider_registry=registry,
                principal="user-1", session_id="sess-1", workspace_id="ws-1", invoke_tool_call_id="call_0", turn_no=1,
            )
    finally:
        reset_delegation_sink(token)

    payloads = _payloads(writer)
    assert payloads and all(p["delegated"] is True and p["delegate_tool_call_id"] == "call_0" for p in payloads), (
        "the raw call id alone cannot tell these runs apart: it is the same string at both levels"
    )
    by_depth: dict[int, list[dict]] = {}
    for p in payloads:
        by_depth.setdefault(p["delegate_depth"], []).append(p)
    assert sorted(by_depth) == [1, 2]
    (child_run,) = {p["delegate_run_id"] for p in by_depth[1]}
    (grandchild_run,) = {p["delegate_run_id"] for p in by_depth[2]}
    assert child_run != grandchild_run and len(child_run) == 32
    assert {p.get("delegate_parent_run_id") for p in by_depth[1]} == {None}, "the child was delegated to by the parent TURN"
    assert {p["delegate_parent_run_id"] for p in by_depth[2]} == {child_run}, "the grandchild was delegated to by the child's run"


async def test_the_timeline_nests_a_grandchild_under_the_childs_call_and_the_child_under_the_parents_even_with_one_raw_id():
    """End to end: the records the run writes, folded by the timeline. The child's own call reuses the id ``call_0``; by raw id alone
    the child's records after it nested under ITS call instead of the parent's."""
    storage, registry = _world([_tool_call("call_0"), _text("inner"), _text("outer")])
    writer = _Writer()
    token = set_delegation_sink(DelegationRecorder(writer=writer, event_bus=_Bus(), session_id="sess-1"))
    try:
        with invocation_depth_guard():
            await run_subagent(
                agent_id="agent-sub", prompt="outer", storage_provider=storage, provider_registry=registry,
                principal="user-1", session_id="sess-1", workspace_id="ws-1", invoke_tool_call_id="call_0", turn_no=1,
            )
    finally:
        reset_delegation_sink(token)

    records = [{"seq": 1, "kind": "tool_call", "created_at": T0, "node_id": None,
                "payload": {"id": "call_0", "name": "system__invoke_agent", "arguments": {}}}]
    for n, rec in enumerate(writer.records, start=2):
        records.append({"seq": n, "kind": rec.kind.value, "created_at": T0, "node_id": None, "payload": rec.payload})
    tl = build_turn_timeline(message_lines=_lines(records), turn_log_lines=[], turn_no=0)

    (parents_call,) = tl["children"]
    assert parents_call["kind"] == "tool_call" and parents_call["name"] == "system__invoke_agent"
    childs_calls = [c for c in parents_call["children"] if c["kind"] == "tool_call"]
    assert len(childs_calls) == 1, "the child's own call to the grandchild nests under the PARENT's call"
    grandchild_records = childs_calls[0]["children"]
    assert grandchild_records, "the grandchild's records nest under the CHILD's call"
    assert all(rec["seq"] > childs_calls[0]["seq"] for rec in grandchild_records)


async def test_a_resumed_run_keeps_its_run_id_parent_and_depth_and_is_stamped_with_the_delegating_call():
    """The continuation of a parked run is the same run. It used to read ``context.tool_call_id``, which an
    ``AgentResumeContext`` does not have, so every record of a resumed run said ``delegate_tool_call_id: None``."""
    storage = _StorageProvider(agent=_agent(tools=[]), provider_row=_provider_row())
    registry = _ProviderRegistry(llm=_ScriptedLLM([_text("done after the resume")]), toolset=_DelegatingToolset())
    context = AgentResumeContext(
        session_id="sess-1", workspace_id="ws-1", chat_id=None, principal="user-1", tools=[], turn_no=1,
        delegate_run_id="run-before-the-park", delegate_parent_run_id="run-of-the-parent",
    )
    writer = _Writer()
    token = set_delegation_sink(DelegationRecorder(writer=writer, event_bus=_Bus(), session_id="sess-1"))
    try:
        text = await resume_subagent(
            agent_id="agent-sub", context=context, llm_messages=[], child_result=ToolResultPart(id="call-x", output="ok"),
            depth=2, storage_provider=storage, provider_registry=registry, invoke_tool_call_id="call_0",
        )
    finally:
        reset_delegation_sink(token)

    assert text == "done after the resume"
    payloads = _payloads(writer)
    assert payloads
    for p in payloads:
        assert p["delegate_tool_call_id"] == "call_0"
        assert p["delegate_run_id"] == "run-before-the-park" and p["delegate_parent_run_id"] == "run-of-the-parent"
        assert p["delegate_depth"] == 2


async def test_a_frame_parked_before_run_ids_existed_gets_a_fresh_one_on_resume():
    storage = _StorageProvider(agent=_agent(tools=[]), provider_row=_provider_row())
    registry = _ProviderRegistry(llm=_ScriptedLLM([_text("ok")]), toolset=_DelegatingToolset())
    legacy = AgentResumeContext.from_jsonable({
        "session_id": "sess-1", "workspace_id": "ws-1", "chat_id": None, "principal": "user-1", "tools": [],
    })
    assert legacy.delegate_run_id is None and legacy.delegate_parent_run_id is None
    writer = _Writer()
    token = set_delegation_sink(DelegationRecorder(writer=writer, event_bus=_Bus(), session_id="sess-1"))
    try:
        await resume_subagent(
            agent_id="agent-sub", context=legacy, llm_messages=[], child_result=ToolResultPart(id="call-x", output="ok"),
            depth=1, storage_provider=storage, provider_registry=registry, invoke_tool_call_id="call_0",
        )
    finally:
        reset_delegation_sink(token)
    run_ids = {p["delegate_run_id"] for p in _payloads(writer)}
    assert len(run_ids) == 1 and len(next(iter(run_ids))) == 32


def test_the_run_ids_survive_the_park_blob_and_an_old_blob_still_loads():
    context = AgentResumeContext(
        session_id="s", workspace_id="w", chat_id=None, principal="p", tools=["t"], turn_no=3,
        delegate_run_id="run-1", delegate_parent_run_id="run-0",
    )
    again = AgentResumeContext.from_jsonable(context.to_jsonable())
    assert again.delegate_run_id == "run-1" and again.delegate_parent_run_id == "run-0"
    old = AgentResumeContext.from_jsonable({"session_id": "s", "workspace_id": "w", "chat_id": None, "principal": "p", "tools": []})
    assert old.delegate_run_id is None


async def test_a_parked_run_writes_the_run_id_into_the_frame_context():
    """The id minted at the start of ``run_subagent`` is what the frame carries, so a resume keeps it."""
    from tests.agent.test_run_subagent_yield import _GatedToolsetProvider, _tool_call_script, _FakeLLM
    from primer.agent.approval import ApprovalResolver
    from primer.model.tool_approval import RequiredApprovalConfig, ToolApprovalPolicy
    from primer.model.yield_ import YieldToWorker

    class _Resolver(ApprovalResolver):
        def __init__(self) -> None:
            self._ttl = 60.0
            self._cache = {}

        async def find(self, *, toolset_id, tool_name):  # noqa: ANN001
            return ToolApprovalPolicy(id="p", toolset_id="t1", tool_name="do_it", approval=RequiredApprovalConfig())

    storage = _StorageProvider(agent=_agent(tools=["t1__do_it"]), provider_row=_provider_row())
    registry = _ProviderRegistry(llm=_FakeLLM(events=_tool_call_script("t1__do_it", "call-1")), toolset=_GatedToolsetProvider())
    writer = _Writer()
    token = set_delegation_sink(DelegationRecorder(writer=writer, event_bus=_Bus(), session_id="sess-1"))
    try:
        with pytest.raises(YieldToWorker) as parked:
            with invocation_depth_guard():
                await run_subagent(
                    agent_id="agent-sub", prompt="x", storage_provider=storage, provider_registry=registry, principal="user-1",
                    approval_resolver=_Resolver(), session_id="sess-1", workspace_id="ws-1", invoke_tool_call_id="call_0",
                )
    finally:
        reset_delegation_sink(token)
    (frame,) = parked.value.frames
    stamped = {p["delegate_run_id"] for p in _payloads(writer)}
    assert stamped == {frame.context.delegate_run_id} and frame.context.delegate_run_id is not None

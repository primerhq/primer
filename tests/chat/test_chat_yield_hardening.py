"""Backend correctness hardening for the chat conversational-yield path.

Covers four final-review issues:

* I1 - a yielding tool that is NOT the last (or only) tool call in an
  assistant batch must not leave the other tool_use rows unpaired. Every
  non-pending tool_call in the batch gets a tool_result so the persisted
  history stays valid for the provider continuation.
* I3 - the approval-reply parse must fail closed on a co-occurring
  negative token ("no yes" -> rejected).
* I4 - cancel-while-awaiting abandons the pending call (synthetic
  cancelled tool_result, pending cleared) and processes the new message
  as a fresh turn rather than swallowing it as the answer.
* N1 - an out-of-scope yielding tool fails closed and the turn completes
  exactly once (no infinite re-serve), pending not set.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone

import pytest

from primer.chat.executor import ChatTurnRunner
from primer.model.agent import Agent, AgentModel
from primer.model.chat import (
    Done,
    StreamEvent,
    ToolCallEnd,
    ToolCallPart,
    ToolCallStart,
    ToolResultPart,
)
from primer.model.chats import Chat, ChatMessage
from primer.model.provider import LLMModel
from primer.model.yield_ import Yielded, YieldToWorker


class _FakeLLM:
    def __init__(self, stream_factory):
        self._stream_factory = stream_factory

    async def list_models(self):
        return ["m"]

    def stream(self, *, model, messages, **kwargs):
        return self._stream_factory()

    async def aclose(self):
        return None


def _runner(chat_store, msg_store, tool_manager, *, llm=None) -> ChatTurnRunner:
    agent = Agent(
        id="ag", description="x",
        model=AgentModel(provider_id="p", model_name="m"),
    )
    return ChatTurnRunner(
        agent=agent,
        llm=llm if llm is not None else _FakeLLM(lambda: iter(())),
        llm_model=LLMModel(name="m", context_length=4096),
        tool_manager=tool_manager,
        chat_storage=chat_store,
        message_storage=msg_store,
    )


def _assert_paired(history) -> None:
    """Every assistant ToolCallPart must be followed by a matching
    tool-role ToolResultPart with the same id."""
    flat = []
    for m in history:
        for p in m.parts:
            flat.append((m.role, p))
    result_ids = {
        getattr(p, "id", None)
        for role, p in flat
        if role == "tool" and isinstance(p, ToolResultPart)
    }
    call_ids = {
        getattr(p, "id", None)
        for role, p in flat
        if role == "assistant" and isinstance(p, ToolCallPart)
    }
    assert call_ids <= result_ids, (
        f"unpaired tool_use rows: {call_ids - result_ids}"
    )


# ---------------------------------------------------------------------------
# I1 - poison history on multi-tool-call turns
# ---------------------------------------------------------------------------


class _MixedToolManager:
    """`echo` runs normally; `ask_user`/`_approval` yield."""

    async def list_tools(self, *, principal=None):
        return [{"name": "echo", "description": "d", "parameters": {}}]

    async def execute(self, call, *, principal=None, bypass_approval=False):
        if call.name in ("ask_user", "_approval"):
            raise YieldToWorker(
                Yielded(
                    tool_name=call.name,
                    event_key=f"ask_user:{call.id}",
                    resume_metadata={"prompt": "Which?"},
                ),
                tool_call_id=call.id,
            )
        return ToolResultPart(id=call.id, output=f"echoed:{call.id}", error=False)


async def _drive_batch(chat_store, msg_store, batch):
    """Stream an assistant turn with the given [(id, name)] tool calls,
    let the runner dispatch, expect a YieldToWorker to escape."""
    def _stream() -> AsyncIterator[StreamEvent]:
        async def _gen():
            for cid, name in batch:
                yield ToolCallStart(id=cid, name=name, index=0)
                yield ToolCallEnd(id=cid, arguments={}, index=0)
            yield Done(stop_reason="tool_use", raw_reason="tool_use")
        return _gen()

    runner = _runner(
        chat_store, msg_store, _MixedToolManager(),
        llm=_FakeLLM(_stream),
    )
    chat = await chat_store.get("c1")
    caught: YieldToWorker | None = None
    try:
        async for _ in runner.run_turn(chat, "go"):
            pass
    except YieldToWorker as exc:
        caught = exc
    assert caught is not None, "expected a YieldToWorker to escape"
    # Mirror the dispatch layer: the escaped yield becomes the single
    # pending_tool_call via soft_yield.
    await runner.soft_yield(chat, caught)
    return runner


@pytest.mark.asyncio
async def test_i1_normal_then_yield(fake_storage_provider):
    chat_store = fake_storage_provider.get_storage(Chat)
    msg_store = fake_storage_provider.get_storage(ChatMessage)
    await chat_store.create(
        Chat(id="c1", agent_id="ag", created_at=datetime.now(timezone.utc)),
    )
    runner = await _drive_batch(
        chat_store, msg_store, [("n1", "echo"), ("y1", "ask_user")],
    )
    rows = await runner._read_messages_full("c1")
    # echo executed and produced a tool_result.
    assert any(
        r.kind == "tool_result" and (r.payload or {}).get("id") == "n1"
        for r in rows
    ), "normal tool produced no result"
    # ask_user is the pending call -> no tool_result yet.
    assert not any(
        r.kind == "tool_result" and (r.payload or {}).get("id") == "y1"
        for r in rows
    )
    chat = await chat_store.get("c1")
    assert chat.pending_tool_call["tool_call_id"] == "y1"


@pytest.mark.asyncio
async def test_i1_yield_then_normal_skips_secondary(fake_storage_provider):
    chat_store = fake_storage_provider.get_storage(Chat)
    msg_store = fake_storage_provider.get_storage(ChatMessage)
    await chat_store.create(
        Chat(id="c1", agent_id="ag", created_at=datetime.now(timezone.utc)),
    )
    runner = await _drive_batch(
        chat_store, msg_store, [("y1", "ask_user"), ("n1", "echo")],
    )
    rows = await runner._read_messages_full("c1")
    # The secondary normal tool gets a synthetic (skipped) error result.
    skipped = [
        r for r in rows
        if r.kind == "tool_result" and (r.payload or {}).get("id") == "n1"
    ]
    assert skipped, "secondary tool got no synthetic result"
    assert skipped[0].payload.get("error") is True
    assert "skipped" in str(skipped[0].payload).lower()
    chat = await chat_store.get("c1")
    assert chat.pending_tool_call["tool_call_id"] == "y1"


@pytest.mark.asyncio
async def test_i1_two_yields_second_gets_error(fake_storage_provider):
    chat_store = fake_storage_provider.get_storage(Chat)
    msg_store = fake_storage_provider.get_storage(ChatMessage)
    await chat_store.create(
        Chat(id="c1", agent_id="ag", created_at=datetime.now(timezone.utc)),
    )
    runner = await _drive_batch(
        chat_store, msg_store, [("a", "ask_user"), ("b", "ask_user")],
    )
    rows = await runner._read_messages_full("c1")
    # Only the FIRST yield becomes pending; the second gets an error result.
    second = [
        r for r in rows
        if r.kind == "tool_result" and (r.payload or {}).get("id") == "b"
    ]
    assert second, "second yield got no result"
    assert second[0].payload.get("error") is True
    chat = await chat_store.get("c1")
    assert chat.pending_tool_call["tool_call_id"] == "a"
    # History after pairing the pending call is fully paired.
    runner2 = _runner(chat_store, msg_store, _MixedToolManager())
    await runner2._append(chat, kind="tool_result", payload={
        "id": "a", "name": "ask_user", "result": "x", "error": False,
    })
    history = await runner2._load_history("c1")
    _assert_paired(history)


# ---------------------------------------------------------------------------
# I3 - affirmative heuristic
# ---------------------------------------------------------------------------


class _ApprovingToolManager:
    def __init__(self):
        self.calls = []

    async def execute(self, call, *, bypass_approval=False):
        self.calls.append((call, bypass_approval))
        return ToolResultPart(id=call.id, output="deployed", error=False)


async def _resume_decision(fake_storage_provider, text):
    chat_store = fake_storage_provider.get_storage(Chat)
    msg_store = fake_storage_provider.get_storage(ChatMessage)
    now = datetime.now(timezone.utc)
    chat = Chat(id="c1", agent_id="ag", created_at=now, turn_status="running",
                pending_tool_call={"tool_call_id": "tc1", "mode": "approval",
                                   "original_call": {"id": "tc1", "name": "deploy",
                                                     "arguments": {}}})
    await chat_store.create(chat)
    reply = ChatMessage(
        id=ChatMessage.make_id("c1", 1), chat_id="c1", seq=1,
        kind="user_message", payload={"content": text}, created_at=now,
    )
    await msg_store.create(reply)
    chat.last_seq = 1
    await chat_store.update(chat)
    tm = _ApprovingToolManager()
    runner = _runner(chat_store, msg_store, tm)
    await runner.resume_pending(chat, chat.pending_tool_call, reply)
    rows = await runner._read_messages_full("c1")
    tr = next(r for r in rows
              if r.kind == "tool_result" and (r.payload or {}).get("id") == "tc1")
    approved = not tr.payload["error"]
    return approved, tm.calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text,expected",
    [
        ("yes", True),
        ("approve", True),
        ("no", False),
        ("no yes", False),
        ("nope", False),
        ("", False),
        ("maybe", False),
    ],
)
async def test_i3_decision_parse(fake_storage_provider, text, expected):
    approved, calls = await _resume_decision(fake_storage_provider, text)
    assert approved is expected, f"{text!r} -> approved={approved}"
    assert (len(calls) == 1) is expected


# ---------------------------------------------------------------------------
# N1 - out-of-scope yielding tool completes the turn cleanly
# ---------------------------------------------------------------------------


class _OutOfScopeToolManager:
    """`mcp_task` yields; it is out of scope on the chat surface."""

    def __init__(self):
        self.list_calls = 0

    async def list_tools(self, *, principal=None):
        self.list_calls += 1
        return [{"name": "mcp_task", "description": "d", "parameters": {}}]

    async def execute(self, call, *, principal=None, bypass_approval=False):
        raise YieldToWorker(
            Yielded(tool_name="mcp_task", event_key=f"mcp_task:s:{call.id}",
                    resume_metadata={}),
            tool_call_id=call.id,
        )


@pytest.mark.asyncio
async def test_n1_out_of_scope_completes_once(fake_storage_provider):
    chat_store = fake_storage_provider.get_storage(Chat)
    msg_store = fake_storage_provider.get_storage(ChatMessage)
    await chat_store.create(
        Chat(id="c1", agent_id="ag", created_at=datetime.now(timezone.utc)),
    )

    state = {"round": 0}

    def _stream() -> AsyncIterator[StreamEvent]:
        async def _gen():
            state["round"] += 1
            if state["round"] == 1:
                yield ToolCallStart(id="m1", name="mcp_task", index=0)
                yield ToolCallEnd(id="m1", arguments={}, index=0)
                yield Done(stop_reason="tool_use", raw_reason="tool_use")
            else:
                # After seeing the error tool_result the model concludes.
                yield Done(stop_reason="stop", raw_reason="stop")
        return _gen()

    runner = _runner(
        chat_store, msg_store, _OutOfScopeToolManager(),
        llm=_FakeLLM(_stream),
    )
    chat = await chat_store.get("c1")
    rows = []
    async for r in runner.run_turn(chat, "go"):
        rows.append(r)

    kinds = [r.kind for r in rows]
    # Error tool_result for the unsupported tool.
    errs = [r for r in rows
            if r.kind == "tool_result" and (r.payload or {}).get("id") == "m1"]
    assert errs and errs[0].payload.get("error") is True
    assert "not supported" in str(errs[0].payload)
    # Exactly one terminal done -> the turn completed once, not re-served.
    assert kinds.count("done") == 1, kinds
    fresh = await chat_store.get("c1")
    assert fresh.pending_tool_call is None


# ---------------------------------------------------------------------------
# I4 - cancel-while-awaiting abandons the pending call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_i4_cancel_abandons_pending(
    fake_storage_provider, fake_provider_registry,
):
    from pydantic import SecretStr

    from primer.bus.in_memory import InMemoryEventBus
    from primer.chat.dispatch import ChatDispatchDeps, run_one_chat_turn
    from primer.chat.tick_router import ChatTickRouter
    from primer.model.chat import Done, TextDelta
    from primer.model.provider import (
        AnthropicConfig, Limits, LLMProvider, LLMProviderType,
    )
    from primer.model.storage import (
        FieldRef, Op, OffsetPage, OrderBy, Predicate, Value,
    )

    class _CompletingLLM:
        def __init__(self):
            self.calls = []

        async def list_models(self):
            return ["m"]

        def stream(self, *, model, messages, **kwargs):
            self.calls.append(list(messages))
            return self._impl()

        async def _impl(self):
            yield TextDelta(text="fresh", index=0)
            yield Done(stop_reason="stop", raw_reason="stop")

        async def aclose(self):
            return None

    await fake_storage_provider.get_storage(LLMProvider).create(LLMProvider(
        id="p", provider=LLMProviderType.ANTHROPIC,
        models=[LLMModel(name="m", context_length=8192)],
        config=AnthropicConfig(api_key=SecretStr("test")),
        limits=Limits(max_concurrency=1),
    ))
    await fake_storage_provider.get_storage(Agent).create(Agent(
        id="ag", description="x",
        model=AgentModel(provider_id="p", model_name="m"),
    ))
    llm = _CompletingLLM()

    async def _get_llm(_pid):
        return llm
    fake_provider_registry.get_llm = _get_llm  # type: ignore[assignment]

    chats = fake_storage_provider.get_storage(Chat)
    msgs = fake_storage_provider.get_storage(ChatMessage)
    now = datetime.now(timezone.utc)
    # Awaiting input: orig user_message (seq1), assistant prompt (seq2),
    # unresolved tool_call (seq3), then a NEW user_message (seq4) that
    # arrives WHILE a cancel is pending.
    chat = Chat(
        id="c1", agent_id="ag", created_at=now, turn_status="running",
        pending_tool_call={"tool_call_id": "tc1", "mode": "ask_user",
                           "response_schema": None},
        cancel_requested_at=now,
        last_seq=4,
    )
    await chats.create(chat)
    seed = [
        (1, "user_message", {"content": "which env?"}),
        (2, "assistant_token", {"delta": "Which env?"}),
        (3, "tool_call", {"id": "tc1", "name": "ask_user", "args": {}}),
        (4, "user_message", {"content": "actually, what's the weather?"}),
    ]
    for seq, kind, payload in seed:
        await msgs.create(ChatMessage(
            id=ChatMessage.make_id("c1", seq),
            chat_id="c1", seq=seq, kind=kind, payload=payload,
            created_at=now,
        ))

    bus = InMemoryEventBus()
    await bus.initialize()
    deps = ChatDispatchDeps(
        storage_provider=fake_storage_provider,
        provider_registry=fake_provider_registry,
        event_bus=bus,
        chat_tick_router=ChatTickRouter(),
        fake_llm=llm,
    )
    try:
        disposition = await run_one_chat_turn(deps, chat_id="c1", worker_id="w1")
    finally:
        await bus.aclose()

    pred = Predicate(left=FieldRef(name="chat_id"), op=Op.EQ,
                     right=Value(value="c1"))
    page = await msgs.find(pred, OffsetPage(offset=0, length=200),
                           order_by=[OrderBy(field="seq", direction="asc")])
    rows = page.items
    # The pending call got a synthetic cancelled tool_result.
    cancelled = [
        r for r in rows
        if r.kind == "tool_result" and (r.payload or {}).get("id") == "tc1"
    ]
    assert cancelled, "pending call was not abandoned with a tool_result"
    assert cancelled[0].payload.get("error") is True
    assert "cancel" in str(cancelled[0].payload).lower()

    final = await chats.get("c1")
    assert final.pending_tool_call is None

    # The NEW message (seq4) was processed as a FRESH turn (not swallowed
    # as the pending answer): seq4 must NOT be _history_excluded.
    new_um = await msgs.get(ChatMessage.make_id("c1", 4))
    assert not (new_um.payload or {}).get("_history_excluded"), (
        "new message was swallowed as the pending answer"
    )

    runner = ChatTurnRunner(
        agent=await fake_storage_provider.get_storage(Agent).get("ag"),
        llm=llm,
        llm_model=LLMModel(name="m", context_length=8192),
        tool_manager=object(),
        chat_storage=chats,
        message_storage=msgs,
    )
    history = await runner._load_history("c1")
    _assert_paired(history)
    assert disposition == "idle"

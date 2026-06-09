"""Resume path: when a chat agent yields on ask_user/approval (soft_yield
records ``chat.pending_tool_call`` and leaves the yielding tool_call row
unresolved), the human's next user_message is consumed as that pending
call's tool_result and the agent loop continues from the augmented
history WITHOUT injecting a new user turn."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from primer.chat.executor import ChatTurnRunner
from primer.model.agent import Agent, AgentModel
from primer.model.chat import ToolResultPart
from primer.model.chats import Chat, ChatMessage
from primer.model.provider import LLMModel


class _FakeLLM:
    async def list_models(self):
        return ["m"]

    def stream(self, *, model, messages, **kwargs):
        raise AssertionError("resume_pending is invoked directly; no stream")

    async def aclose(self):
        return None


class _ApprovingToolManager:
    """execute(call, bypass_approval=True) returns a deployed result."""

    def __init__(self):
        self.calls: list = []

    async def execute(self, call, *, bypass_approval=False):
        self.calls.append((call, bypass_approval))
        return ToolResultPart(id=call.id, output="deployed", error=False)


def _runner(chat_store, msg_store, tool_manager=None) -> ChatTurnRunner:
    agent = Agent(
        id="ag", description="x",
        model=AgentModel(provider_id="p", model_name="m"),
    )
    return ChatTurnRunner(
        agent=agent,
        llm=_FakeLLM(),
        llm_model=LLMModel(name="m", context_length=4096),
        tool_manager=tool_manager if tool_manager is not None else object(),
        chat_storage=chat_store,
        message_storage=msg_store,
    )


async def _seed(chat_store, msg_store, pending, tc_payload):
    now = datetime.now(timezone.utc)
    chat = Chat(
        id="c1", agent_id="ag", created_at=now,
        turn_status="running", pending_tool_call=pending,
    )
    await chat_store.create(chat)
    # seq 1: assistant_token, seq 2: tool_call (unresolved), seq 3: reply
    rows = [
        ("assistant_token", {"delta": "Which env?"}),
        ("tool_call", tc_payload),
        ("user_message", {"content": "staging"}),
    ]
    for seq, (kind, payload) in enumerate(rows, start=1):
        await msg_store.create(ChatMessage(
            id=ChatMessage.make_id("c1", seq),
            chat_id="c1", seq=seq, kind=kind, payload=payload,
            created_at=now,
        ))
    chat.last_seq = len(rows)
    await chat_store.update(chat)
    reply = await msg_store.get(ChatMessage.make_id("c1", 3))
    return chat, reply


@pytest.mark.asyncio
async def test_resume_ask_user(fake_storage_provider):
    chat_store = fake_storage_provider.get_storage(Chat)
    msg_store = fake_storage_provider.get_storage(ChatMessage)
    chat, reply = await _seed(
        chat_store, msg_store,
        pending={"tool_call_id": "tc1", "mode": "ask_user",
                 "response_schema": None},
        tc_payload={"id": "tc1", "name": "ask_user", "args": {}},
    )
    runner = _runner(chat_store, msg_store)

    await runner.resume_pending(chat, chat.pending_tool_call, reply)

    rows = await runner._read_messages_full("c1")
    results = [r for r in rows
               if r.kind == "tool_result" and (r.payload or {}).get("id") == "tc1"]
    assert results, "no tool_result for tc1"
    assert results[0].payload["result"] == "staging"
    assert results[0].payload["error"] is False

    # The reply user_message is excluded from future history.
    reply_after = await msg_store.get(ChatMessage.make_id("c1", 3))
    assert reply_after.payload["_history_excluded"] is True

    fresh = await chat_store.get("c1")
    assert fresh.pending_tool_call is None

    # _load_history yields a valid paired tool_use/tool_result sequence and
    # NO extra user turn for the reply.
    history = await runner._load_history("c1", current_user_msg_seq=999)
    # Find the assistant message carrying the ToolCallPart.
    flat = []
    for m in history:
        for p in m.parts:
            flat.append((m.role, p))
    tc_idx = next(i for i, (role, p) in enumerate(flat)
                  if getattr(p, "id", None) == "tc1" and role == "assistant")
    nxt_role, nxt_part = flat[tc_idx + 1]
    assert nxt_role == "tool"
    assert isinstance(nxt_part, ToolResultPart)
    assert nxt_part.id == "tc1"
    assert nxt_part.output == "staging"
    assert not any(role == "user" and getattr(p, "text", None) == "staging"
                   for role, p in flat)


# ---------------------------------------------------------------------------
# Dispatch-level: drive the full resume path through run_one_chat_turn and
# assert the consumed reply is NOT re-served as a fresh turn.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispatch_resume_continues_and_does_not_double_process(
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
            yield TextDelta(text="done", index=0)
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
    # Parked state: orig user_message (seq1), assistant prompt (seq2),
    # unresolved tool_call (seq3), human reply (seq4). No terminal row.
    chat = Chat(
        id="c1", agent_id="ag", created_at=now, turn_status="running",
        pending_tool_call={"tool_call_id": "tc1", "mode": "ask_user",
                           "response_schema": None},
        last_seq=4,
    )
    await chats.create(chat)
    seed = [
        (1, "user_message", {"content": "which env?"}),
        (2, "assistant_token", {"delta": "Which env?"}),
        (3, "tool_call", {"id": "tc1", "name": "ask_user", "args": {}}),
        (4, "user_message", {"content": "staging"}),
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
    kinds = [r.kind for r in page.items]
    # The continuation produced exactly one tool_result (resume) + one done.
    assert kinds.count("tool_result") == 1
    tr = next(r for r in page.items if r.kind == "tool_result")
    assert tr.payload["result"] == "staging"
    # Exactly ONE done: the reply was NOT re-processed as a fresh turn.
    assert kinds.count("done") == 1, kinds
    # The LLM was called exactly once (continuation only).
    assert len(llm.calls) == 1
    final = await chats.get("c1")
    assert final.pending_tool_call is None
    # Clean drain -> idle disposition; the fenced adapter applies it.
    assert disposition == "idle"
    reply = await msgs.get(ChatMessage.make_id("c1", 4))
    assert reply.payload["_history_excluded"] is True


@pytest.mark.asyncio
async def test_resume_approval_yes(fake_storage_provider):
    chat_store = fake_storage_provider.get_storage(Chat)
    msg_store = fake_storage_provider.get_storage(ChatMessage)
    tm = _ApprovingToolManager()
    chat, reply = await _seed(
        chat_store, msg_store,
        pending={"tool_call_id": "tc1", "mode": "approval",
                 "original_call": {"id": "tc1", "name": "deploy",
                                   "arguments": {}}},
        tc_payload={"id": "tc1", "name": "deploy", "args": {}},
    )
    # Reply text is "yes".
    reply.payload = {"content": "yes"}
    await msg_store.update(reply)
    runner = _runner(chat_store, msg_store, tool_manager=tm)

    await runner.resume_pending(chat, chat.pending_tool_call, reply)

    rows = await runner._read_messages_full("c1")
    results = [r for r in rows
               if r.kind == "tool_result" and (r.payload or {}).get("id") == "tc1"]
    assert results[0].payload["result"] == "deployed"
    assert results[0].payload["error"] is False
    assert len(tm.calls) == 1
    assert tm.calls[0][1] is True  # bypass_approval

    fresh = await chat_store.get("c1")
    assert fresh.pending_tool_call is None


@pytest.mark.asyncio
async def test_resume_approval_no(fake_storage_provider):
    chat_store = fake_storage_provider.get_storage(Chat)
    msg_store = fake_storage_provider.get_storage(ChatMessage)
    tm = _ApprovingToolManager()
    chat, reply = await _seed(
        chat_store, msg_store,
        pending={"tool_call_id": "tc1", "mode": "approval",
                 "original_call": {"id": "tc1", "name": "deploy",
                                   "arguments": {}}},
        tc_payload={"id": "tc1", "name": "deploy", "args": {}},
    )
    reply.payload = {"content": "no"}
    await msg_store.update(reply)
    runner = _runner(chat_store, msg_store, tool_manager=tm)

    await runner.resume_pending(chat, chat.pending_tool_call, reply)

    rows = await runner._read_messages_full("c1")
    results = [r for r in rows
               if r.kind == "tool_result" and (r.payload or {}).get("id") == "tc1"]
    assert results[0].payload["error"] is True
    assert len(tm.calls) == 0  # no execute on decline

    fresh = await chat_store.get("c1")
    assert fresh.pending_tool_call is None

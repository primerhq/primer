"""Soft-yield: a chat-surface YieldToWorker (ask_user / approval gate)
must NOT park. Instead the runner surfaces the tool's prompt as a
visible assistant message, records the pending tool call on the chat
row, and lets the turn end. The human's next message resolves it.

Out-of-scope yielding tools (e.g. mcp_task) fail closed with an error
tool_result so the agent isn't stuck."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from primer.chat.executor import ChatTurnRunner
from primer.model.agent import Agent, AgentModel
from primer.model.chats import Chat, ChatMessage
from primer.model.provider import LLMModel
from primer.model.yield_ import Yielded, YieldToWorker


class _FakeLLM:
    async def list_models(self):
        return ["m"]

    def stream(self, *, model, messages, **kwargs):
        raise AssertionError("soft_yield is invoked directly; no stream")

    async def aclose(self):
        return None


def _runner(chat_store, msg_store) -> ChatTurnRunner:
    agent = Agent(
        id="ag", description="x",
        model=AgentModel(provider_id="p", model_name="m"),
    )
    return ChatTurnRunner(
        agent=agent,
        llm=_FakeLLM(),
        llm_model=LLMModel(name="m", context_length=4096),
        tool_manager=object(),
        chat_storage=chat_store,
        message_storage=msg_store,
    )


@pytest.mark.asyncio
async def test_soft_yield_ask_user(fake_storage_provider):
    chat_store = fake_storage_provider.get_storage(Chat)
    msg_store = fake_storage_provider.get_storage(ChatMessage)
    chat = Chat(id="c1", agent_id="ag", created_at=datetime.now(timezone.utc))
    await chat_store.create(chat)
    runner = _runner(chat_store, msg_store)

    exc = YieldToWorker(
        Yielded(
            tool_name="ask_user",
            event_key="ask_user:c1:tc1",
            timeout=None,
            resume_metadata={
                "prompt": "Which env?",
                "response_schema": None,
                "tool_call_id": "tc1",
            },
        ),
        tool_call_id="tc1",
    )
    await runner.soft_yield(chat, exc)

    rows = await runner._read_messages_full("c1")
    assert any(
        "Which env?" in str(r.payload) for r in rows
    ), "no visible prompt message persisted"
    assert not any(
        r.kind == "tool_result" and (r.payload or {}).get("id") == "tc1"
        for r in rows
    ), "unexpected tool_result for tc1"

    fresh = await chat_store.get("c1")
    assert fresh.pending_tool_call == {
        "tool_call_id": "tc1", "mode": "ask_user", "response_schema": None,
    }


@pytest.mark.asyncio
async def test_soft_yield_approval(fake_storage_provider):
    chat_store = fake_storage_provider.get_storage(Chat)
    msg_store = fake_storage_provider.get_storage(ChatMessage)
    chat = Chat(id="c1", agent_id="ag", created_at=datetime.now(timezone.utc))
    await chat_store.create(chat)
    runner = _runner(chat_store, msg_store)

    exc = YieldToWorker(
        Yielded(
            tool_name="_approval",
            event_key="tool_approval:c1:tc1",
            resume_metadata={
                "gate_reason": "sensitive",
                "original_call": {"id": "tc1", "name": "deploy", "arguments": {}},
            },
        ),
        tool_call_id="tc1",
    )
    await runner.soft_yield(chat, exc)

    rows = await runner._read_messages_full("c1")
    visible = [
        r for r in rows
        if "deploy" in str(r.payload) and "Approve" in str(r.payload)
    ]
    assert visible, "no visible approval message mentioning deploy + Approve"

    fresh = await chat_store.get("c1")
    assert fresh.pending_tool_call["mode"] == "approval"
    assert fresh.pending_tool_call["original_call"]["name"] == "deploy"


@pytest.mark.asyncio
async def test_soft_yield_out_of_scope(fake_storage_provider):
    chat_store = fake_storage_provider.get_storage(Chat)
    msg_store = fake_storage_provider.get_storage(ChatMessage)
    chat = Chat(id="c1", agent_id="ag", created_at=datetime.now(timezone.utc))
    await chat_store.create(chat)
    runner = _runner(chat_store, msg_store)

    exc = YieldToWorker(
        Yielded(
            tool_name="mcp_task",
            event_key="mcp_task:s:tc1",
            resume_metadata={},
        ),
        tool_call_id="tc1",
    )
    await runner.soft_yield(chat, exc)

    rows = await runner._read_messages_full("c1")
    errs = [
        r for r in rows
        if r.kind == "tool_result" and (r.payload or {}).get("id") == "tc1"
    ]
    assert errs, "no error tool_result for out-of-scope yield"
    assert "not supported" in str(errs[0].payload)
    assert errs[0].payload.get("error") is True

    fresh = await chat_store.get("c1")
    assert fresh.pending_tool_call is None

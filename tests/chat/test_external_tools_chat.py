"""Chat-surface external tool flow: soft-yield, resume, abandon."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from primer.chat.executor import ChatTurnRunner
from primer.model.agent import Agent, AgentModel
from primer.model.chats import Chat, ChatMessage
from primer.model.external_tool import ExternalToolCall
from primer.model.model_profile import ModelProfileConfig
from primer.model.yield_ import Yielded, YieldToWorker
from primer.model_profile import ResolvedModel


class _FakeLLM:
    async def list_models(self):
        return ["m"]

    def stream(self, *, model, messages, **kwargs):
        raise AssertionError("these tests never stream")

    async def aclose(self):
        return None


def _runner(chat_store, msg_store, external_calls=None) -> ChatTurnRunner:
    agent = Agent(
        id="ag", description="x",
        model=AgentModel(profile_id="p--m"),
    )
    return ChatTurnRunner(
        agent=agent,
        llm=_FakeLLM(),
        llm_model=ResolvedModel(
            profile_id="test-profile",
            provider_id="test-provider",
            model_name="m",
            context_length=4096,
            config=ModelProfileConfig(),
        ),
        tool_manager=object(),
        chat_storage=chat_store,
        message_storage=msg_store,
        external_call_storage=external_calls,
    )


def _external_yield(tcid="tc-1"):
    return YieldToWorker(
        Yielded(
            tool_name="_external",
            event_key=f"external_tool:c1:{tcid}",
            resume_metadata={
                "original_call": {
                    "id": tcid,
                    "name": "lookup_customer",
                    "arguments": {"id": "c1"},
                },
                "external_call_row_id": "etool-c1",
            },
        ),
        tool_call_id=tcid,
    )


@pytest.mark.asyncio
async def test_soft_yield_external_records_pending_and_row_kind(
    fake_storage_provider,
):
    chat_store = fake_storage_provider.get_storage(Chat)
    msg_store = fake_storage_provider.get_storage(ChatMessage)
    chat = Chat(id="c1", agent_id="ag", created_at=datetime.now(timezone.utc))
    await chat_store.create(chat)
    runner = _runner(chat_store, msg_store)

    await runner.soft_yield(chat, _external_yield())

    fresh = await chat_store.get("c1")
    assert fresh.pending_tool_call["mode"] == "external"
    assert fresh.pending_tool_call["name"] == "lookup_customer"
    assert fresh.pending_tool_call["external_call_row_id"] == "etool-c1"
    rows = await runner._read_messages_full("c1")
    kinds = [r.kind for r in rows]
    assert "external_tool_call" in kinds
    frame = next(r for r in rows if r.kind == "external_tool_call")
    assert frame.payload["name"] == "lookup_customer"
    assert frame.payload["id"] == "tc-1"
    # Machine consumers, not prose: no assistant_token prompt is emitted.
    assert "assistant_token" not in kinds


@pytest.mark.asyncio
async def test_resume_external_pending_appends_paired_result(
    fake_storage_provider,
):
    chat_store = fake_storage_provider.get_storage(Chat)
    msg_store = fake_storage_provider.get_storage(ChatMessage)
    chat = Chat(id="c1", agent_id="ag", created_at=datetime.now(timezone.utc))
    chat.pending_tool_call = {
        "tool_call_id": "tc-1",
        "mode": "external",
        "name": "lookup_customer",
        "arguments": {"id": "c1"},
        "external_call_row_id": "etool-c1",
        "external_result": {"result": {"customer": "Ada"}, "is_error": False},
    }
    await chat_store.create(chat)
    runner = _runner(chat_store, msg_store)

    await runner.resume_external_pending(chat, chat.pending_tool_call)

    rows = await runner._read_messages_full("c1")
    tr = next(r for r in rows if r.kind == "tool_result")
    assert tr.payload["id"] == "tc-1"
    assert tr.payload["name"] == "lookup_customer"
    assert "Ada" in tr.payload["result"]
    assert tr.payload["error"] is False
    fresh = await chat_store.get("c1")
    assert fresh.pending_tool_call is None


@pytest.mark.asyncio
async def test_abandon_external_pending_flips_row(fake_storage_provider):
    chat_store = fake_storage_provider.get_storage(Chat)
    msg_store = fake_storage_provider.get_storage(ChatMessage)
    calls = fake_storage_provider.get_storage(ExternalToolCall)
    await calls.create(
        ExternalToolCall(
            id="etool-c1",
            chat_id="c1",
            tool_call_id="tc-1",
            tool_name="lookup_customer",
            arguments={},
            created_at=datetime.now(timezone.utc),
        )
    )
    chat = Chat(id="c1", agent_id="ag", created_at=datetime.now(timezone.utc))
    pending = {
        "tool_call_id": "tc-1",
        "mode": "external",
        "name": "lookup_customer",
        "arguments": {},
        "external_call_row_id": "etool-c1",
    }
    chat.pending_tool_call = pending
    await chat_store.create(chat)
    runner = _runner(chat_store, msg_store, external_calls=calls)

    await runner.abandon_pending(chat, pending)

    row = await calls.get("etool-c1")
    assert row.status == "cancelled"
    rows = await runner._read_messages_full("c1")
    tr = next(r for r in rows if r.kind == "tool_result")
    assert tr.payload["id"] == "tc-1"
    assert tr.payload["name"] == "lookup_customer"
    assert tr.payload["error"] is True
    fresh = await chat_store.get("c1")
    assert fresh.pending_tool_call is None

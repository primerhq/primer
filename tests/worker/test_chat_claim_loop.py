"""WorkerPool spins a _claim_chat_loop that picks up claimable
chats and dispatches them to run_one_chat_turn."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from pydantic import SecretStr

from matrix.model.agent import Agent, AgentModel
from matrix.model.chats import Chat, ChatMessage
from matrix.model.provider import (
    AnthropicConfig, Limits, LLMModel, LLMProvider, LLMProviderType,
)


@pytest_asyncio.fixture
async def seeded_agent(app):
    sp = app.state.storage_provider
    await sp.get_storage(LLMProvider).create(
        LLMProvider(
            id="llm-p", provider=LLMProviderType.ANTHROPIC,
            models=[LLMModel(name="m", context_length=8192)],
            config=AnthropicConfig(api_key=SecretStr("test")),
            limits=Limits(max_concurrency=1),
        ),
    )
    await sp.get_storage(Agent).create(Agent(
        id="ag-chat", description="x",
        model=AgentModel(provider_id="llm-p", model_name="m"),
    ))


@pytest.mark.asyncio
async def test_worker_pool_claims_and_processes_chat(
    app, fake_llm, seeded_agent,
):
    """app fixture must start a worker pool with the chat claim loop.
    We seed a claimable chat and assert the worker drains it."""
    chats = app.state.storage_provider.get_storage(Chat)
    msgs = app.state.storage_provider.get_storage(ChatMessage)
    now = datetime.now(timezone.utc)
    chat = Chat(
        id="c1", agent_id="ag-chat", created_at=now,
        turn_status="claimable",
    )
    await chats.create(chat)
    await msgs.create(ChatMessage(
        id=ChatMessage.make_id("c1", 1),
        chat_id="c1", seq=1, kind="user_message",
        payload={"content": "hi"}, created_at=now,
    ))
    chat.last_seq = 1
    await chats.update(chat)
    # Upsert the chat into the engine claim queue (new engine-based claim path).
    from matrix.int.claim import ClaimKind
    await app.state.worker_pool._engine.upsert(ClaimKind.CHAT, "c1", priority=100)

    # Wait up to ~2.5s for the chat to drain (turn_status→idle).
    for _ in range(50):
        row = await chats.get("c1")
        if row.turn_status == "idle":
            break
        await asyncio.sleep(0.05)
    assert row.turn_status == "idle", (
        f"worker did not drain the chat: turn_status={row.turn_status!r}, "
        f"claimed_by={row.claimed_by!r}"
    )
    assert row.claimed_by is None

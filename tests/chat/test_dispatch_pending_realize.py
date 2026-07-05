"""Deferred-queue: a follow-up sent while the agent is still "Thinking…"
must NOT collide with the in-flight turn's seq allocation, and must be
realized as a real user_message ordered AFTER the turn's terminal row.

This is the critical regression the older
``test_dispatch_queue_while_compacting`` test does NOT cover: that test
seeds the second user_message AFTER the first ``TextDelta`` (so the
assistant token already claimed ``seq=2`` and there is no collision
window). Here the follow-up is enqueued BEFORE the first assistant
token — the exact window where the old code allocated a real ``seq=2``
row for the follow-up that then collided with the executor's first
``assistant_token`` (also ``seq=2``), raising ``ConflictError`` and
aborting the turn.

With the deferred queue the follow-up is held on
``Chat.pending_user_messages`` (no seq) until the turn's terminal row
lands, then realized via ``append_user_message`` — so it is impossible
for it to share a seq with an in-flight row, and it sorts AFTER the
response.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

import pytest
from pydantic import SecretStr

from primer.bus.in_memory import InMemoryEventBus
from primer.chat.dispatch import ChatDispatchDeps, run_one_chat_turn
from primer.chat.tick_router import ChatTickRouter
from primer.model.agent import Agent, AgentModel
from primer.model.chat import Done, StreamEvent, TextDelta
from primer.model.chats import Chat, ChatMessage
from primer.model.provider import (
    AnthropicConfig, Limits, LLMModel, LLMProvider, LLMProviderType,
)
from primer.model.storage import (
    FieldRef, Op, OffsetPage, OrderBy, Predicate, Value,
)


class _ThinkingSeederLLM:
    """Fake LLM that — during the FIRST turn, BEFORE the first assistant
    token — enqueues a follow-up onto ``chat.pending_user_messages``,
    simulating the deferred-queue WS recv loop receiving a second send
    while the agent is still "Thinking…". Later turns stream a plain
    ``TextDelta`` + ``Done`` so the drain loop terminates cleanly."""

    def __init__(self, *, on_first_thinking) -> None:
        self._on_first_thinking = on_first_thinking
        self._calls_seen = 0
        self.calls: list[dict[str, Any]] = []

    async def list_models(self):
        return ["m"]

    def stream(self, *, model, messages, **kwargs):
        self._calls_seen += 1
        self.calls.append({"model": model, "messages": list(messages)})
        return self._stream_impl(first=self._calls_seen == 1)

    async def _stream_impl(self, *, first: bool) -> AsyncIterator[StreamEvent]:
        # Fire the side effect BEFORE any token lands — this is the
        # "Thinking…" collision window the old code tripped on.
        if first:
            await self._on_first_thinking()
        yield TextDelta(text="hi", index=0)
        yield Done(stop_reason="stop", raw_reason="stop")

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_followup_during_thinking_is_deferred_and_realized_after_turn(
    fake_storage_provider, fake_provider_registry,
) -> None:
    await fake_storage_provider.get_storage(LLMProvider).create(
        LLMProvider(
            id="p", provider=LLMProviderType.ANTHROPIC,
            models=[LLMModel(name="m", context_length=8192)],
            config=AnthropicConfig(api_key=SecretStr("test")),
            limits=Limits(max_concurrency=1),
        ),
    )
    await fake_storage_provider.get_storage(Agent).create(Agent(
        id="ag", description="x",
        model=AgentModel(provider_id="p", model_name="m"),
    ))

    chats = fake_storage_provider.get_storage(Chat)
    msgs = fake_storage_provider.get_storage(ChatMessage)

    now = datetime.now(timezone.utc)
    chat = Chat(
        id="c1", agent_id="ag", created_at=now,
        turn_status="running",  # the worker pool has already claimed it
        last_seq=1,
    )
    await chats.create(chat)
    await msgs.create(ChatMessage(
        id=ChatMessage.make_id("c1", 1),
        chat_id="c1", seq=1, kind="user_message",
        payload={"content": "first"}, created_at=now,
    ))

    async def _enqueue_pending_followup() -> None:
        # Mirror exactly what the WS recv loop does when a frame arrives
        # while a turn is active: hold it on pending_user_messages (NO seq).
        cur = await chats.get("c1")
        cur.pending_user_messages = [
            {
                "parts": [{"type": "text", "text": "second"}],
                "attribution": None,
                "client_msg_id": "cid-2",
                "queued_at": datetime.now(timezone.utc).isoformat(),
            }
        ]
        await chats.update(cur)

    fake_llm = _ThinkingSeederLLM(on_first_thinking=_enqueue_pending_followup)

    async def _get_llm(_pid):
        return fake_llm
    fake_provider_registry.get_llm = _get_llm  # type: ignore[assignment]

    bus = InMemoryEventBus()
    await bus.initialize()
    deps = ChatDispatchDeps(
        storage_provider=fake_storage_provider,
        provider_registry=fake_provider_registry,
        event_bus=bus,
        chat_tick_router=ChatTickRouter(),
        fake_llm=fake_llm,
    )

    try:
        disposition = await run_one_chat_turn(deps, chat_id="c1", worker_id="w1")
    finally:
        await bus.aclose()

    # (a) The turn did NOT abort on a seq collision: two clean LLM turns ran.
    assert fake_llm._calls_seen == 2, (
        f"expected 2 stream calls (turn 1 + the realized follow-up); "
        f"got {fake_llm._calls_seen}"
    )

    page = await msgs.find(
        Predicate(left=FieldRef(name="chat_id"), op=Op.EQ,
                  right=Value(value="c1")),
        OffsetPage(offset=0, length=200),
        order_by=[OrderBy(field="seq", direction="asc")],
    )
    rows = page.items
    kinds = [r.kind for r in rows]

    # (b) The first turn completed with its assistant + done rows, and the
    #     follow-up turn also completed — two of each terminal.
    assert kinds.count("user_message") == 2, kinds
    assert kinds.count("done") == 2, kinds

    # No colliding / duplicate seqs anywhere.
    seqs = [r.seq for r in rows]
    assert len(seqs) == len(set(seqs)), f"duplicate seq detected: {seqs}"

    # (c) The realized follow-up sorts AFTER the first turn's done row.
    user_rows = [r for r in rows if r.kind == "user_message"]
    first_done_seq = next(r.seq for r in rows if r.kind == "done")
    realized = user_rows[1]
    assert realized.payload.get("content") == "second"
    assert realized.seq > first_done_seq, (
        f"realized follow-up seq {realized.seq} must be > first done "
        f"seq {first_done_seq}"
    )
    # client_msg_id is carried onto the realized row for client reconcile.
    assert realized.payload.get("client_msg_id") == "cid-2"

    # (d) It was processed as the next turn (its own done row follows it).
    realized_idx = rows.index(realized)
    assert any(
        r.kind == "done" and r.seq > realized.seq
        for r in rows[realized_idx:]
    ), kinds

    # Pending list drained; clean disposition.
    final = await chats.get("c1")
    assert final.pending_user_messages == []
    assert disposition == "idle"

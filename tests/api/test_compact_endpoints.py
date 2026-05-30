"""REST POST /v1/chats/{id}/compact contract.

Covers the four buckets:

* 200 — normal happy path; persists a ``compaction_marker`` row and
  returns the seq + summary + tokens.
* 404 — chat not found.
* 409 — chat already has a worker turn in flight
  (``turn_status='running'``).
* 503 — agent's LLM provider can't be resolved (e.g. ``models=[]`` on
  the provider row), surfaced via :class:`ConfigError`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from primer.agent.compaction_mixin import CompactionResult
from primer.api.app import create_test_app
from primer.model.agent import Agent, AgentModel
from primer.model.chats import Chat, ChatMessage
from primer.model.provider import (
    AnthropicConfig,
    Limits,
    LLMModel,
    LLMProvider,
    LLMProviderType,
)


class _CompactTestFakeLLM:
    """Fake LLM used by the compact endpoint tests.

    ``force_compact`` ultimately calls into the strategy's ``_tier2``
    which invokes ``llm.stream`` for the summarisation pass. We
    short-circuit that round-trip by monkeypatching
    ``primer.agent.compaction_mixin.apply_compaction`` in the test —
    this fake is here only so the runtime's
    ``provider_registry.get_llm`` call resolves to a non-None object.
    """

    async def list_models(self):
        return ["m"]

    async def aclose(self):
        return None


@pytest.fixture
def fake_llm() -> _CompactTestFakeLLM:
    return _CompactTestFakeLLM()


@pytest.fixture
def app(
    fake_storage_provider,
    fake_provider_registry,
    fake_llm,
) -> FastAPI:
    async def _get_llm(_pid: str) -> _CompactTestFakeLLM:
        return fake_llm

    fake_provider_registry.get_llm = _get_llm  # type: ignore[assignment]
    # Don't start the chat worker — these tests pre-seed chat rows
    # with the exact ``turn_status`` they want to exercise.
    return create_test_app(
        storage_provider=fake_storage_provider,
        provider_registry=fake_provider_registry,
        start_chat_worker=False,
    )


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t",
    ) as c:
        try:
            await c.post(
                "/v1/auth/register",
                json={"username": "testuser", "password": "testpassword"},
            )
        except Exception:
            pass
        yield c


async def _seed_provider_and_agent(
    app: FastAPI,
    *,
    provider_id: str = "llm-p",
    model_name: str = "m",
    context_length: int = 8000,
    agent_id: str = "ag-chat",
    provider_models: list[str] | None = None,
) -> None:
    """Helper that seeds the LLMProvider + Agent rows the compact
    endpoint resolves.

    ``provider_models`` lets the caller seed a provider with a model
    list that does NOT include ``model_name`` — the shape the 503
    test exercises (agent points at a model the provider no longer
    enables). Defaults to ``[model_name]``.
    """
    names = provider_models if provider_models is not None else [model_name]
    await app.state.storage_provider.get_storage(LLMProvider).create(
        LLMProvider(
            id=provider_id,
            provider=LLMProviderType.ANTHROPIC,
            models=[
                LLMModel(name=n, context_length=context_length) for n in names
            ],
            config=AnthropicConfig(api_key=SecretStr("test-only")),
            limits=Limits(max_concurrency=1),
        ),
    )
    await app.state.storage_provider.get_storage(Agent).create(
        Agent(
            id=agent_id,
            description="chat agent",
            model=AgentModel(provider_id=provider_id, model_name=model_name),
            tools=[],
            system_prompt=[],
        ),
    )


async def _seed_chat(
    app: FastAPI,
    *,
    chat_id: str = "c1",
    agent_id: str = "ag-chat",
    turn_status: str = "idle",
    last_seq: int = 2,
) -> Chat:
    chat = Chat(
        id=chat_id,
        agent_id=agent_id,
        created_at=datetime.now(timezone.utc),
        last_seq=last_seq,
        turn_status=turn_status,  # type: ignore[arg-type]
    )
    await app.state.storage_provider.get_storage(Chat).create(chat)
    msgs = app.state.storage_provider.get_storage(ChatMessage)
    now = datetime.now(timezone.utc)
    if last_seq >= 1:
        await msgs.create(
            ChatMessage(
                id=ChatMessage.make_id(chat_id, 1),
                chat_id=chat_id, seq=1, kind="user_message",
                payload={"content": "hello"},
                created_at=now,
            ),
        )
    if last_seq >= 2:
        await msgs.create(
            ChatMessage(
                id=ChatMessage.make_id(chat_id, 2),
                chat_id=chat_id, seq=2, kind="assistant_token",
                payload={"delta": "hi back"},
                created_at=now,
            ),
        )
    return chat


def _patch_apply_compaction(monkeypatch, *, summary: str = "SUMMARY") -> None:
    """Replace ``apply_compaction`` so ``force_compact`` returns a
    deterministic :class:`CompactionResult` without touching an LLM."""
    from primer.agent import compaction_mixin

    async def _fake_apply(**_kw) -> CompactionResult:
        return CompactionResult(
            new_history=[],
            summary_text=summary,
            tokens_before=4321,
            tokens_after=120,
            model="m",
            created_at=datetime.now(timezone.utc),
        )

    monkeypatch.setattr(compaction_mixin, "apply_compaction", _fake_apply)


@pytest.mark.asyncio
class TestCompactEndpoint:
    async def test_200_returns_marker_seq_and_summary(
        self, client, app, monkeypatch,
    ) -> None:
        await _seed_provider_and_agent(app)
        await _seed_chat(app, chat_id="c1", turn_status="idle", last_seq=2)
        _patch_apply_compaction(monkeypatch, summary="ROLLED UP")

        resp = await client.post("/v1/chats/c1/compact")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["compaction_marker_seq"] == 3
        assert body["summary"] == "ROLLED UP"
        assert body["tokens_before"] == 4321
        assert body["tokens_after"] == 120

        # The marker row was persisted with the expected payload shape.
        msgs = app.state.storage_provider.get_storage(ChatMessage)
        marker = await msgs.get(ChatMessage.make_id("c1", 3))
        assert marker is not None
        assert marker.kind == "compaction_marker"
        assert marker.payload["summary"] == "ROLLED UP"
        assert marker.payload["model"] == "m"
        assert marker.payload["tokens_before"] == 4321
        assert marker.payload["tokens_after"] == 120
        assert marker.payload["trigger"] == "operator_forced"
        assert marker.payload["replaced_to_seq"] == 2

        # The chat row's last_seq advanced.
        chat = await app.state.storage_provider.get_storage(Chat).get("c1")
        assert chat is not None
        assert chat.last_seq == 3

    async def test_404_when_chat_not_found(self, client) -> None:
        resp = await client.post("/v1/chats/does-not-exist/compact")
        assert resp.status_code == 404

    async def test_409_when_turn_in_flight(
        self, client, app, monkeypatch,
    ) -> None:
        await _seed_provider_and_agent(app)
        await _seed_chat(app, chat_id="c1", turn_status="running", last_seq=2)
        _patch_apply_compaction(monkeypatch)

        resp = await client.post("/v1/chats/c1/compact")
        assert resp.status_code == 409

        # And no marker was persisted.
        msgs = app.state.storage_provider.get_storage(ChatMessage)
        assert await msgs.get(ChatMessage.make_id("c1", 3)) is None

    async def test_503_when_agent_provider_unresolved(
        self, client, app, monkeypatch,
    ) -> None:
        """Seed a provider whose ``models`` list doesn't carry the
        model the agent points at — the resolver path raises
        :class:`ConfigError` which surfaces as 503."""
        await _seed_provider_and_agent(app, provider_models=["other-model"])
        await _seed_chat(app, chat_id="c1", turn_status="idle", last_seq=2)
        _patch_apply_compaction(monkeypatch)

        resp = await client.post("/v1/chats/c1/compact")
        assert resp.status_code == 503

        # And no marker was persisted.
        msgs = app.state.storage_provider.get_storage(ChatMessage)
        assert await msgs.get(ChatMessage.make_id("c1", 3)) is None

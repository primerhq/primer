"""End-to-end auto + manual compaction journey for the chat surface.

This pins the REST + storage round-trip for the chat compaction surface:
the on-demand ``POST /v1/chats/{id}/compact`` path persists a
``compaction_marker`` row that subsequently shows up in
``GET /v1/chats/{id}/messages``, and a second compaction against the
same chat is also accepted (idempotency-ish — no orphan / 5xx).

WS-side replay of the compaction envelope (the cursor=0 reconnection
shape) is already pinned by ``tests/api/test_chat_ws_envelopes.py``
which directly drives the envelope translator added in T10.1. Mixing
a long-running WS connect into this REST journey would risk the
deadlock that bit the original T10.1 attempt, so the combined
coverage is left split: this file owns REST + storage, T10.1's unit
test owns the WS envelope shape.

The fixtures here mirror the in-memory app pattern from
``tests/api/test_compact_endpoints.py`` so the test can run without
the live ``primer api`` server the rest of ``tests/e2e/`` requires
(see ``tests/e2e/conftest.py`` for the default-skip mechanism — this
file is collected only when ``PRIMER_RUN_E2E=1`` is set).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone

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
from tests.conftest import _FakeStorageProvider  # noqa: F401
from tests.api.conftest import fake_provider_registry  # noqa: F401


# ---------------------------------------------------------------------------
# Fake LLM with a count_tokens that scales with message length so the
# 0.90 auto-compaction threshold actually trips when the chat is seeded
# with high-token-count history.
# ---------------------------------------------------------------------------


class _CompactJourneyFakeLLM:
    """Fake LLM exposing the minimum surface compaction needs.

    The compact endpoint uses ``force_compact`` which is monkeypatched
    out (it's expensive), but the surrounding plumbing still needs a
    non-None ``llm`` object resolved via ``provider_registry.get_llm``.
    ``count_tokens`` returns the sum of message character lengths — a
    cheap proxy that lets the threshold test trip deterministically.
    """

    async def list_models(self) -> list[str]:
        return ["m"]

    async def count_tokens(
        self, *, model: str, messages, tools=None,
    ) -> int:
        total = 0
        for msg in messages:
            for part in getattr(msg, "parts", []) or []:
                text = getattr(part, "text", None)
                if text:
                    total += len(text)
        return total

    async def aclose(self) -> None:
        return None


@pytest.fixture
def fake_llm() -> _CompactJourneyFakeLLM:
    return _CompactJourneyFakeLLM()


@pytest.fixture
def app(
    fake_storage_provider,
    fake_provider_registry,
    fake_llm,
) -> FastAPI:
    async def _get_llm(_pid: str) -> _CompactJourneyFakeLLM:
        return fake_llm

    fake_provider_registry.get_llm = _get_llm  # type: ignore[assignment]
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
        except Exception:  # noqa: BLE001
            pass
        yield c


# ---------------------------------------------------------------------------
# Seed helpers.
# ---------------------------------------------------------------------------


PROVIDER_ID = "llm-journey"
AGENT_ID = "ag-journey"
MODEL_NAME = "m"
CONTEXT_LENGTH = 10_000


async def _seed_provider_and_agent(app: FastAPI) -> None:
    await app.state.storage_provider.get_storage(LLMProvider).create(
        LLMProvider(
            id=PROVIDER_ID,
            provider=LLMProviderType.ANTHROPIC,
            models=[
                LLMModel(name=MODEL_NAME, context_length=CONTEXT_LENGTH),
            ],
            config=AnthropicConfig(api_key=SecretStr("test-only")),
            limits=Limits(max_concurrency=1),
        ),
    )
    await app.state.storage_provider.get_storage(Agent).create(
        Agent(
            id=AGENT_ID,
            description="compaction journey",
            model=AgentModel(provider_id=PROVIDER_ID, model_name=MODEL_NAME),
            tools=[],
            system_prompt=[],
        ),
    )


async def _seed_high_token_chat(
    app: FastAPI,
    *,
    chat_id: str = "c1",
    pairs: int = 50,
    text_len_per_msg: int = 200,
) -> int:
    """Insert ``pairs`` user_message / assistant_token row pairs whose
    combined fake-token count lands above the 0.90 threshold for
    ``CONTEXT_LENGTH``. Returns the last seq written.
    """
    chats = app.state.storage_provider.get_storage(Chat)
    msgs = app.state.storage_provider.get_storage(ChatMessage)
    now = datetime.now(timezone.utc)
    chat = Chat(
        id=chat_id,
        agent_id=AGENT_ID,
        created_at=now,
        last_seq=0,
        turn_status="idle",  # type: ignore[arg-type]
    )
    await chats.create(chat)

    body = "x" * text_len_per_msg
    seq = 0
    for _ in range(pairs):
        seq += 1
        await msgs.create(
            ChatMessage(
                id=ChatMessage.make_id(chat_id, seq),
                chat_id=chat_id, seq=seq, kind="user_message",
                payload={"content": body},
                created_at=now,
            ),
        )
        seq += 1
        await msgs.create(
            ChatMessage(
                id=ChatMessage.make_id(chat_id, seq),
                chat_id=chat_id, seq=seq, kind="assistant_token",
                payload={"delta": body},
                created_at=now,
            ),
        )
    chat.last_seq = seq
    await chats.update(chat)
    return seq


def _patch_apply_compaction(monkeypatch, *, summary: str) -> None:
    from primer.agent import compaction_mixin

    async def _fake_apply(**_kw) -> CompactionResult:
        return CompactionResult(
            new_history=[],
            summary_text=summary,
            tokens_before=9500,
            tokens_after=400,
            model=MODEL_NAME,
            created_at=datetime.now(timezone.utc),
        )

    monkeypatch.setattr(compaction_mixin, "apply_compaction", _fake_apply)


# ===========================================================================
# Tests.
# ===========================================================================


@pytest.mark.asyncio
class TestChatCompactJourney:
    async def test_manual_compaction_persists_marker_and_lists_via_messages(
        self, client: AsyncClient, app: FastAPI, monkeypatch,
    ) -> None:
        """Force compaction returns 200, the marker row is persisted,
        and ``GET /v1/chats/{id}/messages`` surfaces it.
        """
        await _seed_provider_and_agent(app)
        last_seq = await _seed_high_token_chat(app)
        _patch_apply_compaction(monkeypatch, summary="ROLLED UP HISTORY")

        # 1) Manual compaction → 200, body carries the expected fields.
        resp = await client.post("/v1/chats/c1/compact")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        marker_seq = body["compaction_marker_seq"]
        assert marker_seq == last_seq + 1, body
        assert body["summary"] == "ROLLED UP HISTORY", body
        assert body["tokens_before"] == 9500, body
        assert body["tokens_after"] == 400, body

        # 2) GET /messages with after_seq=last_seq shows the new marker
        #    (mirrors the WS cursor=0 replay shape — same storage scan
        #    seq-ordered ascending).
        resp = await client.get(
            f"/v1/chats/c1/messages?after_seq={last_seq}",
        )
        assert resp.status_code == 200, resp.text
        page = resp.json()
        items = page["items"]
        # The marker row must be in the visible page.
        markers = [i for i in items if i["kind"] == "compaction_marker"]
        assert len(markers) == 1, items
        marker = markers[0]
        assert marker["seq"] == marker_seq, marker
        assert marker["payload"]["summary"] == "ROLLED UP HISTORY", marker
        assert marker["payload"]["trigger"] == "operator_forced", marker
        assert marker["payload"]["model"] == MODEL_NAME, marker

    async def test_second_compaction_against_same_chat_also_succeeds(
        self, client: AsyncClient, app: FastAPI, monkeypatch,
    ) -> None:
        """Two back-to-back ``/compact`` calls both return 200 (no
        idempotency error / no 5xx). Each persists its own marker row.
        """
        await _seed_provider_and_agent(app)
        last_seq = await _seed_high_token_chat(app)
        _patch_apply_compaction(monkeypatch, summary="FIRST PASS")

        first = await client.post("/v1/chats/c1/compact")
        assert first.status_code == 200, first.text
        first_marker = first.json()["compaction_marker_seq"]
        assert first_marker == last_seq + 1

        _patch_apply_compaction(monkeypatch, summary="SECOND PASS")
        second = await client.post("/v1/chats/c1/compact")
        assert second.status_code == 200, second.text
        second_marker = second.json()["compaction_marker_seq"]
        assert second_marker == first_marker + 1

        # Both markers visible via /messages.
        resp = await client.get(
            f"/v1/chats/c1/messages?after_seq={last_seq}",
        )
        assert resp.status_code == 200, resp.text
        items = resp.json()["items"]
        markers = [i for i in items if i["kind"] == "compaction_marker"]
        assert [m["seq"] for m in markers] == [first_marker, second_marker], (
            items
        )
        # Distinct payloads — the second pass didn't clobber the first.
        assert markers[0]["payload"]["summary"] == "FIRST PASS", markers
        assert markers[1]["payload"]["summary"] == "SECOND PASS", markers

    async def test_auto_compaction_threshold_trips_on_seeded_history(
        self, app: FastAPI, fake_llm: _CompactJourneyFakeLLM,
    ) -> None:
        """Direct unit-level check that the seeded high-token history
        actually exceeds the 0.90 threshold. The runner's pre-turn
        ``should_compact`` is what auto-fires compaction; here we
        invoke it against the same loaded history the runner would
        see, to pin that the seed is genuinely past 90% of the
        configured context length.
        """
        from primer.agent.compaction_mixin import should_compact
        from primer.chat.executor import ChatTurnRunner

        await _seed_provider_and_agent(app)
        await _seed_high_token_chat(app)

        agent = await app.state.storage_provider.get_storage(Agent).get(
            AGENT_ID,
        )
        provider = await app.state.storage_provider.get_storage(
            LLMProvider,
        ).get(PROVIDER_ID)
        assert agent is not None and provider is not None

        # Build a stub runner the same way the REST endpoint does so
        # _load_history sees the same surface.
        runner = ChatTurnRunner.__new__(ChatTurnRunner)
        runner._agent = agent
        runner._llm = fake_llm
        runner._model = provider.models[0]
        runner._tools = None
        runner._chats = app.state.storage_provider.get_storage(Chat)
        runner._messages = app.state.storage_provider.get_storage(ChatMessage)
        runner._cancel_event = None
        runner._marker_persisted = False
        runner._last_input_tokens = None
        runner._last_output_tokens = None

        history = await runner._load_history("c1")
        trip, count = await should_compact(
            llm=fake_llm,
            model_name=MODEL_NAME,
            context_length=CONTEXT_LENGTH,
            history=history,
        )
        # Token count must clear the 0.90 of (10_000 - 2000) = 7200
        # trigger that the next user_message would consult.
        assert count >= 7200, count
        assert trip is True, (count, trip)

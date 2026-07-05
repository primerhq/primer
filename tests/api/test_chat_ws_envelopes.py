"""Unit tests for the WS envelope encoders (spec §6.4).

Pure-function tests against ``_compaction_envelope`` /
``_usage_envelope`` / ``_message_to_wire`` plus a bounded WS-client
test that pins the initial ``usage`` envelope on connect.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import pytest_asyncio
from fastapi import FastAPI
from pydantic import SecretStr

from primer.api.app import create_test_app
from primer.api.routers.chats import (
    _compaction_envelope,
    _message_to_wire,
    _usage_envelope,
)
from primer.chat.usage_cache import reset_cache, set_usage
from primer.model.agent import Agent, AgentModel
from primer.model.chats import ChatMessage
from primer.model.provider import (
    AnthropicConfig,
    Limits,
    LLMModel,
    LLMProvider,
    LLMProviderType,
)


@pytest.fixture(autouse=True)
def _reset() -> None:
    reset_cache()


class TestCompactionEnvelope:
    def test_translates_marker_row_to_compaction_envelope(self) -> None:
        row = ChatMessage(
            id=ChatMessage.make_id("c1", 48),
            chat_id="c1",
            seq=48,
            kind="compaction_marker",
            payload={
                "summary": "test summary",
                "tokens_before": 7820,
                "tokens_after": 1180,
                "replaced_from_seq": 1,
                "replaced_to_seq": 47,
                "model": "gpt-4o",
                "compaction_prompt_source": "default",
                "created_at": "2026-05-30T14:30:00Z",
            },
            created_at=datetime.now(timezone.utc),
        )
        env = _compaction_envelope(row)
        assert env == {
            "kind": "compaction",
            "seq": 48,
            "summary": "test summary",
            "tokens_before": 7820,
            "tokens_after": 1180,
            "replaced_from_seq": 1,
            "replaced_to_seq": 47,
        }

    def test_missing_payload_keys_default_to_safe_values(self) -> None:
        row = ChatMessage(
            id=ChatMessage.make_id("c1", 9),
            chat_id="c1",
            seq=9,
            kind="compaction_marker",
            payload={},
            created_at=datetime.now(timezone.utc),
        )
        env = _compaction_envelope(row)
        assert env["summary"] == ""
        assert env["tokens_before"] == 0
        assert env["tokens_after"] == 0
        assert env["replaced_from_seq"] is None
        assert env["replaced_to_seq"] is None


class TestUsageEnvelope:
    def test_zero_when_nothing_cached(self) -> None:
        env = _usage_envelope("c1", context_length=10_000)
        assert env["kind"] == "usage"
        assert env["seq"] is None
        assert env["input_tokens"] == 0
        assert env["output_tokens"] == 0
        assert env["context_length"] == 10_000
        assert env["used_pct"] == 0.0

    def test_reflects_cached_tokens(self) -> None:
        set_usage("c1", input_tokens=1234, output_tokens=56)
        env = _usage_envelope("c1", context_length=10_000)
        assert env["input_tokens"] == 1234
        assert env["output_tokens"] == 56
        assert env["used_pct"] == pytest.approx(0.1234)

    def test_zero_context_length_safe(self) -> None:
        set_usage("c1", input_tokens=100, output_tokens=10)
        env = _usage_envelope("c1", context_length=0)
        assert env["used_pct"] == 0.0  # no divide-by-zero


class TestMessageToWireRouting:
    def test_routes_compaction_marker_to_compaction_envelope(self) -> None:
        row = ChatMessage(
            id=ChatMessage.make_id("c1", 5),
            chat_id="c1",
            seq=5,
            kind="compaction_marker",
            payload={
                "summary": "rolled up",
                "tokens_before": 100,
                "tokens_after": 10,
                "replaced_from_seq": 1,
                "replaced_to_seq": 4,
            },
            created_at=datetime.now(timezone.utc),
        )
        wire = _message_to_wire(row)
        assert wire["kind"] == "compaction"
        assert wire["seq"] == 5
        assert wire["summary"] == "rolled up"

    def test_passes_non_compaction_rows_through(self) -> None:
        row = ChatMessage(
            id=ChatMessage.make_id("c1", 7),
            chat_id="c1",
            seq=7,
            kind="assistant_token",
            payload={"delta": "hello"},
            created_at=datetime.now(timezone.utc),
        )
        wire = _message_to_wire(row)
        assert wire == {"kind": "assistant_token", "seq": 7, "delta": "hello"}


# ---------------------------------------------------------------------------
# End-to-end WS handshake test: confirms the initial usage envelope arrives
# ---------------------------------------------------------------------------


@pytest.fixture
def envelope_app(fake_storage_provider, fake_provider_registry) -> FastAPI:
    """App fixture for the initial-usage WS test.

    Uses ``start_chat_worker=True`` to match the test_chats.py setup
    that drives auth + WS through SyncTestClient; the worker pool is
    not exercised by this test (we never send a user_message), but
    the attached lifespan ensures the event_bus / tick router are
    wired correctly when SyncTestClient runs the app.
    """
    return create_test_app(
        storage_provider=fake_storage_provider,
        provider_registry=fake_provider_registry,
        start_chat_worker=True,
    )


@pytest_asyncio.fixture
async def envelope_seeded_agent(envelope_app: FastAPI) -> Agent:
    sp = envelope_app.state.storage_provider
    await sp.get_storage(LLMProvider).create(
        LLMProvider(
            id="llm-env",
            provider=LLMProviderType.ANTHROPIC,
            models=[LLMModel(name="m-env", context_length=12_345)],
            config=AnthropicConfig(api_key=SecretStr("test-only")),
            limits=Limits(max_concurrency=1),
        ),
    )
    agent = Agent(
        id="ag-env",
        description="envelope-test agent",
        model=AgentModel(provider_id="llm-env", model_name="m-env"),
        tools=[],
        system_prompt=[],
    )
    await sp.get_storage(Agent).create(agent)
    return agent


class TestUsageEnvelopeOnConnect:
    """Pin the spec §6.4 initial ``usage`` frame contract end-to-end."""

    def test_initial_usage_frame_appears_in_first_frames(
        self, envelope_app, envelope_seeded_agent,
    ) -> None:
        """Connect, receive the initial ``usage`` envelope, then use a
        ping/pong round trip as a bounded sync barrier so we never
        wait open-ended on a frame that might not come (that's how
        the earlier T10.1 attempt wedged for 6 hours).
        """
        from starlette.testclient import TestClient as SyncTestClient

        with SyncTestClient(envelope_app) as sclient:
            sclient.post(
                "/v1/auth/register",
                json={"username": "envuser", "password": "envpass123"},
            )
            sclient.post(
                "/v1/auth/login",
                json={"username": "envuser", "password": "envpass123"},
            )
            r = sclient.post("/v1/chats", json={"agent_id": "ag-env"})
            assert r.status_code == 201, r.text
            cid = r.json()["id"]

            with sclient.websocket_connect(f"/v1/chats/{cid}/ws") as ws:
                frames: list[dict] = []
                # First frame must be the initial usage envelope.
                frames.append(ws.receive_json())
                # Use ping/pong as a bounded second receive — pong is
                # the only other frame the server is guaranteed to
                # send without further activity.
                ws.send_json({"kind": "ping"})
                frames.append(ws.receive_json())

        assert frames[0].get("kind") == "usage", frames
        usage = frames[0]
        assert usage["seq"] is None
        # The seeded LLMProvider declared context_length=12345.
        assert usage["context_length"] == 12_345
        # No tokens consumed yet.
        assert usage["input_tokens"] == 0
        assert usage["output_tokens"] == 0
        assert usage["used_pct"] == 0.0
        # And the pong arrives after the usage frame — proves the
        # initial envelope didn't displace normal protocol traffic.
        assert frames[1].get("kind") == "pong", frames


class TestUsageRecoveryFromHistory:
    """The usage numerator must survive a process restart: the live cache
    is volatile, so a cold cache is re-seeded from the last persisted
    ``done`` row (which now carries the turn's token counts)."""

    @pytest.mark.asyncio
    async def test_seed_recovers_last_done_usage_when_cache_cold(
        self, fake_storage_provider,
    ) -> None:
        from primer.api.routers.chats import _seed_usage_cache_from_history
        from primer.chat.usage_cache import get_usage
        from primer.model.chats import Chat

        sp = fake_storage_provider
        await sp.get_storage(Chat).create(
            Chat(
                id="c9", agent_id="ag",
                created_at=datetime.now(timezone.utc),
                last_seq=3, next_unprocessed_seq=4,
            )
        )
        await sp.get_storage(ChatMessage).create(
            ChatMessage(
                id=ChatMessage.make_id("c9", 3), chat_id="c9", seq=3,
                kind="done",
                payload={
                    "stop_reason": "stop",
                    "input_tokens": 4096, "output_tokens": 128,
                },
                created_at=datetime.now(timezone.utc),
            )
        )
        # Cold cache (post-restart) → numerator would be 0 without recovery.
        assert get_usage("c9") == {"input_tokens": 0, "output_tokens": 0}
        await _seed_usage_cache_from_history(sp, "c9")
        assert get_usage("c9") == {"input_tokens": 4096, "output_tokens": 128}

    @pytest.mark.asyncio
    async def test_seed_is_noop_when_cache_already_warm(
        self, fake_storage_provider,
    ) -> None:
        from primer.api.routers.chats import _seed_usage_cache_from_history
        from primer.chat.usage_cache import get_usage, set_usage
        from primer.model.chats import Chat

        sp = fake_storage_provider
        set_usage("c9", input_tokens=999, output_tokens=9)  # a live turn
        await sp.get_storage(Chat).create(
            Chat(
                id="c9", agent_id="ag",
                created_at=datetime.now(timezone.utc),
                last_seq=3, next_unprocessed_seq=4,
            )
        )
        await sp.get_storage(ChatMessage).create(
            ChatMessage(
                id=ChatMessage.make_id("c9", 3), chat_id="c9", seq=3,
                kind="done", payload={"input_tokens": 1, "output_tokens": 1},
                created_at=datetime.now(timezone.utc),
            )
        )
        await _seed_usage_cache_from_history(sp, "c9")
        # Live counters win — history must not clobber a warmer cache.
        assert get_usage("c9") == {"input_tokens": 999, "output_tokens": 9}

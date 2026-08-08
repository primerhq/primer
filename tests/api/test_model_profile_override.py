"""Per-run ModelProfile overrides on the session and chat surfaces.

An agent's ``model.profile_id`` is a DEFAULT. Session create and chat
create/switch may name a different profile for that run, so one agent
definition can run against a cheap non-reasoning profile in one place and
a reasoning profile in another.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from primer.model.chats import Chat
from primer.model.workspace_session import AgentSessionBinding


class TestSessionBindingOverride:
    def test_defaults_to_none(self) -> None:
        """Omitted means 'use the agent's own default'."""
        b = AgentSessionBinding(agent_id="ag")
        assert b.profile_id is None

    def test_accepts_an_override(self) -> None:
        b = AgentSessionBinding(agent_id="ag", profile_id="gx10--qwen-fast")
        assert b.profile_id == "gx10--qwen-fast"

    def test_round_trips_through_json(self) -> None:
        b = AgentSessionBinding(agent_id="ag", profile_id="p--m")
        assert AgentSessionBinding.model_validate(
            b.model_dump(mode="json")
        ) == b


class TestChatOverride:
    def test_defaults_to_none(self) -> None:
        from datetime import UTC, datetime

        c = Chat(id="c1", agent_id="ag", created_at=datetime.now(UTC))
        assert c.profile_id is None


class TestChatRoutes:
    @pytest_asyncio.fixture(autouse=True)
    async def _seed_agent(self, app):
        """The chat routes resolve the agent before creating the row."""
        from primer.model.agent import Agent, AgentModel

        store = app.state.storage_provider.get_storage(Agent)
        for aid in ("ag", "ag-other"):
            await store.create(
                Agent(
                    id=aid,
                    description="override-test agent",
                    model=AgentModel(profile_id="p--m"),
                ),
            )

    @pytest.mark.asyncio
    async def test_create_accepts_a_profile_override(self, client) -> None:
        r = await client.post(
            "/v1/chats", json={"agent_id": "ag", "profile_id": "p--m"},
        )
        assert r.status_code in (200, 201), r.text
        assert r.json()["profile_id"] == "p--m"

    @pytest.mark.asyncio
    async def test_create_without_an_override_is_null(self, client) -> None:
        r = await client.post("/v1/chats", json={"agent_id": "ag"})
        assert r.status_code in (200, 201), r.text
        assert r.json()["profile_id"] is None

    @pytest.mark.asyncio
    async def test_switch_clears_a_stale_override_when_omitted(
        self, client
    ) -> None:
        """An omitted profile_id on a switch means 'the new agent's own
        default', not 'keep the previous agent's override' -- carrying one
        across would apply one agent's model choice to a different agent.
        """
        r = await client.post(
            "/v1/chats", json={"agent_id": "ag", "profile_id": "p--m"},
        )
        cid = r.json()["id"]
        assert r.json()["profile_id"] == "p--m"

        # A switch to the SAME agent short-circuits as an idempotent
        # no-op, so this must target a genuinely different agent.
        sw = await client.post(
            f"/v1/chats/{cid}/agent", json={"agent_id": "ag-other"},
        )
        assert sw.status_code == 200, sw.text
        assert sw.json()["profile_id"] is None

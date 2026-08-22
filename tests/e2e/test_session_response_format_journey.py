"""Journey: structured output on sessions (S1 P2 Task 16).

Ported from tests/e2e/test_chat_response_format_journey.py. The PUT
persists a schema for every later turn; a steer can carry one for a
single turn that outranks it.

Precedence (spec decision 8): ephemeral beats session beats agent. The
ephemeral value is popped rather than read, which is what stops a
retried turn silently re-applying a schema the caller asked for once.

In-process app with fake storage; no live server. PRIMER_RUN_E2E=1
lifts the default skip.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from primer.api.app import create_test_app
from primer.model.agent import Agent, AgentModel
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.session.response_format import (
    EPHEMERAL_KEY,
    effective_response_format,
    pop_ephemeral,
)
from tests.api.conftest import fake_provider_registry  # noqa: F401
from tests.conftest import _FakeStorageProvider  # noqa: F401

AGENT_ID = "ag-session-rf"
WID = "ws-rf"
SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}}


@pytest.fixture
def app(fake_storage_provider, fake_provider_registry) -> FastAPI:
    return create_test_app(
        storage_provider=fake_storage_provider,
        provider_registry=fake_provider_registry,
        start_chat_worker=False,
    )


@pytest_asyncio.fixture
async def client(app: FastAPI):
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


async def _seed(app: FastAPI, sid: str, **over) -> None:
    sp = app.state.storage_provider
    if await sp.get_storage(Agent).get(AGENT_ID) is None:
        await sp.get_storage(Agent).create(
            Agent(id=AGENT_ID, description="rf journey",
                  model=AgentModel(profile_id="p--m"), tools=[],
                  system_prompt=[]),
        )
    fields = {
        "id": sid, "workspace_id": WID,
        "binding": AgentSessionBinding(agent_id=AGENT_ID),
        "status": SessionStatus.WAITING,
        "created_at": datetime.now(UTC),
        "turn_status": "idle",
    }
    fields.update(over)
    await sp.get_storage(WorkspaceSession).create(WorkspaceSession(**fields))


async def _row(app: FastAPI, sid: str) -> WorkspaceSession:
    return await app.state.storage_provider.get_storage(
        WorkspaceSession
    ).get(sid)


@pytest.mark.asyncio
class TestSessionResponseFormatJourney:
    async def test_put_persists_the_schema(
        self, client: AsyncClient, app: FastAPI,
    ) -> None:
        await _seed(app, "rf-1")
        r = await client.put(
            f"/v1/workspaces/{WID}/sessions/rf-1/response_format",
            json={"response_format": SCHEMA},
        )
        assert r.status_code == 200, r.text
        assert (await _row(app, "rf-1")).response_format == SCHEMA

    async def test_put_null_clears_it(
        self, client: AsyncClient, app: FastAPI,
    ) -> None:
        await _seed(app, "rf-2", response_format=SCHEMA)
        r = await client.put(
            f"/v1/workspaces/{WID}/sessions/rf-2/response_format",
            json={"response_format": None},
        )
        assert r.status_code == 200, r.text
        assert (await _row(app, "rf-2")).response_format is None

    async def test_put_is_refused_mid_turn(
        self, client: AsyncClient, app: FastAPI,
    ) -> None:
        """Accepting would imply an effect on a turn that already
        resolved its format."""
        await _seed(app, "rf-3", turn_status="running",
                    status=SessionStatus.RUNNING)
        r = await client.put(
            f"/v1/workspaces/{WID}/sessions/rf-3/response_format",
            json={"response_format": SCHEMA},
        )
        assert r.status_code == 409, r.text

    async def test_put_rejects_an_invalid_schema(
        self, client: AsyncClient, app: FastAPI,
    ) -> None:
        """Caught here rather than as a provider error mid-turn."""
        await _seed(app, "rf-4")
        r = await client.put(
            f"/v1/workspaces/{WID}/sessions/rf-4/response_format",
            json={"response_format": {"type": 42}},
        )
        assert r.status_code == 422, r.text

    async def test_put_unknown_session_404s(
        self, client: AsyncClient, app: FastAPI,
    ) -> None:
        await _seed(app, "rf-5")
        r = await client.put(
            f"/v1/workspaces/{WID}/sessions/nope/response_format",
            json={"response_format": SCHEMA},
        )
        assert r.status_code == 404, r.text

    async def test_precedence_ephemeral_over_session_over_agent(
        self, client: AsyncClient, app: FastAPI,
    ) -> None:
        """The rule the two entry points exist to express."""
        await _seed(app, "rf-6")
        await client.put(
            f"/v1/workspaces/{WID}/sessions/rf-6/response_format",
            json={"response_format": {"title": "session"}},
        )
        row = await _row(app, "rf-6")
        assert effective_response_format(
            row, agent_default={"title": "agent"},
        ) == {"title": "session"}

        row.metadata[EPHEMERAL_KEY] = {"title": "ephemeral"}
        assert effective_response_format(
            row, agent_default={"title": "agent"},
        ) == {"title": "ephemeral"}

        # Popped, so the turn after it falls back to the session value.
        assert pop_ephemeral(row) == {"title": "ephemeral"}
        assert effective_response_format(
            row, agent_default={"title": "agent"},
        ) == {"title": "session"}

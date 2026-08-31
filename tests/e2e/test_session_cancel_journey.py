"""Journey: stopping a session turn (S1 P2 Task 16).

Ported from tests/e2e/test_chat_cancel_journey.py, and the port is not
one-to-one. Chat had a single cancel verb; sessions split it in two:

  interrupt  stop the in-flight turn, session stays alive (WAITING)
  cancel     hard end, session reaches ENDED/cancelled

Both publish session:{sid}:cancel so the worker's watcher preempts the
turn, which is why the distinction lives in what the row becomes rather
than in the signal.

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
from tests.api.conftest import fake_provider_registry  # noqa: F401
from tests.conftest import _FakeStorageProvider  # noqa: F401

AGENT_ID = "ag-session-cancel"
WID = "ws-cancel"


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


async def _seed(app: FastAPI, sid: str, **over) -> WorkspaceSession:
    sp = app.state.storage_provider
    if await sp.get_storage(Agent).get(AGENT_ID) is None:
        await sp.get_storage(Agent).create(
            Agent(id=AGENT_ID, description="cancel journey",
                  model=AgentModel(profile_id="p--m"), tools=[],
                  system_prompt=[]),
        )
    fields = {
        "id": sid, "workspace_id": WID,
        "binding": AgentSessionBinding(agent_id=AGENT_ID),
        "status": SessionStatus.RUNNING,
        "created_at": datetime.now(UTC),
        "turn_status": "running",
    }
    fields.update(over)
    row = WorkspaceSession(**fields)
    await sp.get_storage(WorkspaceSession).create(row)
    return row


async def _row(app: FastAPI, sid: str) -> WorkspaceSession:
    return await app.state.storage_provider.get_storage(
        WorkspaceSession
    ).get(sid)


@pytest.mark.asyncio
class TestSessionInterruptJourney:
    async def test_interrupt_flags_a_running_turn_and_keeps_the_session(
        self, client: AsyncClient, app: FastAPI,
    ) -> None:
        """The stop button: the turn dies, the session does not."""
        await _seed(app, "s-int")
        r = await client.post(f"/v1/workspaces/{WID}/sessions/s-int/interrupt")
        assert r.status_code == 200, r.text

        fresh = await _row(app, "s-int")
        assert fresh.interrupt_requested is True
        assert fresh.status is not SessionStatus.ENDED

    async def test_interrupt_on_an_idle_session_is_a_noop(
        self, client: AsyncClient, app: FastAPI,
    ) -> None:
        """Nothing to stop is not an error: the button stays harmless."""
        await _seed(
            app, "s-idle", status=SessionStatus.WAITING, turn_status="idle",
        )
        r = await client.post(f"/v1/workspaces/{WID}/sessions/s-idle/interrupt")
        assert r.status_code == 200, r.text
        assert (await _row(app, "s-idle")).status is not SessionStatus.ENDED

    async def test_interrupt_on_an_ended_session_is_409(
        self, client: AsyncClient, app: FastAPI,
    ) -> None:
        await _seed(
            app, "s-done", status=SessionStatus.ENDED, turn_status="idle",
            ended_reason="completed",
        )
        r = await client.post(f"/v1/workspaces/{WID}/sessions/s-done/interrupt")
        assert r.status_code == 409, r.text

    async def test_unknown_session_404s(
        self, client: AsyncClient, app: FastAPI,
    ) -> None:
        await _seed(app, "s-any")
        r = await client.post(f"/v1/workspaces/{WID}/sessions/nope/interrupt")
        assert r.status_code == 404, r.text


@pytest.mark.asyncio
class TestSessionCancelJourney:
    async def test_cancel_ends_an_unleased_session_directly(
        self, client: AsyncClient, app: FastAPI,
    ) -> None:
        """No worker holds it, so there is nothing to preempt: end it."""
        await _seed(
            app, "c-wait", status=SessionStatus.WAITING, turn_status="idle",
        )
        r = await client.post(f"/v1/workspaces/{WID}/sessions/c-wait/cancel")
        assert r.status_code == 200, r.text

        fresh = await _row(app, "c-wait")
        assert fresh.status is SessionStatus.ENDED
        assert fresh.ended_reason == "cancelled"

    async def test_cancel_on_an_ended_session_is_409(
        self, client: AsyncClient, app: FastAPI,
    ) -> None:
        await _seed(
            app, "c-done", status=SessionStatus.ENDED, turn_status="idle",
            ended_reason="completed",
        )
        r = await client.post(f"/v1/workspaces/{WID}/sessions/c-done/cancel")
        assert r.status_code == 409, r.text

    async def test_cancel_differs_from_interrupt_on_the_same_state(
        self, client: AsyncClient, app: FastAPI,
    ) -> None:
        """The distinction the chat original could not express."""
        await _seed(
            app, "x-int", status=SessionStatus.WAITING, turn_status="idle",
        )
        await _seed(
            app, "x-can", status=SessionStatus.WAITING, turn_status="idle",
        )

        await client.post(f"/v1/workspaces/{WID}/sessions/x-int/interrupt")
        await client.post(f"/v1/workspaces/{WID}/sessions/x-can/cancel")

        assert (await _row(app, "x-int")).status is not SessionStatus.ENDED
        assert (await _row(app, "x-can")).status is SessionStatus.ENDED

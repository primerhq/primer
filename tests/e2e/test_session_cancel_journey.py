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

import json
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
from primer.toolset.workspaces import build_workspaces_toolset
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


@pytest.mark.asyncio
class TestSessionInterruptToolJourney:
    """Stop as a system tool (task 01a10871): the same journeys, driven through the workspaces toolset on the SAME app, storage and bus
    the REST client reads, so the two surfaces are seen to agree end to end. (The toolset is built the way the app builds it, without a
    scheduler or claim engine: a Stop needs neither.)"""

    @staticmethod
    def _stop_tool(app: FastAPI):
        toolset = build_workspaces_toolset(
            storage_provider=app.state.storage_provider, workspace_registry=app.state.workspace_registry,
            scheduler=None, claim_engine=None, event_bus=app.state.event_bus,
        )

        async def stop(sid: str):
            return await toolset.call(
                tool_name="interrupt_workspace_session", arguments={"workspace_id": WID, "session_id": sid},
            )

        return stop

    async def test_the_tool_stops_a_running_turn_and_the_rest_row_shows_a_live_session(
        self, client: AsyncClient, app: FastAPI,
    ) -> None:
        await _seed(app, "t-int")

        result = await self._stop_tool(app)("t-int")

        assert not result.is_error, result.output
        assert json.loads(result.output)["interrupt_requested"] is True
        over_rest = (await client.get("/v1/sessions/t-int")).json()
        assert over_rest["interrupt_requested"] is True and over_rest["status"] != "ended"

    async def test_the_tool_on_an_idle_session_says_nothing_was_recorded(
        self, client: AsyncClient, app: FastAPI,
    ) -> None:
        """The trap the description warns about: a success answer with ``interrupt_requested: false`` is NOT a Stop."""
        await _seed(app, "t-idle", status=SessionStatus.WAITING, turn_status="idle")

        result = await self._stop_tool(app)("t-idle")

        assert not result.is_error, result.output
        assert json.loads(result.output)["interrupt_requested"] is False
        assert (await _row(app, "t-idle")).interrupt_requested is False

    async def test_the_tool_on_an_ended_session_is_a_conflict(self, client: AsyncClient, app: FastAPI) -> None:
        await _seed(app, "t-done", status=SessionStatus.ENDED, turn_status="idle", ended_reason="completed")

        result = await self._stop_tool(app)("t-done")

        assert result.is_error and json.loads(result.output)["type"] == "conflict"

    async def test_a_stopped_session_stays_alive_and_a_cancel_over_rest_then_ends_it(
        self, client: AsyncClient, app: FastAPI,
    ) -> None:
        """Stop and Cancel are different verbs on the same session, whichever surface says them."""
        await _seed(app, "t-then-cancel")

        await self._stop_tool(app)("t-then-cancel")
        assert (await _row(app, "t-then-cancel")).status is not SessionStatus.ENDED

        r = await client.post(f"/v1/workspaces/{WID}/sessions/t-then-cancel/cancel")
        assert r.status_code == 200, r.text
        assert (await _row(app, "t-then-cancel")).cancel_requested is True

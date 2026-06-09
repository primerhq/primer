from datetime import datetime, timezone

import pytest

from primer.int.claim import ParkRequest, ReleaseOutcome
from primer.claim.adapters.sessions import SessionClaimAdapter
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)

pytestmark = pytest.mark.asyncio


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _make_session(session_id: str, turn_no: int = 3) -> WorkspaceSession:
    return WorkspaceSession(
        id=session_id,
        workspace_id="w1",
        binding=AgentSessionBinding(agent_id="ag1"),
        status=SessionStatus.RUNNING,
        created_at=_now(),
        turn_no=turn_no,
    )


class FakeStorage:
    def __init__(self, session: WorkspaceSession) -> None:
        self._session = session
        self.updated: list[WorkspaceSession] = []

    async def get(self, id: str, *, conn=None) -> WorkspaceSession | None:
        return self._session if self._session.id == id else None

    async def update(self, entity: WorkspaceSession, *, conn=None) -> WorkspaceSession:
        self.updated.append(entity)
        self._session = entity
        return entity


@pytest.fixture
def storage() -> FakeStorage:
    return FakeStorage(_make_session("sess-1", turn_no=3))


async def test_on_release_park_writes_columns_no_turn_bump(storage: FakeStorage) -> None:
    """When outcome.park is set, park columns are written and turn_no is NOT bumped."""
    adapter = SessionClaimAdapter(session_storage=storage)
    now = datetime.now(timezone.utc)
    park = ParkRequest(
        parked_state={"schema_version": 1, "yielded": {"tool_name": "ask_user"}},
        parked_event_key="ask_user:sess-1:tc-1",
        parked_until=now,
        parked_at=now,
    )
    await adapter.on_release(
        conn=None,
        entity_id="sess-1",
        outcome=ReleaseOutcome(success=True, drop_lease=True, park=park),
    )
    row = storage._session
    assert row.parked_status == "parked"
    assert row.parked_event_key == "ask_user:sess-1:tc-1"
    assert row.parked_state is not None
    assert row.parked_state["yielded"]["tool_name"] == "ask_user"
    assert row.parked_at == now
    assert row.parked_until == now
    assert row.turn_no == 3  # NOT bumped


async def test_on_release_success_without_park_clears_and_bumps(storage: FakeStorage) -> None:
    """A successful release with no park clears park columns and bumps turn_no."""
    adapter = SessionClaimAdapter(session_storage=storage)
    await adapter.on_release(
        conn=None,
        entity_id="sess-1",
        outcome=ReleaseOutcome(success=True, drop_lease=True),
    )
    row = storage._session
    assert row.parked_status is None
    assert row.parked_event_key is None
    assert row.parked_state is None
    assert row.turn_no == 4  # bumped from 3


async def test_on_release_failure_does_not_bump(storage: FakeStorage) -> None:
    """A failed release with no park clears park columns but does NOT bump turn_no."""
    adapter = SessionClaimAdapter(session_storage=storage)
    await adapter.on_release(
        conn=None,
        entity_id="sess-1",
        outcome=ReleaseOutcome(success=False, drop_lease=True, last_error="boom"),
    )
    row = storage._session
    assert row.turn_no == 3  # NOT bumped

"""Rename, the response-format PUT and resume write only their own fields; delete still refuses a running session.

Each of these routes reads the session row and then writes it. They used to write the WHOLE row they read
(``sessions.update(row)``), so whatever another writer committed between the read and the write was put back: a park
the turn committed (``parked_status``, ``parked_state`` and the rest revert to the snapshot's no-park values, and the
parked assistant message, which lives only in ``parked_state``, is lost), the hook's ``parked -> resumable`` flip, a
cancel that ended the row. Each now writes one field-scoped ``patch_if`` of the fields it changes:

* rename and the response-format PUT patch their ONE field, fenced on ``workspace_id`` (immutable, so the fence always
  holds): a running or parked session still renames, and nothing is refused that was not refused before;
* resume patches ``status``, ``pause_requested`` and ``started_at`` (only when the row it read has none), fenced on the
  status it read ALONE (no park term: the hook flips ``parked`` to ``resumable`` under a PAUSED row resume has just
  read). It arms (``scheduler.enqueue`` and the lease upsert) only after a write that LANDED; a rejected write re-reads
  once (ENDED is 409, RUNNING is the idempotent no-op, any other resumable status retries once, a second rejection is
  409).

The races run on a REAL SQLite storage: the in-memory fakes hand back the stored object itself, so the route's
mutation of its snapshot would be the stored row before any write and no race could happen. The competing write is
committed by a wrapper around the session storage handle at the moment the route's write arrives (after the route's
read, before its write), through the real storage; the wrapper also counts the route's write attempts.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pydantic_core import to_jsonable_python

from primer.api.app import create_test_app
from primer.api.registries import ProviderRegistry, WorkspaceRegistry
from primer.claim.adapters.sessions import SessionClaimAdapter
from primer.int.claim import ClaimKind, ParkRequest, ReleaseOutcome
from primer.model.provider import SqliteConfig
from primer.model.workspace import LocalWorkspaceConfig, Workspace, WorkspaceProvider, WorkspaceProviderType
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.model.yield_ import Yielded
from primer.storage.sqlite import SqliteStorageProvider
from primer.worker.yield_runtime import ParkedState, ToolWaitParkedState
from tests.api.test_sessions import _FakeBackendForSessions, _FakeClaimEngine, _runtime_meta

WID = "ws-idle"
SID = "sess-idle"
SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}}


# ---------------------------------------------------------------------------
# Fixtures: the sessions suite's fake workspace backend and claim-engine spy, on a SQLite storage
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def sp(tmp_path):
    provider = SqliteStorageProvider(SqliteConfig(path=tmp_path / "idle-writers.sqlite"))
    await provider.initialize()
    yield provider
    await provider.aclose()


@pytest.fixture
def app(sp):
    registry = ProviderRegistry(
        sp,  # type: ignore[arg-type]
        llm_factory=lambda p: object(),
        embedder_factory=lambda p: object(),
        cross_encoder_factory=lambda p: object(),
        toolset_factory=lambda p: object(),
    )
    _app = create_test_app(
        storage_provider=sp,  # type: ignore[arg-type]
        provider_registry=registry,
        workspace_registry=WorkspaceRegistry(sp, factory=_FakeBackendForSessions),  # type: ignore[arg-type]
    )
    _app.state.claim_engine = _FakeClaimEngine()
    return _app


@pytest_asyncio.fixture
async def client(app, sp):
    await sp.get_storage(WorkspaceProvider).create(WorkspaceProvider(
        id="p-idle", provider=WorkspaceProviderType.LOCAL, config=LocalWorkspaceConfig(root_path="/tmp/primer-idle"),
    ))
    await sp.get_storage(Workspace).create(Workspace(
        id=WID, template_id="t-1", provider_id="p-idle", created_at=datetime.now(UTC), runtime_meta=_runtime_meta(),
    ))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        try:
            await c.post("/v1/auth/register", json={"username": "testuser", "password": "testpassword"})
        except Exception:  # noqa: BLE001
            pass
        yield c


@pytest.fixture
def enqueued(app, monkeypatch) -> list[str]:
    """Every ``scheduler.enqueue`` the routes make."""
    calls: list[str] = []
    real = app.state.scheduler.enqueue

    async def spy(session_id: str, **kwargs: Any) -> None:
        calls.append(session_id)
        await real(session_id, **kwargs)

    monkeypatch.setattr(app.state.scheduler, "enqueue", spy)
    return calls


def _upserted(app) -> list[tuple]:
    return list(app.state.claim_engine.upserted)


# ---------------------------------------------------------------------------
# Rows and parks
# ---------------------------------------------------------------------------


_STARTED = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)


def _row(**over: Any) -> WorkspaceSession:
    fields: dict[str, Any] = dict(
        id=SID, workspace_id=WID, binding=AgentSessionBinding(agent_id="ag-idle"), status=SessionStatus.WAITING,
        created_at=_STARTED, started_at=_STARTED, turn_no=3, last_seq=7, next_unprocessed_seq=8, turn_status="idle",
    )
    fields.update(over)
    return WorkspaceSession(**fields)


def _tool_wait_park(parked_status: str = "parked") -> dict[str, Any]:
    """A flag-on park: the turn parked on a tool batch (the blob dispatch writes for an agent-bound batch)."""
    key = f"tool_wait:{SID}:3:x"
    state = ToolWaitParkedState(
        outstanding_task_ids=[f"{SID}/x:tool:3:1"], notifying_task_ids=[], event_key=key,
        llm_messages=[{"role": "assistant", "content": "calling the tool"}], turn_no=3, started_at=_STARTED,
    ).to_jsonable()
    return dict(
        parked_status=parked_status, parked_event_key=key, parked_until=None,
        parked_at=_STARTED + timedelta(minutes=1), parked_state=state,
    )


def _ask_user_park(parked_status: str = "parked") -> dict[str, Any]:
    """A flag-off park: an ``ask_user`` yield (answered when ``resumable``)."""
    key = f"ask_user:{SID}:tc-1"
    state = ParkedState(
        yielded=Yielded(tool_name="ask_user", event_key=key, timeout=600.0),
        llm_messages=[{"role": "assistant", "content": "asking"}], turn_no=3, started_at=_STARTED,
        tool_call_id="tc-1", resume_event_payload={"answer": "yes"} if parked_status == "resumable" else None,
    ).to_jsonable()
    return dict(
        parked_status=parked_status, parked_event_key=key, parked_until=_STARTED + timedelta(minutes=11),
        parked_at=_STARTED + timedelta(minutes=1), parked_state=state,
    )


async def _seed(sp, **over: Any) -> WorkspaceSession:
    sessions = sp.get_storage(WorkspaceSession)
    await sessions.create(_row(**over))
    return await sessions.get(SID)


async def _stored(sp) -> WorkspaceSession | None:
    return await sp.get_storage(WorkspaceSession).get(SID)


def _park_fields(row: WorkspaceSession) -> dict[str, Any]:
    return {
        k: getattr(row, k)
        for k in ("parked_status", "parked_event_key", "parked_event_keys", "parked_until", "parked_at", "parked_state")
    }


# ---------------------------------------------------------------------------
# The competing writes
# ---------------------------------------------------------------------------


Write = Callable[[Any], Awaitable[None]]


def _park(park: dict[str, Any]) -> Write:
    """The turn parks: the production park write (``SessionClaimAdapter.on_release`` with a ``ParkRequest``)."""

    async def write(storage) -> None:
        await SessionClaimAdapter(session_storage=storage).on_release(None, SID, outcome=ReleaseOutcome(
            success=True, drop_lease=True, park=ParkRequest(
                parked_state=park["parked_state"], parked_event_key=park["parked_event_key"],
                parked_until=park["parked_until"], parked_at=park["parked_at"],
            ),
        ))

    return write


async def _flip_to_resumable(storage) -> None:
    """The batch's hook wakes the park: ``parked -> resumable``, the park status only, as the hook writes it."""
    flipped = await storage.patch_if(SID, {"parked_status": "resumable"}, where={"parked_status": ["parked"]})
    assert flipped is not None


def _set(**fields: Any) -> Write:
    """Another writer moves the row (a cancel, a resume in another process, a status change)."""

    async def write(storage) -> None:
        moved = await storage.patch_if(SID, to_jsonable_python(fields), where={"workspace_id": [WID]})
        assert moved is not None

    return write


def _then(*writes: Write) -> Write:
    async def write(storage) -> None:
        for w in writes:
            await w(storage)

    return write


class _CommitBeforeWrite:
    """Wraps the session storage handle every route shares. Each write the ROUTE makes to the row (a whole-row
    ``update`` or a ``patch_if``) first commits the next queued competing write through the real storage, then goes
    through; ``attempts`` records the route's write attempts. Writes made by the competing writers are not counted."""

    def __init__(self, monkeypatch, storage, *competing: Write) -> None:
        self.attempts: list[str] = []
        self._competing = list(competing)
        self._inside = False
        real_update, real_patch_if = storage.update, storage.patch_if

        async def update(entity, *args: Any, **kwargs: Any):
            if not self._inside and getattr(entity, "id", None) == SID:
                self.attempts.append("update")
                await self._commit_next(storage)
            return await real_update(entity, *args, **kwargs)

        async def patch_if(row_id, patch=None, *args: Any, **kwargs: Any):
            if not self._inside and row_id == SID:
                self.attempts.append("patch_if")
                await self._commit_next(storage)
            return await real_patch_if(row_id, patch, *args, **kwargs)

        monkeypatch.setattr(storage, "update", update)
        monkeypatch.setattr(storage, "patch_if", patch_if)

    async def _commit_next(self, storage) -> None:
        if not self._competing:
            return
        write = self._competing.pop(0)
        self._inside = True
        try:
            await write(storage)
        finally:
            self._inside = False


def _wrap(monkeypatch, sp, *competing: Write) -> _CommitBeforeWrite:
    return _CommitBeforeWrite(monkeypatch, sp.get_storage(WorkspaceSession), *competing)


# ---------------------------------------------------------------------------
# rename
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", ["running_turn_parks", "parked_row_is_woken"])
async def test_rename_of_a_running_or_parked_session_still_works_and_cannot_erase_a_park(
    app, client, sp, monkeypatch, case: str,
) -> None:
    """N105, N134. A RUNNING session whose turn parks between the rename's read and its write, and a parked session
    whose park the hook flips to ``resumable`` in that window. The rename lands (the row carries the new name: no
    guard refuses a running or parked rename) and the competing write survives it."""
    park = _tool_wait_park()
    if case == "running_turn_parks":
        await _seed(sp, status=SessionStatus.RUNNING, turn_status="running")
        competing, expected_status = _park(park), "parked"
    else:
        await _seed(sp, status=SessionStatus.RUNNING, **park)
        competing, expected_status = _flip_to_resumable, "resumable"
    ws = await app.state.workspace_registry.get_workspace(WID)
    await ws.start_session(AgentSessionBinding(agent_id="ag-idle"), id=SID)
    writes = _wrap(monkeypatch, sp, competing)

    r = await client.patch(f"/v1/workspaces/{WID}/sessions/{SID}", json={"name": "renamed"})

    assert r.status_code == 200, r.text
    assert r.json()["name"] == "renamed"
    assert len(writes.attempts) == 1
    row = await _stored(sp)
    assert row.name == "renamed", "the rename did not reach the session row"
    assert row.status == SessionStatus.RUNNING
    assert _park_fields(row) == {**_park_fields(_row(**park)), "parked_status": expected_status}, (
        "the rename wrote its snapshot back over the park committed after its read"
    )


# ---------------------------------------------------------------------------
# PUT response_format
# ---------------------------------------------------------------------------


async def test_response_format_racing_a_turn_that_parks_keeps_the_park_and_the_turn(
    client, sp, monkeypatch,
) -> None:
    """The PUT reads an idle row; before its write a steer's turn starts and parks. The schema lands and the turn's
    own fields (status, turn_status, last_seq) and its park survive."""
    await _seed(sp)
    park = _tool_wait_park()
    writes = _wrap(monkeypatch, sp, _then(
        _set(status=SessionStatus.RUNNING, turn_status="running", last_seq=8), _park(park),
    ))

    r = await client.put(f"/v1/workspaces/{WID}/sessions/{SID}/response_format", json={"response_format": SCHEMA})

    assert r.status_code == 200, r.text
    assert r.json()["response_format"] == SCHEMA
    assert len(writes.attempts) == 1
    row = await _stored(sp)
    assert row.response_format == SCHEMA
    assert (row.status, row.turn_status, row.last_seq) == (SessionStatus.RUNNING, "running", 8), (
        "the PUT wrote its idle snapshot back over the turn that started after its read"
    )
    assert _park_fields(row) == _park_fields(_row(**park)), "the PUT erased the park committed after its read"


@pytest.mark.parametrize("case", ["mid_turn", "invalid_schema", "clear"])
async def test_response_format_keeps_its_mid_turn_refusal_and_its_schema_validation(
    client, sp, monkeypatch, case: str,
) -> None:
    """Pin: a turn in flight is still 409 and an invalid schema still 422, both before any write; null clears."""
    if case == "mid_turn":
        before = await _seed(sp, status=SessionStatus.RUNNING, turn_status="running")
        body, expected = {"response_format": SCHEMA}, 409
    elif case == "invalid_schema":
        before = await _seed(sp)
        body, expected = {"response_format": {"type": 42}}, 422
    else:
        before = await _seed(sp, response_format=SCHEMA)
        body, expected = {"response_format": None}, 200
    writes = _wrap(monkeypatch, sp)

    r = await client.put(f"/v1/workspaces/{WID}/sessions/{SID}/response_format", json=body)

    assert r.status_code == expected, r.text
    row = await _stored(sp)
    if case == "clear":
        assert row.response_format is None
        assert len(writes.attempts) == 1
    else:
        assert writes.attempts == []
        assert row == before


# ---------------------------------------------------------------------------
# resume
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("park", [_ask_user_park, _tool_wait_park], ids=["flag_off_ask_user", "flag_on_tool_wait"])
async def test_resume_from_paused_with_a_resumable_park_still_resumes(
    app, client, sp, monkeypatch, enqueued, park,
) -> None:
    """N108. A PAUSED row whose park is ``resumable`` (``_pause_session`` dropped the lease so that /resume re-arms
    it) resumes: RUNNING, the pause flag cleared, the park untouched, armed once. The route reads no flag; what
    differs with the flag on is the park the row carries (a tool batch instead of a yield), so both are seeded."""
    resumable = park("resumable")
    await _seed(sp, status=SessionStatus.PAUSED, pause_requested=True, **resumable)
    writes = _wrap(monkeypatch, sp)

    r = await client.post(f"/v1/workspaces/{WID}/sessions/{SID}/resume")

    assert r.status_code == 200, r.text
    assert r.json()["status"] == "running"
    assert len(writes.attempts) == 1
    row = await _stored(sp)
    assert (row.status, row.pause_requested, row.started_at) == (SessionStatus.RUNNING, False, _STARTED)
    assert _park_fields(row) == _park_fields(_row(**resumable))
    assert enqueued == [SID]
    assert _upserted(app) == [(ClaimKind.SESSION, SID)]


async def test_resume_succeeds_when_the_hook_flips_the_park_between_get_and_write(
    app, client, sp, monkeypatch, enqueued,
) -> None:
    """N132. A PAUSED row parked on a tool batch; the batch's hook flips the park to ``resumable`` between the
    resume's read and its write. The resume lands with ONE write attempt (its fence names the status alone, so the
    flip does not reject it: a retry cannot hide a park term) and the flip survives it."""
    park = _tool_wait_park()
    await _seed(sp, status=SessionStatus.PAUSED, pause_requested=True, **park)
    writes = _wrap(monkeypatch, sp, _flip_to_resumable)

    r = await client.post(f"/v1/workspaces/{WID}/sessions/{SID}/resume")

    assert r.status_code == 200, r.text
    row = await _stored(sp)
    assert row.parked_status == "resumable", "the resume wrote its snapshot's 'parked' back over the hook's flip"
    assert len(writes.attempts) == 1, f"expected one write attempt, got {writes.attempts}"
    assert (row.status, row.pause_requested) == (SessionStatus.RUNNING, False)
    assert _park_fields(row) == {**_park_fields(_row(**park)), "parked_status": "resumable"}
    assert enqueued == [SID]
    assert _upserted(app) == [(ClaimKind.SESSION, SID)]


@pytest.mark.parametrize("case", ["cancelled", "resumed_elsewhere"])
async def test_resume_does_not_arm_the_lease_after_a_rejected_write(
    app, client, sp, monkeypatch, enqueued, case: str,
) -> None:
    """N133. A PAUSED row moves before the resume's write: a cancel ends it (409, the existing answer for an ENDED
    session) or a resume in another process has already started it (the idempotent no-op on the fresh row). The
    resume's write is rejected, it re-reads once, and it neither enqueues nor upserts the lease; the row keeps what
    the other writer wrote."""
    await _seed(sp, status=SessionStatus.PAUSED, pause_requested=True)
    if case == "cancelled":
        competing = _set(status=SessionStatus.ENDED, ended_reason="cancelled", ended_at=datetime.now(UTC))
    else:
        competing = _set(status=SessionStatus.RUNNING, pause_requested=False)
    writes = _wrap(monkeypatch, sp, competing)

    r = await client.post(f"/v1/workspaces/{WID}/sessions/{SID}/resume")

    row = await _stored(sp)
    assert enqueued == [], "the resume enqueued a session whose write did not land"
    assert _upserted(app) == [], "the resume armed the lease after a rejected write"
    assert len(writes.attempts) == 1
    if case == "cancelled":
        assert r.status_code == 409, r.text
        assert (row.status, row.ended_reason) == (SessionStatus.ENDED, "cancelled"), "the resume resurrected an ENDED row"
    else:
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "running"
        assert row.status == SessionStatus.RUNNING


@pytest.mark.parametrize("case", ["moved_once", "moved_twice"])
async def test_resume_retries_once_from_the_fresh_row_and_a_second_rejection_is_409(
    app, client, sp, monkeypatch, enqueued, case: str,
) -> None:
    """A rejection whose fresh row is still resumable (PAUSED -> WAITING) retries the write once from the fresh row
    and arms after it lands; a second rejection (WAITING -> PAUSED before the retry) is 409 and arms nothing."""
    await _seed(sp, status=SessionStatus.PAUSED, pause_requested=True)
    competing = [_set(status=SessionStatus.WAITING)]
    if case == "moved_twice":
        competing.append(_set(status=SessionStatus.PAUSED))
    writes = _wrap(monkeypatch, sp, *competing)

    r = await client.post(f"/v1/workspaces/{WID}/sessions/{SID}/resume")

    assert len(writes.attempts) == 2
    row = await _stored(sp)
    if case == "moved_once":
        assert r.status_code == 200, r.text
        assert (row.status, row.pause_requested) == (SessionStatus.RUNNING, False)
        assert enqueued == [SID]
        assert _upserted(app) == [(ClaimKind.SESSION, SID)]
    else:
        assert r.status_code == 409, r.text
        assert row.status == SessionStatus.PAUSED
        assert enqueued == []
        assert _upserted(app) == []


@pytest.mark.parametrize("park", [None, "parked", "resumable"])
async def test_resume_on_a_running_session_writes_nothing(
    app, client, sp, monkeypatch, enqueued, park: str | None,
) -> None:
    """Pin: a RUNNING row (idle between turns, parked, or woken) is the idempotent no-op: 200, no write, no
    enqueue, no lease upsert."""
    before = await _seed(sp, status=SessionStatus.RUNNING, **(_tool_wait_park(park) if park else {}))
    writes = _wrap(monkeypatch, sp)

    r = await client.post(f"/v1/workspaces/{WID}/sessions/{SID}/resume")

    assert r.status_code == 200, r.text
    assert r.json()["status"] == "running"
    assert writes.attempts == []
    assert await _stored(sp) == before
    assert enqueued == []
    assert _upserted(app) == []


@pytest.mark.parametrize("status, started_at", [
    (SessionStatus.CREATED, None), (SessionStatus.WAITING, _STARTED),
], ids=["created_unstarted", "waiting_started"])
async def test_resume_stamps_started_at_only_when_the_row_it_read_has_none(
    client, sp, status, started_at,
) -> None:
    """Pin: ``started_at`` is decided from the row resume read: stamped when unset, otherwise kept."""
    await _seed(sp, status=status, started_at=started_at)
    before = datetime.now(UTC)

    r = await client.post(f"/v1/workspaces/{WID}/sessions/{SID}/resume")

    assert r.status_code == 200, r.text
    row = await _stored(sp)
    assert row.status == SessionStatus.RUNNING
    if started_at is None:
        assert row.started_at is not None and before <= row.started_at <= datetime.now(UTC)
    else:
        assert row.started_at == started_at


# ---------------------------------------------------------------------------
# delete (no behaviour change: the N109 regression pin)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", ["running", "parked", "idle"])
async def test_delete_refuses_a_running_session(client, sp, monkeypatch, case: str) -> None:
    """N109. A non-force DELETE of a RUNNING session (a turn in flight, or parked: a parked row is RUNNING) is 409
    and leaves the row exactly as it was; an idle session is still deleted."""
    if case == "running":
        before = await _seed(sp, status=SessionStatus.RUNNING, turn_status="running")
    elif case == "parked":
        before = await _seed(sp, status=SessionStatus.RUNNING, **_tool_wait_park())
    else:
        before = await _seed(sp)
    writes = _wrap(monkeypatch, sp)

    r = await client.delete(f"/v1/workspaces/{WID}/sessions/{SID}")

    if case == "idle":
        assert r.status_code == 204, r.text
        assert await _stored(sp) is None
    else:
        assert r.status_code == 409, r.text
        assert writes.attempts == []
        assert await _stored(sp) == before

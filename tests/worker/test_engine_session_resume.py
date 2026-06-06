"""Task 4.1 - resume parked sessions on the engine dispatch path.

``WorkerPool._run_engine_session`` loads the session row before
dispatching and, when ``parked_status == 'resumable'``, runs the
engine-native resume branch instead of a normal turn. This branch
(``_resume_engine_session`` / ``_resume_graph_engine`` / ``_end_session``)
returns a :class:`ReleaseOutcome` rather than touching the scheduler:

  * agent success  -> ReleaseOutcome(success=True, drop_lease=False):
    the SessionClaimAdapter.on_release clears the park columns + bumps
    turn_no, and the lease is kept so the continuation LLM turn runs on
    the next claim.
  * fail-closed     -> ENDED status written directly to the row +
    ReleaseOutcome(success=True, drop_lease=True): the lease is dropped
    so the ended session is not re-claimed.

These tests drive ``_run_engine_session`` through a real
InMemoryClaimEngine wired with the real SessionClaimAdapter (so the
on_release park-clear / turn-bump book-keeping is exercised), the real
fake storage provider, and a recording fake agent executor. The real
``ask_user`` resume hook is used (imported below for its
register_resume_hook side-effect).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from primer.claim.adapters.sessions import SessionClaimAdapter
from primer.claim.in_memory import InMemoryClaimEngine
from primer.int.claim import ClaimKind
from primer.model.chat import Message, ToolCallPart, ToolResultPart
from primer.model.scheduler import WorkerConfig
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)
from primer.model.yield_ import Yielded
from primer.worker.pool import WorkerPool
from primer.worker.yield_runtime import ParkedState

from tests.conftest import _FakeStorageProvider

# Register the ask_user resume hook (register_resume_hook("ask_user", ...)
# runs at module import). Without this the resume branch raises
# "no resume hook registered for 'ask_user'".
import primer.toolset.misc  # noqa: F401,E402


async def _async_return(value):
    return value


class _RecordingExecutor:
    """Stand-in for WorkspaceAgentExecutor exposing the resume-path
    surface (``inject_resume_messages``) + a ``_tool_manager`` slot."""

    def __init__(self, *, tool_manager=None):
        self._tool_manager = tool_manager
        self.injected: list[list[Message]] = []

    async def inject_resume_messages(self, messages):
        self.injected.append(list(messages))


class _NoopPersist:
    pass


def _build_engine(session_storage) -> InMemoryClaimEngine:
    return InMemoryClaimEngine(
        adapters={
            ClaimKind.SESSION: SessionClaimAdapter(session_storage=session_storage),
        },
    )


def _build_pool(storage, engine) -> WorkerPool:
    pool = WorkerPool(
        config=WorkerConfig(concurrency=1),
        scheduler=None,                  # type: ignore[arg-type]
        storage=storage,
        workspace_registry=None,         # type: ignore[arg-type]
        provider_registry=None,          # type: ignore[arg-type]
        engine=engine,
    )
    pool._worker_id = "wrk-engine-resume"
    return pool


def _make_resumable_session(
    sid: str,
    *,
    tool_name: str,
    tool_call_id: str,
    resume_event_payload: dict | None,
    parked_state_blob: dict | None = None,
    llm_messages: list | None = None,
    turn_no: int = 0,
) -> WorkspaceSession:
    """Build a WorkspaceSession row pre-stamped with a resumable park,
    mirroring what the listener + mark_resumable leave behind."""
    parked_at = datetime.now(timezone.utc) - timedelta(seconds=5)
    if parked_state_blob is None:
        stamped_rm = {"parked_at_iso": parked_at.isoformat()}
        yielded = Yielded(
            tool_name=tool_name,
            event_key=f"ask_user:{sid}:{tool_call_id}",
            timeout=600.0,
            resume_metadata=stamped_rm,
        )
        parked_state = ParkedState(
            yielded=yielded,
            llm_messages=llm_messages or [],
            turn_no=turn_no,
            started_at=parked_at,
            tool_call_id=tool_call_id,
            resume_event_payload=resume_event_payload,
        )
        parked_state_blob = parked_state.to_jsonable()

    return WorkspaceSession(
        id=sid,
        workspace_id=f"ws-{sid}",
        binding=AgentSessionBinding(kind="agent", agent_id="ag-1"),
        status=SessionStatus.RUNNING,
        created_at=datetime.now(timezone.utc),
        turn_no=turn_no,
        parked_status="resumable",
        parked_event_key=f"ask_user:{sid}:{tool_call_id}",
        parked_until=parked_at + timedelta(seconds=600),
        parked_at=parked_at,
        parked_state=parked_state_blob,
    )


async def _claim_session(engine, sid: str):
    """Re-arm + claim the lease for ``sid`` and return the ClaimLease."""
    await engine.mark_resumable(ClaimKind.SESSION, sid)
    leases = await engine.claim_due("wrk-engine-resume", max_count=10)
    for lease in leases:
        if lease.kind == ClaimKind.SESSION and lease.entity_id == sid:
            return lease
    raise AssertionError(f"no claimable lease for session {sid!r}")


@pytest.mark.asyncio
async def test_resume_agent_clears_park_injects_and_keeps_lease(monkeypatch):
    """A resumable agent-bound session parked on ask_user: the engine
    resume branch runs the hook, injects [assistant, tool_result], and
    returns drop_lease=False. The SessionClaimAdapter.on_release then
    clears the park columns + bumps turn_no, and the lease survives so
    the continuation turn can be claimed next."""
    sid = "sess-engine-resume-ok"
    tool_call_id = "tc-ask-1"

    assistant_msg = Message(
        role="assistant",
        parts=[
            ToolCallPart(
                id=tool_call_id,
                name="_misc__ask_user",
                arguments={"prompt": "What is your name?"},
            ),
        ],
    )

    storage_provider = _FakeStorageProvider()
    session_storage = storage_provider.get_storage(WorkspaceSession)
    engine = _build_engine(session_storage)
    pool = _build_pool(storage_provider, engine)

    sess = _make_resumable_session(
        sid,
        tool_name="ask_user",
        tool_call_id=tool_call_id,
        resume_event_payload={"response": "Alice"},
        llm_messages=[assistant_msg.model_dump(mode="json")],
    )
    await session_storage.create(sess)

    fake_executor = _RecordingExecutor()
    monkeypatch.setattr(
        pool, "_load_workspace_for_persist",
        lambda _ws_id: _async_return(_NoopPersist()),
    )
    monkeypatch.setattr(
        pool, "_build_agent_executor",
        lambda _s, _w: _async_return(fake_executor),
    )

    lease = await _claim_session(engine, sid)
    await pool._run_engine_session(lease)

    # 1. inject_resume_messages was called with a non-empty list ending
    #    in a tool-role message carrying the resumed result.
    assert len(fake_executor.injected) == 1
    injected = fake_executor.injected[0]
    assert len(injected) >= 1
    assert injected[-1].role == "tool"
    tool_part = next(
        p for p in injected[-1].parts if isinstance(p, ToolResultPart)
    )
    assert tool_part.id == tool_call_id
    assert tool_part.error is False

    # 2. on_release cleared the park columns + bumped turn_no.
    row = await session_storage.get(sid)
    assert row is not None
    assert row.parked_status is None
    assert row.parked_state is None
    assert row.parked_event_key is None
    assert row.turn_no == sess.turn_no + 1

    # 3. The session is NOT ended.
    assert row.status != SessionStatus.ENDED

    # 4. drop_lease was False -> a claimable lease still exists for the
    #    continuation turn.
    leases = await engine.claim_due("wrk-engine-resume", max_count=10)
    assert any(
        l.kind == ClaimKind.SESSION and l.entity_id == sid for l in leases
    ), "continuation lease must survive (drop_lease=False)"


@pytest.mark.asyncio
async def test_resume_malformed_parked_state_ends_failed(monkeypatch):
    """A resumable session whose parked_state is malformed
    (unknown schema_version) cannot be rehydrated: the resume branch
    fails closed -> writes ENDED/failed to the row and drops the lease."""
    sid = "sess-engine-resume-malformed"

    storage_provider = _FakeStorageProvider()
    session_storage = storage_provider.get_storage(WorkspaceSession)
    engine = _build_engine(session_storage)
    pool = _build_pool(storage_provider, engine)

    sess = _make_resumable_session(
        sid,
        tool_name="ask_user",
        tool_call_id="tc-x",
        resume_event_payload=None,
        # Unknown schema_version -> ParkedState.from_jsonable raises.
        parked_state_blob={"schema_version": 999},
    )
    await session_storage.create(sess)

    # The executor must NOT be built on the malformed path.
    def _fail(*a, **kw):
        raise AssertionError("executor must not be built for malformed park")

    monkeypatch.setattr(pool, "_build_agent_executor", _fail)
    monkeypatch.setattr(
        pool, "_load_workspace_for_persist",
        lambda _ws_id: _async_return(_NoopPersist()),
    )

    lease = await _claim_session(engine, sid)
    await pool._run_engine_session(lease)

    row = await session_storage.get(sid)
    assert row is not None
    assert row.status == SessionStatus.ENDED
    assert row.ended_reason == "failed"
    assert row.ended_at is not None
    # on_release (success=True, non-park) clears the park columns.
    assert row.parked_status is None

    # drop_lease was True -> no claimable lease remains.
    leases = await engine.claim_due("wrk-engine-resume", max_count=10)
    assert not any(
        l.kind == ClaimKind.SESSION and l.entity_id == sid for l in leases
    ), "ended session must not have a surviving lease (drop_lease=True)"

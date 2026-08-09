"""External tool call round trip on the engine resume path.

The API-side halves of the journey are covered elsewhere (provider
dispatch parks + writes the row: tests/agent/test_external_tools.py;
steer validates/wakes: tests/api/test_external_tools_steer.py). This
module proves the worker half: a session parked on ``_external`` whose
wake payload is the invoker's result resumes through the registered
hook, injects the paired tool_result the continuation turn will see,
and clears the park. The cancelled-marker payload variant proves the
synthetic superseded result reaches the transcript the same way.
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
from primer.worker.yield_runtime import ParkedState, make_cancelled_payload

from tests.conftest import _FakeStorageProvider

# Registers the "_external" resume hook at import time (the production
# worker gets it via primer.worker.session_resume_coordinator's import).
import primer.agent.external_tools  # noqa: F401,E402


async def _async_return(value):
    return value


class _RecordingExecutor:
    def __init__(self):
        self._tool_manager = None
        self.injected: list[list[Message]] = []

    async def inject_resume_messages(self, messages):
        self.injected.append(list(messages))


class _NoopPersist:
    pass


def _build_pool(storage, engine) -> WorkerPool:
    pool = WorkerPool(
        config=WorkerConfig(concurrency=1),
        scheduler=None,                  # type: ignore[arg-type]
        storage=storage,
        workspace_registry=None,         # type: ignore[arg-type]
        provider_registry=None,          # type: ignore[arg-type]
        engine=engine,
    )
    pool._worker_id = "wrk-external-roundtrip"
    return pool


def _make_resumable_external_session(
    sid: str, *, tool_call_id: str, resume_event_payload: dict,
) -> WorkspaceSession:
    parked_at = datetime.now(timezone.utc) - timedelta(seconds=5)
    ek = f"external_tool:{sid}:{tool_call_id}"
    assistant_msg = Message(
        role="assistant",
        parts=[
            ToolCallPart(
                id=tool_call_id,
                name="external__lookup_customer",
                arguments={"id": "c1"},
            ),
        ],
    )
    yielded = Yielded(
        tool_name="_external",
        event_key=ek,
        timeout=600.0,
        resume_metadata={
            "original_call": {
                "id": tool_call_id,
                "name": "lookup_customer",
                "arguments": {"id": "c1"},
            },
            "external_call_row_id": "etool-rt-1",
            "parked_at_iso": parked_at.isoformat(),
        },
    )
    parked_state = ParkedState(
        yielded=yielded,
        llm_messages=[assistant_msg.model_dump(mode="json")],
        turn_no=1,
        started_at=parked_at,
        tool_call_id=tool_call_id,
        resume_event_payload=resume_event_payload,
    )
    return WorkspaceSession(
        id=sid,
        workspace_id=f"ws-{sid}",
        binding=AgentSessionBinding(kind="agent", agent_id="ag-1"),
        status=SessionStatus.RUNNING,
        created_at=datetime.now(timezone.utc),
        turn_no=1,
        parked_status="resumable",
        parked_event_key=ek,
        parked_until=parked_at + timedelta(seconds=600),
        parked_at=parked_at,
        parked_state=parked_state.to_jsonable(),
    )


def _build_engine(session_storage) -> InMemoryClaimEngine:
    return InMemoryClaimEngine(
        adapters={
            ClaimKind.SESSION: SessionClaimAdapter(
                session_storage=session_storage
            ),
        },
    )


async def _claim_session(engine, sid: str):
    await engine.mark_resumable(ClaimKind.SESSION, sid)
    leases = await engine.claim_due("wrk-external-roundtrip", max_count=10)
    for lease in leases:
        if lease.kind == ClaimKind.SESSION and lease.entity_id == sid:
            return lease
    raise AssertionError(f"no claimable lease for session {sid!r}")


async def _run_resume(sid: str, resume_event_payload: dict, monkeypatch):
    storage_provider = _FakeStorageProvider()
    session_storage = storage_provider.get_storage(WorkspaceSession)
    engine = _build_engine(session_storage)
    pool = _build_pool(storage_provider, engine)

    sess = _make_resumable_external_session(
        sid, tool_call_id="tc-rt-1", resume_event_payload=resume_event_payload,
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
    return fake_executor, session_storage


@pytest.mark.asyncio
async def test_external_result_resumes_and_pairs_transcript(monkeypatch):
    fake_executor, session_storage = await _run_resume(
        "sess-ext-rt-ok",
        {"result": {"name": "Ada"}, "is_error": False},
        monkeypatch,
    )

    assert len(fake_executor.injected) == 1
    injected = fake_executor.injected[0]
    assert injected[-1].role == "tool"
    tool_part = next(
        p for p in injected[-1].parts if isinstance(p, ToolResultPart)
    )
    assert tool_part.id == "tc-rt-1"
    assert tool_part.error is False
    assert "Ada" in tool_part.output

    row = await session_storage.get("sess-ext-rt-ok")
    assert row.parked_status is None
    assert row.parked_state is None
    assert row.turn_no == 2
    assert row.status != SessionStatus.ENDED


@pytest.mark.asyncio
async def test_cancelled_marker_resumes_with_synthetic_result(monkeypatch):
    fake_executor, session_storage = await _run_resume(
        "sess-ext-rt-cancel",
        make_cancelled_payload(reason="superseded by new user message"),
        monkeypatch,
    )

    injected = fake_executor.injected[0]
    tool_part = next(
        p for p in injected[-1].parts if isinstance(p, ToolResultPart)
    )
    assert tool_part.error is True
    assert '"cancelled": true' in tool_part.output
    assert "superseded" in tool_part.output

    row = await session_storage.get("sess-ext-rt-cancel")
    assert row.parked_status is None

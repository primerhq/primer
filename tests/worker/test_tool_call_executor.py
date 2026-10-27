"""Red probes for the TOOL_CALL claim executor (BPF wave 1, spec: A2 analysis section 6(a)).

The flagship C4 defect: with ``tool_calls_as_claims_enabled=True`` a parked tool batch
writes independently-claimable ``ToolCallTask`` rows + ``TOOL_CALL`` leases, but the pool
registers no TOOL_CALL handler in ``_dispatch``, so the reserved tool slice never claims
them (its own ``TOOL_CALL in self._dispatch`` guard) and the session's park resolves only
by the hour-long timeout, ending the session FAILED. That pre-fix red was captured live
against a real LLM (S4: park -> 1 h -> FAILED, /var/tmp/revamp/bulletproof/p3/leg1-summary.md);
that capture is probe (iv) of the build brief and is NOT re-run here (no e2e in this PR).

The three probes below are red on main for the same root cause: the lease is never
claimed, so (i) the task never runs, (ii) an orphan task is never voided, (iii) a
poisoned task never reaches its terminal retry cap.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from primer.claim.adapters.tool_calls import ToolCallClaimAdapter
from primer.claim.in_memory import InMemoryClaimEngine
from primer.int.claim import CLAIM_PRIORITY_RESUME, ClaimKind
from primer.int.toolset import ToolsetProvider
from primer.model.agent import Agent, AgentModel
from primer.model.chat import Tool, ToolCallResult
from primer.model.scheduler import WorkerConfig
from primer.model.tool_call_task import ToolCallTask, ToolCallTaskState
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionMessageKind,
    SessionMessageRecord,
    SessionStatus,
    WorkspaceSession,
)
from primer.scheduler.in_memory import InMemoryScheduler
from primer.session.yields import durably_mark_session_resumable, tool_wait_event_key
from primer.worker.pool import WorkerPool
from primer.worker.yield_runtime import ToolWaitParkedState
from tests.conftest import _InMemoryStorage

TOOLSET_ID = "testtools"
SESSION_ID = "sess-exec"
SCOPED = "x:tool:0:1"
TASK_ID = f"{SESSION_ID}/{SCOPED}"
CALL_ID = "call-raw-1"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class FakeToolset(ToolsetProvider):
    """One in-process toolset: ``do_thing`` succeeds, ``boom`` always raises."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def list_tools(self, *, principal: str | None = None):
        for name in ("do_thing", "boom"):
            yield Tool(
                id=name,
                description=f"the {name} test tool",
                toolset_id=TOOLSET_ID,
                args_schema={"type": "object", "properties": {}},
            )

    async def call(
        self,
        *,
        tool_name: str,
        arguments: dict,
        principal: str | None = None,
        ctx: object | None = None,
    ) -> ToolCallResult:
        self.calls.append(tool_name)
        if tool_name == "boom":
            raise RuntimeError("the boom tool failed")
        return ToolCallResult(output=f"ok-from-{tool_name}", is_error=False)


class FakeAgentSession:
    """The on-disk AgentSession slot the builder looks up by session id."""

    def __init__(self, session_id: str) -> None:
        self.workspace_id = "ws-1"
        self.session_id = session_id
        self.workspace_tools: list = []


class FakeWorkspace:
    """A workspace whose messages.jsonl holds the batch's TOOL_CALL record."""

    id = "ws-1"
    state_path = ".state"

    def __init__(self, records_by_session: dict[str, list[SessionMessageRecord]]) -> None:
        self._records = records_by_session

    async def get_session(self, session_id: str) -> FakeAgentSession:
        return FakeAgentSession(session_id)

    async def read_file(self, path: str) -> bytes:
        # .state/sessions/<sid>/messages.jsonl
        parts = path.split("/")
        if len(parts) == 4 and parts[0] == self.state_path and parts[1] == "sessions":
            sid = parts[2]
            if sid in self._records:
                return "\n".join(
                    json.dumps(r.model_dump(mode="json")) for r in self._records[sid]
                ).encode("utf-8")
        from primer.model.except_ import NotFoundError

        raise NotFoundError(f"no such file {path!r}")


class FakeWorkspaceRegistry:
    def __init__(self, workspace: FakeWorkspace) -> None:
        self._workspace = workspace

    async def get_workspace(self, workspace_id: str) -> FakeWorkspace | None:
        return self._workspace if workspace_id == "ws-1" else None


class FakeProviderRegistry:
    def __init__(self, toolset: FakeToolset) -> None:
        self._toolset = toolset

    async def get_toolset(self, toolset_id: str):
        return self._toolset if toolset_id == TOOLSET_ID else None


class FakeStorageProvider:
    """One ``_InMemoryStorage`` per model class, behind the StorageProvider seam."""

    def __init__(self) -> None:
        self._by_cls: dict = {}

    def get_storage(self, model_cls):
        return self._by_cls.setdefault(model_cls, _InMemoryStorage(model_cls))


def _tool_call_record() -> SessionMessageRecord:
    return SessionMessageRecord(
        seq=1,
        kind=SessionMessageKind.TOOL_CALL,
        created_at=_now(),
        payload={
            "id": SCOPED,
            "name": f"{TOOLSET_ID}__do_thing",
            "arguments": {"n": 7},
        },
    )


def _parked_session(
    workspace_session_cls_rows: dict, *, outstanding_task_ids: list[str],
) -> WorkspaceSession:
    wake_key = tool_wait_event_key(SESSION_ID, scoped_task_id=outstanding_task_ids[0])
    parked_state = ToolWaitParkedState(
        outstanding_task_ids=outstanding_task_ids,
        notifying_task_ids=[],
        event_key=wake_key,
        llm_messages=[],
        turn_no=0,
        started_at=_now(),
    ).to_jsonable()
    session = WorkspaceSession(
        id=SESSION_ID,
        workspace_id="ws-1",
        binding=AgentSessionBinding(
            agent_id="agent-1",
            agent_snapshot=Agent(
                model=AgentModel(profile_id="profile-1"),
                description="the probe agent",
                tools=[f"{TOOLSET_ID}__do_thing", f"{TOOLSET_ID}__boom"],
            ),
        ),
        binding_epoch=1,
        status=SessionStatus.RUNNING,
        created_at=_now(),
        turn_no=0,
        parked_status="parked",
        parked_state=parked_state,
        parked_event_key=wake_key,
        parked_at=_now(),
    )
    workspace_session_cls_rows[session.id] = session
    return session


async def _build_pool(
    *,
    outstanding_task_ids: list[str],
    config: WorkerConfig | None = None,
):
    """A flag-on pool whose reserved tool slice could claim ``TASK_ID``.

    Returns ``(pool, engine, provider, toolset, session_rows, task_rows)``. The engine
    carries the real ToolCallClaimAdapter and the factory's own wake hook (last sibling
    terminal -> durably_mark_session_resumable), exactly as the claim factory wires it.
    """
    provider = FakeStorageProvider()
    toolset = FakeToolset()
    workspace = FakeWorkspace({SESSION_ID: [_tool_call_record()]})
    registry = FakeWorkspaceRegistry(workspace)
    provider_registry = FakeProviderRegistry(toolset)

    session_rows: dict = {}
    session = _parked_session(session_rows, outstanding_task_ids=outstanding_task_ids)
    session_storage = provider.get_storage(WorkspaceSession)
    await session_storage.create(session)

    task_rows: dict = {}

    def _task(task_id: str, *, tool: str = "do_thing") -> ToolCallTask:
        task = ToolCallTask(
            id=task_id,
            session_id=SESSION_ID,
            turn_no=0,
            tool_name=f"{TOOLSET_ID}__{tool}",
            state=ToolCallTaskState.QUEUED,
            record_seq=1,
            call_id=CALL_ID,
            batch_task_ids=outstanding_task_ids,
            created_at=_now(),
        )
        task_rows[task.id] = task
        return task

    task_storage = provider.get_storage(ToolCallTask)
    await task_storage.create(_task(TASK_ID))

    engine = InMemoryClaimEngine(adapters={
        ClaimKind.TOOL_CALL: ToolCallClaimAdapter(task_storage=task_storage),
    })

    async def _wake_session_on_tool_wait_ready(signal) -> None:
        row = await session_storage.get(signal.session_id)
        if row is None:
            return
        await durably_mark_session_resumable(
            row,
            event_key=signal.event_key,
            payload=signal.payload,
            session_storage=session_storage,
            engine=engine,
        )

    engine.bind_post_release_hook(_wake_session_on_tool_wait_ready)
    await engine.upsert(ClaimKind.TOOL_CALL, TASK_ID, priority=CLAIM_PRIORITY_RESUME)

    scheduler = InMemoryScheduler()
    await scheduler.initialize()
    if config is None:
        config = WorkerConfig(
            concurrency=2,
            heartbeat_interval_seconds=1,
            lease_ttl_seconds=5,
            poll_interval_seconds=0.1,
            drain_timeout_seconds=3,
            tool_calls_as_claims_enabled=True,
        )
    pool = WorkerPool(
        config=config,
        scheduler=scheduler,
        storage=provider,  # type: ignore[arg-type]
        workspace_registry=registry,  # type: ignore[arg-type]
        provider_registry=provider_registry,  # type: ignore[arg-type]
        engine=engine,
    )

    async def _fake_resolve_llm(agent, override_profile_id=None):
        return object(), object()

    # The documented resolution seam (executor_builders.resolve_llm_and_model routes
    # through pool._resolve_llm precisely so a test can steer it).
    pool._resolve_llm = _fake_resolve_llm  # type: ignore[method-assign]
    return pool, engine, provider, toolset, session_rows, task_rows, scheduler


async def _until(predicate, message: str, timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, message
        await asyncio.sleep(0.02)


# ---- probe (i): the flagship C4 case ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_flag_on_pool_claims_runs_and_wakes_a_parked_tool_call():
    """A real park with a claimable tool: the pool CLAIMS the TOOL_CALL lease, RUNS the
    tool through the session's own tool surface, lands the DONE row with its
    result_state, and the last-sibling wake flips the session resumable. On main the
    reserved tool slice claims nothing (no handler registered), the task stays QUEUED
    and the session's park can only time out (C4)."""
    pool, engine, provider, toolset, session_rows, task_rows, scheduler = await _build_pool(
        outstanding_task_ids=[TASK_ID],
    )
    try:
        await pool.start()
        task_storage = provider.get_storage(ToolCallTask)
        session_storage = provider.get_storage(WorkspaceSession)
        await _until(
            lambda: task_storage._data[TASK_ID].state is ToolCallTaskState.DONE,  # noqa: SLF001
            "the claimed tool call never reached DONE (the lease was never claimed or run)",
        )
        task = task_storage._data[TASK_ID]  # noqa: SLF001
        assert toolset.calls == ["do_thing"], f"the tool did not run exactly once: {toolset.calls}"
        assert task.result_state is not None, "the DONE row carries no result_state"
        assert task.result_state.get("output") == "ok-from-do_thing"
        assert task.finished_at is not None
        assert (ClaimKind.TOOL_CALL, TASK_ID) not in engine._leases, "the lease was not dropped"
        await _until(
            lambda: session_storage._data[SESSION_ID].parked_status == "resumable",  # noqa: SLF001
            "the last-sibling wake never re-armed the session's park",
        )
    finally:
        await pool.drain_and_stop(timeout=3)
        await scheduler.aclose()


# ---- probe (ii): orphan liveness ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_task_not_referenced_by_the_live_park_is_voided_never_executed():
    """A task whose id the owning session's live tool_wait parked_state no longer
    references (the non-byte-identical crash-retry orphan, D-A2-06) is voided: terminal
    FAILED without running the tool, lease dropped, never QUEUED again. On main nothing
    claims the lease, so the orphan row sits QUEUED indefinitely."""
    other_task_id = f"{SESSION_ID}/x:tool:9:1"
    pool, engine, provider, toolset, session_rows, task_rows, scheduler = await _build_pool(
        # The session parked on a LATER turn's batch: TASK_ID is not in it.
        outstanding_task_ids=[other_task_id],
    )
    try:
        await pool.start()
        task_storage = provider.get_storage(ToolCallTask)
        await _until(
            lambda: task_storage._data[TASK_ID].state in (
                ToolCallTaskState.DONE, ToolCallTaskState.FAILED,
            ),
            "the orphan task never reached a terminal state",
        )
        task = task_storage._data[TASK_ID]  # noqa: SLF001
        assert task.state is ToolCallTaskState.FAILED, "an orphan is voided FAILED, never DONE"
        assert task.last_error, "the void must record why the task never ran"
        assert toolset.calls == [], f"the orphan's tool must never run: {toolset.calls}"
        assert (ClaimKind.TOOL_CALL, TASK_ID) not in engine._leases, "the orphan lease must drop"
    finally:
        await pool.drain_and_stop(timeout=3)
        await scheduler.aclose()


# ---- probe (iii): the poison cap (D-A2-03) ------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_poisoned_tool_call_reaches_terminal_failed_with_a_synthesized_result_state():
    """A tool that always raises must stop requeueing: at attempt_count == max_attempts
    the task goes terminal FAILED carrying a synthesized error result_state (the resume
    coordinator hands the model a tool_result for its tool_use instead of wedging).
    On main nothing claims the lease, so no attempt count ever advances."""
    config = WorkerConfig(
        concurrency=2,
        heartbeat_interval_seconds=1,
        lease_ttl_seconds=5,
        poll_interval_seconds=0.1,
        drain_timeout_seconds=3,
        tool_calls_as_claims_enabled=True,
        max_attempts=2,
        base_backoff_seconds=0.1,
        max_backoff_seconds=1.0,
    )
    pool, engine, provider, toolset, session_rows, task_rows, scheduler = await _build_pool(
        outstanding_task_ids=[TASK_ID],
        config=config,
    )
    # Swap the task's tool for the one that always raises (the row's tool_name is what
    # the rehydrated ToolCallPart carries).
    task_storage = provider.get_storage(ToolCallTask)
    task_storage._data[TASK_ID] = ToolCallTask(  # noqa: SLF001
        id=TASK_ID,
        session_id=SESSION_ID,
        turn_no=0,
        tool_name=f"{TOOLSET_ID}__boom",
        state=ToolCallTaskState.QUEUED,
        record_seq=1,
        call_id=CALL_ID,
        batch_task_ids=[TASK_ID],
        created_at=_now(),
    )
    try:
        await pool.start()
        await _until(
            lambda: task_storage._data[TASK_ID].state in (
                ToolCallTaskState.DONE, ToolCallTaskState.FAILED,
            ),
            "the poisoned task never reached a terminal state",
        )
        task = task_storage._data[TASK_ID]  # noqa: SLF001
        assert task.state is ToolCallTaskState.FAILED
        assert toolset.calls == ["boom", "boom"], (
            f"the tool ran {len(toolset.calls)} times; it must run exactly max_attempts "
            f"({config.max_attempts}) times before the cap"
        )
        assert task.attempts >= config.max_attempts, "the row's unclean-execution count did not advance"
        assert task.result_state is not None, "the poison must write a synthesized result_state"
        assert task.result_state.get("error") is True
        assert task.result_state.get("id") == CALL_ID, "the synthesized part carries the wire call id"
        assert task.last_error
        assert (ClaimKind.TOOL_CALL, TASK_ID) not in engine._leases, "the poisoned lease must drop"
    finally:
        await pool.drain_and_stop(timeout=3)
        await scheduler.aclose()

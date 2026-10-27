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
from primer.int.claim import CLAIM_PRIORITY_RESUME, ClaimAdapter, ClaimKind
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


class _NeverClaimedSessionAdapter(ClaimAdapter):
    """The SESSION lease the last-sibling wake re-arms is never claimed in these
    probes: the probes assert the wake's own durable flip (its hook return), not
    the resumed turn (the resume coordinator has its own test coverage)."""

    entity_table = "sessions"

    def eligibility_sql(self) -> str:
        return "false"

    async def is_eligible(self, entity_id: str, now: object) -> bool:
        return False

    async def on_release(self, conn, entity_id: str, *, outcome) -> None:
        return None


class FakeToolset(ToolsetProvider):
    """One in-process toolset: ``do_thing`` succeeds, ``boom`` always raises."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def required_role(self, tool_name: str) -> str:
        # The ABC default is "admin" (fail-closed for MCP proxies); these are
        # ordinary user tools, so the unattributed/user-rank test principal clears
        # the floor.
        return "user"

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
        self.system_prompt_fragment = ""


class FakeWorkspace:
    """A workspace whose messages.jsonl holds the batch's TOOL_CALL record."""

    id = "ws-1"
    state_path = ".state"

    def __init__(self, records_by_session: dict[str, list[SessionMessageRecord]]) -> None:
        self._records = records_by_session

    async def get_session(self, session_id: str) -> FakeAgentSession:
        return FakeAgentSession(session_id)

    async def file_info(self, path: str) -> None:
        from primer.model.except_ import NotFoundError

        raise NotFoundError(f"no such file {path!r}")

    async def read_file(self, path: str) -> bytes:
        # .state/sessions/<sid>/messages.jsonl
        parts = path.split("/")
        if len(parts) == 4 and parts[0] == self.state_path and parts[1] == "sessions":
            sid = parts[2]
            if sid in self._records:
                # Every record line is newline-terminated (the writer's own
                # shape): _complete_lines treats a dangling final line as
                # in-flight, so the last record must carry its terminator.
                return (
                    "\n".join(
                        json.dumps(r.model_dump(mode="json"))
                        for r in self._records[sid]
                    )
                    + "\n"
                ).encode("utf-8")
        from primer.model.except_ import NotFoundError

        raise NotFoundError(f"no such file {path!r}")


class FakeWorkspaceRegistry:
    def __init__(self, workspace: FakeWorkspace) -> None:
        self._workspace = workspace

    async def get_workspace(self, workspace_id: str) -> FakeWorkspace | None:
        return self._workspace if workspace_id == "ws-1" else None

    async def check_workspace_allowed(self, *args, **kwargs):
        return True


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


def _tool_call_record(tool: str = "do_thing") -> SessionMessageRecord:
    return SessionMessageRecord(
        seq=1,
        kind=SessionMessageKind.TOOL_CALL,
        created_at=_now(),
        payload={
            "id": SCOPED,
            "name": f"{TOOLSET_ID}__{tool}",
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
    tool: str = "do_thing",
):
    """A flag-on pool whose reserved tool slice could claim ``TASK_ID``.

    ``tool`` names the tool both the durable TOOL_CALL record and the task row
    carry (the record is the rehydration source of truth; the row's own
    tool_name is a denormalized copy of it). Returns
    ``(pool, engine, provider, toolset, session_rows, task_rows, scheduler,
    wake_calls)``. The engine carries the real ToolCallClaimAdapter and the
    factory's own wake hook (last sibling terminal ->
    durably_mark_session_resumable), exactly as the claim factory wires it.
    """
    provider = FakeStorageProvider()
    toolset = FakeToolset()
    workspace = FakeWorkspace({SESSION_ID: [_tool_call_record(tool)]})
    registry = FakeWorkspaceRegistry(workspace)
    provider_registry = FakeProviderRegistry(toolset)

    session_rows: dict = {}
    session = _parked_session(session_rows, outstanding_task_ids=outstanding_task_ids)
    session_storage = provider.get_storage(WorkspaceSession)
    await session_storage.create(session)

    task_rows: dict = {}

    def _task(task_id: str) -> ToolCallTask:
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
        ClaimKind.SESSION: _NeverClaimedSessionAdapter(),
    })
    wake_calls: list = []

    async def _wake_session_on_tool_wait_ready(signal) -> None:
        row = await session_storage.get(signal.session_id)
        if row is None:
            wake_calls.append((signal, False))
            return
        flipped = await durably_mark_session_resumable(
            row,
            event_key=signal.event_key,
            payload=signal.payload,
            session_storage=session_storage,
            engine=engine,
        )
        wake_calls.append((signal, flipped))

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
    return pool, engine, provider, toolset, session_rows, task_rows, scheduler, wake_calls


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
    pool, engine, provider, toolset, session_rows, task_rows, scheduler, wake_calls = await _build_pool(
        outstanding_task_ids=[TASK_ID],
    )
    try:
        await pool.start()
        task_storage = provider.get_storage(ToolCallTask)
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
        # The last-sibling wake fired through the engine's post-release hook
        # (the factory's own wake) and durably flipped the session's park to
        # resumable. The pool's own session claim may then start the resumed
        # turn and clear the park - the flip's own return is the wake's proof.
        await _until(
            lambda: len(wake_calls) == 1,
            "the last-sibling wake never fired (the engine post-release hook was not called)",
        )
        signal, flipped = wake_calls[0]
        assert signal.session_id == SESSION_ID
        assert flipped, "the wake's parked->resumable flip was refused"
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
    pool, engine, provider, toolset, session_rows, task_rows, scheduler, _wake = await _build_pool(
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
    pool, engine, provider, toolset, session_rows, task_rows, scheduler, _wake = await _build_pool(
        outstanding_task_ids=[TASK_ID],
        config=config,
        tool="boom",
    )
    try:
        await pool.start()
        task_storage = provider.get_storage(ToolCallTask)
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


# ---- branch coverage (green-only; the three probes above are the red-first core) ----


class _GatingToolManager:
    """A tool manager whose execute hits the approval gate (YieldToWorker)."""

    def __init__(self) -> None:
        self.executed = 0

    async def execute(self, call, *, principal=None):
        self.executed += 1
        from primer.model.yield_ import YieldToWorker, Yielded

        raise YieldToWorker(
            Yielded(
                tool_name="_approval",
                event_key=f"tool_approval:{SESSION_ID}:{CALL_ID}",
                resume_metadata={"approval": "pending", "policy_id": "pol-1"},
            ),
            tool_call_id=CALL_ID,
        )


def _executor_with(tool_manager) -> object:
    import types

    return types.SimpleNamespace(
        _executor=types.SimpleNamespace(_tool_manager=tool_manager),
    )


@pytest.mark.asyncio
async def test_a_gated_tool_call_parks_the_task_not_the_session():
    """The approval/yield gate: the handler parks the TASK (GATED row, bumped
    gate_seq, cleared token, lease dropped) and leaves the session's park
    untouched - one gated call does not block its siblings, and the session is
    never woken by the gate itself (the decision side is the decisions slice)."""
    pool, engine, provider, toolset, session_rows, task_rows, scheduler, wake_calls = await _build_pool(
        outstanding_task_ids=[TASK_ID],
    )
    tool_manager = _GatingToolManager()

    async def _gate_executor(session, workspace):
        return _executor_with(tool_manager)

    pool._build_agent_executor = _gate_executor  # type: ignore[method-assign]
    try:
        await pool.start()
        task_storage = provider.get_storage(ToolCallTask)
        await _until(
            lambda: task_storage._data[TASK_ID].state is ToolCallTaskState.GATED,  # noqa: SLF001
            "the gated task never reached GATED",
        )
        task = task_storage._data[TASK_ID]  # noqa: SLF001
        assert tool_manager.executed == 1
        assert toolset.calls == [], "a gated call must not run the real tool"
        assert task.gate_seq == 1, "the gate branch bumps gate_seq"
        assert task.claim_token is None, "the gate branch clears the claim token"
        assert (ClaimKind.TOOL_CALL, TASK_ID) not in engine._leases, "the gated lease must drop"
        # The session's park is untouched. The post-release wake hook FIRES on
        # every tool-call release (it is how the last sibling's terminal release
        # re-arms the session), but the GATED outcome never flips the session:
        # one gated sibling does not un-park a batch whose other sibling is
        # still pending, and the gate itself wakes nothing.
        session = session_rows[SESSION_ID]
        assert session.parked_state is not None and TASK_ID in str(session.parked_state)
        for _signal, flipped in wake_calls:
            assert not flipped, "a GATED outcome must never flip the session resumable"
    finally:
        await pool.drain_and_stop(timeout=3)
        await scheduler.aclose()


@pytest.mark.asyncio
async def test_a_running_row_is_taken_over_with_a_fresh_token():
    """At-least-once: a row left RUNNING by a crashed holder (stale token) is
    re-claimed on takeover, the token is replaced, and the call still runs to DONE."""
    pool, engine, provider, toolset, session_rows, task_rows, scheduler, _wake = await _build_pool(
        outstanding_task_ids=[TASK_ID],
    )
    task_storage = provider.get_storage(ToolCallTask)
    row = task_storage._data[TASK_ID]  # noqa: SLF001
    row.state = ToolCallTaskState.RUNNING
    row.claim_token = "stale-token-from-crashed-holder"
    row.attempts = 1
    row.started_at = _now()
    try:
        await pool.start()
        await _until(
            lambda: task_storage._data[TASK_ID].state is ToolCallTaskState.DONE,  # noqa: SLF001
            "the taken-over row never reached DONE",
        )
        task = task_storage._data[TASK_ID]  # noqa: SLF001
        assert toolset.calls == ["do_thing"]
        assert task.result_state is not None
        assert task.claim_token != "stale-token-from-crashed-holder", (
            "the takeover must replace the crashed holder's token"
        )
        assert (ClaimKind.TOOL_CALL, TASK_ID) not in engine._leases
    finally:
        await pool.drain_and_stop(timeout=3)
        await scheduler.aclose()


@pytest.mark.asyncio
async def test_a_record_mismatch_fails_terminally_without_running_the_tool():
    """The 'record durable before task claimable' invariant broke upstream: the
    task's own TOOL_CALL record is missing (record_seq points past the stored
    record). Terminal FAILED, no requeue, no tool run - a known-bad row must not
    burn the retry budget."""
    pool, engine, provider, toolset, session_rows, task_rows, scheduler, _wake = await _build_pool(
        outstanding_task_ids=[TASK_ID],
    )
    task_storage = provider.get_storage(ToolCallTask)
    task_storage._data[TASK_ID].record_seq = 99  # noqa: SLF001
    try:
        await pool.start()
        await _until(
            lambda: task_storage._data[TASK_ID].state is ToolCallTaskState.FAILED,  # noqa: SLF001
            "the record-mismatched task never reached terminal FAILED",
        )
        task = task_storage._data[TASK_ID]  # noqa: SLF001
        assert toolset.calls == [], f"the mismatched tool must never run: {toolset.calls}"
        assert task.last_error, "the mismatch must be recorded"
        assert (ClaimKind.TOOL_CALL, TASK_ID) not in engine._leases, "no requeue for a known-bad row"
    finally:
        await pool.drain_and_stop(timeout=3)
        await scheduler.aclose()


@pytest.mark.asyncio
async def test_a_refused_run_is_a_transient_failure_that_requeues():
    """The executor build refuses the run (RunRefused: the session's creator was
    deleted/disabled): the tool never runs, the row re-queues with an advanced
    attempt count and a last_error, and the lease survives for the backoff."""
    from primer.authz import RunRefused

    pool, engine, provider, toolset, session_rows, task_rows, scheduler, _wake = await _build_pool(
        outstanding_task_ids=[TASK_ID],
    )

    async def _refused(session, workspace):
        raise RunRefused("the session creator can no longer be resolved")

    pool._build_agent_executor = _refused  # type: ignore[method-assign]
    try:
        await pool.start()
        task_storage = provider.get_storage(ToolCallTask)
        await _until(
            lambda: task_storage._data[TASK_ID].attempts >= 1  # noqa: SLF001
            and task_storage._data[TASK_ID].state is ToolCallTaskState.QUEUED,  # noqa: SLF001
            "the refused task never re-queued with an advanced attempt count",
            timeout=15.0,
        )
        task = task_storage._data[TASK_ID]  # noqa: SLF001
        assert toolset.calls == [], "a refused run must never run the tool"
        assert task.last_error, "the refusal must be recorded"
        assert (ClaimKind.TOOL_CALL, TASK_ID) in engine._leases, "the lease must survive for the backoff"
    finally:
        await pool.drain_and_stop(timeout=3)
        await scheduler.aclose()


@pytest.mark.asyncio
async def test_a_deleted_row_is_dropped_without_error():
    """The row vanished between the lease and the claim (a race window: the
    adapter's eligibility read saw it, the handler's read does not): the handler
    drops the lease cleanly instead of crashing the slice. Driven at the handler
    directly - the engine's own missing-entity rule (not the claim cycle) is
    what reaps a lease whose row is gone for good."""
    from primer.worker.engine_handlers import run_engine_tool_call

    pool, engine, provider, toolset, session_rows, task_rows, scheduler, _wake = await _build_pool(
        outstanding_task_ids=[TASK_ID],
    )
    task_storage = provider.get_storage(ToolCallTask)
    task_storage._data.pop(TASK_ID)  # noqa: SLF001
    lease = engine._leases[(ClaimKind.TOOL_CALL, TASK_ID)]  # noqa: SLF001
    await run_engine_tool_call(pool, lease)
    assert (ClaimKind.TOOL_CALL, TASK_ID) not in engine._leases
    assert toolset.calls == []
    await pool.drain_and_stop(timeout=3)
    await scheduler.aclose()

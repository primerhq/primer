"""Focused coverage for the built TOOL_CALL claim executor (BPF wave 1).

These tests prove the standalone non-gated agent path: the pool claims a task,
runs it through the owning session's tool surface, records its terminal state,
and wakes the parked session when the batch is terminal. They also pin orphan,
retry, gate, takeover, mismatch, refusal, and deletion handling.

They are not evidence that the default-off feature is ready for production.
Graph binding execution is unsupported, GATED decisions are unwired, and cancel,
reconciliation, nested topology, and the post-release crash residual remain.
"""

from __future__ import annotations

import asyncio
import copy
import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import BaseModel

from primer.model.access import Access
from primer.claim.adapters.tool_calls import ToolCallClaimAdapter
from primer.claim.in_memory import InMemoryClaimEngine
from primer.int.claim import CLAIM_PRIORITY_RESUME, ClaimAdapter, ClaimKind
from primer.int.toolset import ToolsetProvider
from primer.model.agent import Agent, AgentModel
from primer.model.chat import Tool, ToolCallResult, ToolResultPart
from primer.model.scheduler import WorkerConfig
from primer.model.tool_call_task import ToolCallTask, ToolCallTaskState
from primer.model.yield_ import Yielded
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


@pytest.fixture(autouse=True)
def _exercise_reserved_resolved_gate_executor(request, monkeypatch) -> None:
    """Keep A2 covered while production S0 refuses human task gates."""
    if "task_gate_execution_disabled" in request.fixturenames:
        return
    monkeypatch.setattr(
        "primer.worker.engine_handlers._task_gate_human_execution_enabled",
        lambda _pool: True,
    )


@pytest.fixture
def task_gate_execution_disabled() -> None:
    """Use the production S0 executor capability state."""


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
        self.role_checks: list[str] = []
        self.contexts: list[object | None] = []

    def required_role(self, tool_name: str) -> str:
        # The ABC default is "admin" (fail-closed for MCP proxies); these are
        # ordinary user tools, so the unattributed/user-rank test principal clears
        # the floor.
        self.role_checks.append(tool_name)
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
        self.contexts.append(ctx)
        if tool_name == "boom":
            raise RuntimeError("the boom tool failed")
        return ToolCallResult(output=f"ok-from-{tool_name}", is_error=False)


class FakeMemoryToolset(FakeToolset):
    async def list_tools(self, *, principal: str | None = None):
        del principal
        for name in ("search", "read_document"):
            yield Tool(
                id=name,
                description=f"the {name} memory tool",
                toolset_id="collections",
                args_schema={"type": "object", "properties": {}},
            )


class _WorkspaceProbeArgs(BaseModel):
    n: int


class FakeWorkspaceTool:
    id = "probe"
    description = "workspace probe"
    examples: list = []
    requires_workspace_context = True
    read_only = False
    interruptible = True
    preview_args = None

    def __init__(self) -> None:
        self.calls: list[int] = []

    def parameters(self):
        return _WorkspaceProbeArgs

    async def execute(self, args, ctx):
        del ctx
        from primer.workspace.tool import ToolResult

        self.calls.append(args.n)
        return ToolResult(output=f"workspace-ok-{args.n}")


class FakeAgentSession:
    """The on-disk AgentSession slot the builder looks up by session id."""

    def __init__(self, session_id: str, *, workspace_tools: list | None = None) -> None:
        self.workspace_id = "ws-1"
        self.session_id = session_id
        self.agent_id = "agent-1"
        self.workspace_tools = list(workspace_tools or [])
        self.system_prompt_fragment = ""


class FakeWorkspace:
    """A workspace whose messages.jsonl holds the batch's TOOL_CALL record."""

    id = "ws-1"
    state_path = ".state"

    def __init__(
        self,
        records_by_session: dict[str, list[SessionMessageRecord]],
        *,
        workspace_tools: list | None = None,
    ) -> None:
        self._records = records_by_session
        self._workspace_tools = list(workspace_tools or [])
        self.session_gets: list[str] = []

    async def get_session(self, session_id: str) -> FakeAgentSession:
        self.session_gets.append(session_id)
        return FakeAgentSession(
            session_id,
            workspace_tools=self._workspace_tools,
        )

    def _log_sid(self, path: str) -> str | None:
        parts = path.split("/")
        if len(parts) == 4 and parts[0] == self.state_path and parts[1] == "sessions" and parts[2] in self._records:
            return parts[2]
        return None

    async def list_files(self, path: str = ".", **_kw) -> list:
        from types import SimpleNamespace

        parts = path.split("/")
        if len(parts) == 3 and parts[0] == self.state_path and parts[1] == "sessions" and parts[2] in self._records:
            return [SimpleNamespace(path=f"{path}/messages.jsonl", kind="file", size_bytes=0)]
        return []

    async def read_file_from(self, path: str, offset: int) -> bytes:
        return (await self.read_file(path))[offset:]

    async def file_info(self, path: str):
        from types import SimpleNamespace

        from primer.model.except_ import NotFoundError

        if path.endswith("/messages.jsonl") and self._log_sid(path) is not None:
            return SimpleNamespace(size_bytes=len(await self.read_file(path)))
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
        self._toolsets = {TOOLSET_ID: toolset}
        self.requests: list[str] = []

    def add_toolset(self, toolset_id: str, toolset: ToolsetProvider) -> None:
        self._toolsets[toolset_id] = toolset

    async def get_toolset(self, toolset_id: str):
        self.requests.append(toolset_id)
        return self._toolsets.get(toolset_id)


class FakeStorageProvider:
    """One ``_InMemoryStorage`` per model class, behind the StorageProvider seam."""

    def __init__(self) -> None:
        self._by_cls: dict = {}

    def get_storage(self, model_cls):
        return self._by_cls.setdefault(model_cls, _InMemoryStorage(model_cls))


def _tool_call_record(
    tool: str = "do_thing",
    *,
    scoped_tool_name: str | None = None,
) -> SessionMessageRecord:
    return SessionMessageRecord(
        seq=1,
        kind=SessionMessageKind.TOOL_CALL,
        created_at=_now(),
        payload={
            "id": SCOPED,
            "name": scoped_tool_name or f"{TOOLSET_ID}__{tool}",
            "arguments": {"n": 7},
        },
    )


def _parked_session(
    workspace_session_cls_rows: dict,
    *,
    outstanding_task_ids: list[str],
    agent_tools: list[str] | None = None,
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
            agent_snapshot=Agent(id="agent-1", access=Access(public=True),
                model=AgentModel(profile_id="profile-1"),
                description="the probe agent",
                tools=(
                    list(agent_tools)
                    if agent_tools is not None
                    else [f"{TOOLSET_ID}__do_thing", f"{TOOLSET_ID}__boom"]
                ),
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
    scoped_tool_name: str | None = None,
    agent_tools: list[str] | None = None,
    workspace_tools: list | None = None,
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
    effective_tool_name = scoped_tool_name or f"{TOOLSET_ID}__{tool}"
    workspace = FakeWorkspace(
        {SESSION_ID: [_tool_call_record(tool, scoped_tool_name=effective_tool_name)]},
        workspace_tools=workspace_tools,
    )
    registry = FakeWorkspaceRegistry(workspace)
    provider_registry = FakeProviderRegistry(toolset)

    session_rows: dict = {}
    session = _parked_session(
        session_rows,
        outstanding_task_ids=outstanding_task_ids,
        agent_tools=agent_tools,
    )
    session_storage = provider.get_storage(WorkspaceSession)
    await session_storage.create(session)

    task_rows: dict = {}

    def _task(task_id: str) -> ToolCallTask:
        task = ToolCallTask(
            id=task_id,
            session_id=SESSION_ID,
            turn_no=0,
            tool_name=effective_tool_name,
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
    result_state, and the last-sibling wake flips the session resumable."""
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
    FAILED without running the tool, lease dropped, never QUEUED again."""
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
    """An execution that always fails must stop requeueing: at attempt_count == max_attempts
    the task goes terminal FAILED carrying a synthesized error result_state (the resume
    coordinator hands the model a tool_result for its tool_use instead of wedging).

    The poison is the tool manager's own failure: a tool that raises is answered by the
    manager as an error result (W16, ruling D1; the next test), so it no longer poisons."""
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
    tool_manager = _PoisonToolManager()

    async def _poison_executor(session, workspace):
        return _executor_with(tool_manager)

    pool._build_agent_executor = _poison_executor  # type: ignore[method-assign]
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
        assert tool_manager.executed == config.max_attempts, (
            f"the call ran {tool_manager.executed} times; it must run exactly max_attempts "
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


@pytest.mark.asyncio
async def test_takeover_of_a_crashed_holder_reaches_the_retry_cap():
    """GR-7: the tool-call retry cap must count the row's unclean executions,
    not only the lease's attempt_count (which a crashed holder never bumps:
    reclaiming an expired lease starts at 0, and only a release counts). A
    row RUNNING under a dead holder's token with attempts far above
    max_attempts is taken over and, at the cap, fails terminal with a
    synthesized result_state instead of running the tool that keeps killing
    the worker."""
    from primer.worker.engine_handlers import run_engine_tool_call

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
    pool, engine, provider, toolset, _s, _t, scheduler, _w = await _build_pool(
        outstanding_task_ids=[TASK_ID], config=config,
    )
    try:
        task_storage = provider.get_storage(ToolCallTask)
        max_attempts = pool.config.max_attempts
        # Simulate max_attempts + 3 previous holders that each claimed the row (state
        # RUNNING, attempts bumped on every takeover) and died with the process (no
        # release ran).
        row = task_storage._data[TASK_ID]  # noqa: SLF001
        task_storage._data[TASK_ID] = row.model_copy(update={  # noqa: SLF001
            "state": ToolCallTaskState.RUNNING,
            "claim_token": "dead-worker:2026-10-31T00:00:00+00:00:abcdef",
            "attempts": max_attempts + 3,
        })
        leases = await engine.claim_due("w-next", max_count=1, kinds=[ClaimKind.TOOL_CALL])
        assert leases, "the lease was not claimable"
        lease = leases[0]
        # The fresh lease counted none of the crashes ...
        assert lease.attempt_count == 0
        await run_engine_tool_call(pool, lease)
        after = task_storage._data[TASK_ID]  # noqa: SLF001
        # ... so the cap must count the row's own unclean executions (the
        # takeover itself bumped them to max_attempts + 4): the tool that
        # keeps killing its worker is failed terminal, never re-run.
        assert toolset.calls == []
        assert after.state is ToolCallTaskState.FAILED
        assert after.result_state is not None
        assert after.result_state.get("error") is True
        assert after.result_state.get("id") == CALL_ID
        assert after.last_error
        assert after.attempts == max_attempts + 4
        assert (ClaimKind.TOOL_CALL, TASK_ID) not in engine._leases
    finally:
        await scheduler.aclose()


@pytest.mark.asyncio
async def test_a_tool_that_raises_is_done_with_an_error_result_on_its_first_attempt():
    """W16 (ruling D1): a tool that raises is answered by the tool manager as an error result, so the task is DONE on its first
    attempt carrying that result (the model sees its own error under its own tool_use), and it is not retried: a retry would run
    the tool, and whatever it did before raising, again."""
    config = WorkerConfig(
        concurrency=2, heartbeat_interval_seconds=1, lease_ttl_seconds=5, poll_interval_seconds=0.1, drain_timeout_seconds=3,
        tool_calls_as_claims_enabled=True, max_attempts=2, base_backoff_seconds=0.1, max_backoff_seconds=1.0,
    )
    pool, engine, provider, toolset, session_rows, task_rows, scheduler, _wake = await _build_pool(
        outstanding_task_ids=[TASK_ID], config=config, tool="boom",
    )
    try:
        await pool.start()
        task_storage = provider.get_storage(ToolCallTask)
        await _until(
            lambda: task_storage._data[TASK_ID].state in (ToolCallTaskState.DONE, ToolCallTaskState.FAILED),  # noqa: SLF001
            "the task never reached a terminal state",
        )
        task = task_storage._data[TASK_ID]  # noqa: SLF001
        assert task.state is ToolCallTaskState.DONE
        assert toolset.calls == ["boom"], "the tool ran once and was not retried"
        assert task.result_state["error"] is True and task.result_state["id"] == CALL_ID
        assert f"{TOOLSET_ID}__boom" in task.result_state["output"]
        assert "the boom tool failed" not in task.result_state["output"], "the exception's own text stays in the server log"
    finally:
        await pool.drain_and_stop(timeout=3)
        await scheduler.aclose()


# ---- branch coverage (green-only; the three probes above are the red-first core) ----


class _PoisonToolManager:
    """A tool manager whose execute itself fails (not a tool's error: the manager does not answer it)."""

    def __init__(self) -> None:
        self.executed = 0

    async def execute(self, call, *, principal=None):
        self.executed += 1
        raise RuntimeError("the tool manager broke")


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
                resume_metadata={
                    "approval": "pending",
                    "policy_id": "pol-1",
                    "original_call": {
                        "id": CALL_ID,
                        "name": f"{TOOLSET_ID}__do_thing",
                        "arguments": {"n": 7},
                    },
                },
            ),
            tool_call_id=CALL_ID,
        )


class _UnexpectedYieldToolManager:
    def __init__(self) -> None:
        self.executed = 0

    async def execute(self, call, *, principal=None):
        self.executed += 1
        from primer.model.yield_ import YieldToWorker, Yielded

        raise YieldToWorker(
            Yielded(
                tool_name="unexpected_human_yield",
                event_key=f"unexpected:{SESSION_ID}:{CALL_ID}",
                resume_metadata={"prompt": "continue?"},
            ),
            tool_call_id=CALL_ID,
        )


class _ResolvedGateToolManager:
    def __init__(
        self, *, re_yield: bool = False, error: str | None = None,
    ) -> None:
        self.calls: list[tuple[object, dict]] = []
        self.re_yield = re_yield
        self.error = error

    async def execute(self, call, **kwargs):
        self.calls.append((call, kwargs))
        if self.error is not None:
            raise RuntimeError(self.error)
        if self.re_yield:
            from primer.model.yield_ import YieldToWorker, Yielded

            raise YieldToWorker(
                Yielded(
                    tool_name="test_task_gate_followup",
                    event_key=f"followup:{SESSION_ID}:{CALL_ID}",
                    resume_metadata={"phase": 2},
                ),
                tool_call_id=CALL_ID,
            )
        return ToolResultPart(id=call.id, output="resumed-ok", error=False)


def _resolve_task_gate(provider, *, payload, tool_name="_approval", metadata=None):
    from primer.model.principal import PrincipalRef
    from primer.model.responder_source import canonical_responder_source
    from primer.worker.task_gate_runtime import (
        TaskGateResolutionReceipt,
        TaskGateResponderReceipt,
        TaskGateState,
        make_resolution_receipt,
    )

    task_storage = provider.get_storage(ToolCallTask)
    task = task_storage._data[TASK_ID]  # noqa: SLF001
    token = "a" * 32
    event_key = f"tool_approval:{SESSION_ID}:{CALL_ID}"
    if tool_name == "ask_user":
        source = canonical_responder_source(
            "ask_user", {"session_id": SESSION_ID, "task_id": TASK_ID},
        )
        receipt = TaskGateResolutionReceipt(
            occurrence_token=token,
            gate_seq=1,
            event_key=event_key,
            payload=payload,
            responder=TaskGateResponderReceipt(
                person=PrincipalRef(
                    type="user", id="person-1", display="Person One",
                    role="user", source="local", user_id="person-1",
                ),
                responder_epoch="e" * 32,
                domain=source.domain,
                source_identity=source.identity,
                source_key=source.key,
                accepted_at=_now(),
            ),
            schema_version=2,
        )
    else:
        receipt = make_resolution_receipt(
            occurrence_token=token,
            gate_seq=1,
            event_key=event_key,
            payload=payload,
        )
    gate = TaskGateState(
        occurrence_token=token,
        gate_seq=1,
        event_key=event_key,
        yielded=Yielded(
            tool_name=tool_name,
            event_key=event_key,
            resume_metadata=metadata or {
                "original_call": {
                    "id": CALL_ID,
                    "name": task.tool_name,
                    "arguments": {"n": 7},
                },
            },
        ),
        tool_call_id=CALL_ID,
        tool_arguments={"n": 7},
        parked_at=_now(),
        deadline=_now() + timedelta(minutes=5),
        binding_epoch=1,
        resolution=receipt,
    )
    task_storage._data[TASK_ID] = task.model_copy(update={  # noqa: SLF001
        "gate_occurrence_token": token,
        "gate_event_key": event_key,
        "gate_until": gate.deadline,
        "gate_seq": 1,
        "gate_state": gate.to_jsonable(),
        "gate_resolution": receipt.to_jsonable(),
    })
    return gate


def _executor_with(tool_manager) -> object:
    import types

    return types.SimpleNamespace(
        _executor=types.SimpleNamespace(_tool_manager=tool_manager),
    )


@pytest.mark.asyncio
async def test_s0_seeded_resolved_human_gate_has_no_resume_or_tool_side_effect(
    task_gate_execution_disabled, monkeypatch,
) -> None:
    pool, engine, provider, toolset, _sessions, _tasks, scheduler, _wake = await _build_pool(
        outstanding_task_ids=[TASK_ID],
    )
    gate = _resolve_task_gate(
        provider, payload={"response": "blue"}, tool_name="ask_user",
        metadata={"prompt": "colour?"},
    )
    hook_lookups: list[str] = []

    def _unexpected_hook(name: str):
        hook_lookups.append(name)
        raise AssertionError("S0 must not resolve a task-gate resume hook")

    monkeypatch.setattr(
        "primer.worker.yield_resume_registry.get_resume_hook", _unexpected_hook,
    )

    async def _unexpected_workspace(_workspace_id):
        raise AssertionError("S0 must not resolve task execution dependencies")

    pool._workspace_registry.get_workspace = _unexpected_workspace  # type: ignore[method-assign]
    try:
        await pool.start()
        await _until(
            lambda: (ClaimKind.TOOL_CALL, TASK_ID) not in engine._leases,  # noqa: SLF001
            "S0 did not remove the seeded human-gate lease",
        )
        stored = await provider.get_storage(ToolCallTask).get(TASK_ID)
        assert stored is not None
        assert stored.state is ToolCallTaskState.QUEUED
        assert stored.gate_state == gate.to_jsonable()
        assert hook_lookups == []
        assert toolset.calls == []
        assert pool._provider_registry.requests == []  # noqa: SLF001
    finally:
        await pool.drain_and_stop(timeout=2.0)
        await scheduler.aclose()


@pytest.mark.parametrize(
    "payload,expected_calls,expected_output",
    [
        ({"decision": "approved", "decided_by": "user-1"}, 1, "resumed-ok"),
        ({"decision": "rejected", "reason": "no"}, 0, '"reason": "no"'),
        ({"__yield_timeout__": True}, 0, '"reason": "timed-out"'),
        ({"__yield_cancelled__": True, "reason": "stopped"}, 0, '"reason": "stopped"'),
    ],
)
@pytest.mark.asyncio
async def test_resolved_approval_uses_shared_resume_semantics(
    payload, expected_calls, expected_output,
):
    pool, engine, provider, _toolset, _sessions, _tasks, scheduler, _wake = await _build_pool(
        outstanding_task_ids=[TASK_ID],
    )
    gate = _resolve_task_gate(provider, payload=payload)
    manager = _ResolvedGateToolManager()
    setup_calls = []
    original_get_workspace = pool._workspace_registry.get_workspace

    async def _workspace(workspace_id):
        setup_calls.append("workspace")
        return await original_get_workspace(workspace_id)

    async def _manager(_session, _workspace, *, required_tool_name):
        setup_calls.append(("manager", required_tool_name))
        return manager

    async def _executor(_session, _workspace):
        raise AssertionError("a resolved approval must not build a full executor")

    pool._workspace_registry.get_workspace = _workspace  # type: ignore[method-assign]
    pool._build_agent_tool_manager = _manager  # type: ignore[method-assign]
    pool._build_agent_executor = _executor  # type: ignore[method-assign]
    try:
        await pool.start()
        task_storage = provider.get_storage(ToolCallTask)
        await _until(
            lambda: task_storage._data[TASK_ID].state is ToolCallTaskState.DONE,  # noqa: SLF001
            "the resolved gate did not reach DONE",
        )
        assert len(manager.calls) == expected_calls
        if expected_calls:
            assert manager.calls[0][1] == {"bypass_approval": True}
            assert setup_calls == [
                "workspace", ("manager", f"{TOOLSET_ID}__do_thing"),
            ]
        else:
            assert setup_calls == [], (
                "a rejected, timed-out or cancelled approval needs no ordinary-call setup"
            )
        assert expected_output in task_storage._data[TASK_ID].result_state["output"]  # noqa: SLF001
        from primer.model.tool_approval import ToolApprovalRecord

        records = provider.get_storage(ToolApprovalRecord)._data.values()  # noqa: SLF001
        assert [r.gate_event_key for r in records] == [
            f"{gate.event_key}@{gate.occurrence_token}",
        ]
    finally:
        await pool.drain_and_stop(timeout=3)
        await scheduler.aclose()


@pytest.mark.asyncio
async def test_resolved_approved_builds_only_narrow_authorized_manager(
    monkeypatch,
):
    from primer.memory import runtime as memory_runtime
    from primer.model.principal import PrincipalRef
    from primer.worker import executor_builders
    from primer.workspace import instructions, session_factory

    pool, _engine, provider, toolset, _sessions, _tasks, scheduler, _wake = await _build_pool(
        outstanding_task_ids=[TASK_ID],
    )
    _resolve_task_gate(provider, payload={"decision": "approved"})

    async def _unused_async(*_args, **_kwargs):
        raise AssertionError("approved resume reached a full-executor dependency")

    def _unused_graph(*_args, **_kwargs):
        raise AssertionError("approved resume built graph services")

    pool._build_agent_executor = _unused_async  # type: ignore[method-assign]
    pool._build_graph_invocation_services = _unused_graph  # type: ignore[method-assign]
    monkeypatch.setattr(executor_builders, "resolve_llm_and_model", _unused_async)
    monkeypatch.setattr(memory_runtime, "memory_enabled", _unused_async)
    monkeypatch.setattr(session_factory, "ensure_agent_slot", _unused_async)
    monkeypatch.setattr(instructions, "load_workspace_instructions", _unused_async)
    try:
        await pool.start()
        task_storage = provider.get_storage(ToolCallTask)
        await _until(
            lambda: task_storage._data[TASK_ID].state is ToolCallTaskState.DONE,  # noqa: SLF001
            "the approved gate did not complete through the narrow manager",
        )
        task = task_storage._data[TASK_ID]  # noqa: SLF001
        workspace = pool._workspace_registry._workspace  # noqa: SLF001
        registry = pool._provider_registry
        assert task.result_state["output"] == "ok-from-do_thing"
        assert registry.requests == [TOOLSET_ID]
        assert workspace.session_gets == [SESSION_ID]
        assert toolset.role_checks and set(toolset.role_checks) == {"do_thing"}
        assert toolset.calls == ["do_thing"]
        (ctx,) = toolset.contexts
        assert (ctx.session_id, ctx.workspace_id) == (SESSION_ID, "ws-1")
        assert ctx.initiated_by == PrincipalRef.unattributed()
    finally:
        await pool.drain_and_stop(timeout=3)
        await scheduler.aclose()


@pytest.mark.asyncio
async def test_resolved_approval_refuses_ordinary_tool_absent_from_agent_surface(
    monkeypatch,
):
    from primer.worker import engine_handlers

    pool, _engine, provider, toolset, _sessions, _tasks, scheduler, _wake = await _build_pool(
        outstanding_task_ids=[TASK_ID],
        agent_tools=[f"{TOOLSET_ID}__boom"],
    )
    _resolve_task_gate(provider, payload={"decision": "approved"})

    async def _must_not_retry(*_args, **_kwargs):
        raise AssertionError("an unauthorized resolved tool reached transient retry")

    monkeypatch.setattr(
        engine_handlers, "_transient_tool_call_failure", _must_not_retry,
    )
    try:
        await pool.start()
        task_storage = provider.get_storage(ToolCallTask)
        await _until(
            lambda: task_storage._data[TASK_ID].state is ToolCallTaskState.DONE,  # noqa: SLF001
            "the unauthorized resolved tool did not terminalize",
        )
        task = task_storage._data[TASK_ID]  # noqa: SLF001
        body = json.loads(task.result_state["output"])
        assert pool._provider_registry.requests == []
        assert toolset.calls == []
        assert body["is_error"] is True
        assert body["tool_name"] == "_approval"
        assert "not in the agent's registered tool list" in body["reason"]
    finally:
        await pool.drain_and_stop(timeout=3)
        await scheduler.aclose()


@pytest.mark.asyncio
async def test_resolved_approval_preserves_enabled_memory_tool_grant():
    from primer.model.memory import AgentMemory

    pool, _engine, provider, _toolset, _sessions, _tasks, scheduler, _wake = await _build_pool(
        outstanding_task_ids=[TASK_ID],
        scoped_tool_name="collections__search",
        agent_tools=[],
    )
    memory_toolset = FakeMemoryToolset()
    pool._provider_registry.add_toolset("collections", memory_toolset)
    await provider.get_storage(AgentMemory).create(
        AgentMemory(id="agent-1", enabled=True),
    )
    _resolve_task_gate(provider, payload={"decision": "approved"})
    try:
        await pool.start()
        task_storage = provider.get_storage(ToolCallTask)
        await _until(
            lambda: task_storage._data[TASK_ID].state is ToolCallTaskState.DONE,  # noqa: SLF001
            "the approved memory-tool gate did not complete",
        )
        task = task_storage._data[TASK_ID]  # noqa: SLF001
        assert task.result_state["output"] == "ok-from-search"
        assert pool._provider_registry.requests == ["collections"]
        assert memory_toolset.calls == ["search"]
    finally:
        await pool.drain_and_stop(timeout=3)
        await scheduler.aclose()


@pytest.mark.asyncio
async def test_resolved_approval_preserves_implicit_workspace_tool_grant():
    workspace_tool = FakeWorkspaceTool()
    pool, _engine, provider, _toolset, _sessions, _tasks, scheduler, _wake = await _build_pool(
        outstanding_task_ids=[TASK_ID],
        scoped_tool_name="workspace__probe",
        agent_tools=[],
        workspace_tools=[workspace_tool],
    )
    _resolve_task_gate(provider, payload={"decision": "approved"})
    try:
        await pool.start()
        task_storage = provider.get_storage(ToolCallTask)
        await _until(
            lambda: task_storage._data[TASK_ID].state is ToolCallTaskState.DONE,  # noqa: SLF001
            "the approved workspace-tool gate did not complete",
        )
        task = task_storage._data[TASK_ID]  # noqa: SLF001
        assert task.result_state["output"] == "workspace-ok-7"
        assert pool._provider_registry.requests == []
        assert workspace_tool.calls == [7]
    finally:
        await pool.drain_and_stop(timeout=3)
        await scheduler.aclose()


@pytest.mark.parametrize("failure", ["external_defs", "manager"])
@pytest.mark.asyncio
async def test_full_executor_preserves_manager_construction_order(
    monkeypatch,
    failure,
):
    from types import SimpleNamespace

    from primer.agent.tool_manager import ToolExecutionManager
    from primer.memory import runtime as memory_runtime
    from primer.model.principal import PrincipalRef
    from primer.worker import executor_builders
    from primer.workspace import session_factory

    pool, _engine, provider, toolset, _sessions, _tasks, scheduler, _wake = await _build_pool(
        outstanding_task_ids=[TASK_ID],
    )
    session = await provider.get_storage(WorkspaceSession).get(SESSION_ID)
    workspace = pool._workspace_registry._workspace  # noqa: SLF001
    events: list[str] = []

    async def _memory_enabled(_storage, _agent):
        events.append("memory_enabled")
        return True

    async def _get_toolset(toolset_id):
        events.append(f"provider:{toolset_id}")
        return toolset

    async def _client_toolset(_pool, _session):
        events.append("client_toolset")
        return {}

    async def _ensure_slot(_workspace, _session, _agent):
        events.append("slot")
        return FakeAgentSession(SESSION_ID)

    async def _principal(_storage, _session):
        events.append("principal")
        return PrincipalRef.unattributed()

    async def _workspace_access(_storage, _session, _principal):
        events.append("workspace_access")

    async def _llm(*_args, **_kwargs):
        events.append("llm")
        return object(), SimpleNamespace(context_length=4096)

    def _graph(**_kwargs):
        events.append("graph")
        return object()

    async def _memory_prompt(*_args, **_kwargs):
        events.append("memory_prompt")
        return [], ""

    async def _memory_lookup(**_kwargs):
        events.append("memory_lookup")
        return None

    def _external_defs(_session, _agent):
        events.append("external_defs")
        if failure == "external_defs":
            raise RuntimeError("external defs failed")
        return None

    def _manager(_cls, **_kwargs):
        events.append("manager")
        raise RuntimeError("manager failed")

    monkeypatch.setattr(memory_runtime, "memory_enabled", _memory_enabled)
    monkeypatch.setattr(memory_runtime, "memory_prompt_parts", _memory_prompt)
    monkeypatch.setattr(memory_runtime, "memory_lookup_for", _memory_lookup)
    monkeypatch.setattr(pool._provider_registry, "get_toolset", _get_toolset)
    monkeypatch.setattr(executor_builders, "client_toolset_for", _client_toolset)
    monkeypatch.setattr(session_factory, "ensure_agent_slot", _ensure_slot)
    monkeypatch.setattr(executor_builders, "resolve_run_principal", _principal)
    monkeypatch.setattr(
        executor_builders, "refuse_run_without_workspace_access", _workspace_access,
    )
    monkeypatch.setattr(executor_builders, "resolve_llm_and_model", _llm)
    pool._build_graph_invocation_services = _graph  # type: ignore[method-assign]
    monkeypatch.setattr(executor_builders, "_external_defs_for", _external_defs)
    monkeypatch.setattr(ToolExecutionManager, "for_workspace", classmethod(_manager))

    try:
        with pytest.raises(RuntimeError, match=f"{failure.replace('_', ' ')} failed"):
            await pool._build_agent_executor(session, workspace)
        expected = [
            "memory_enabled",
            f"provider:{TOOLSET_ID}",
            "provider:collections",
            "client_toolset",
            "slot",
            "principal",
            "workspace_access",
            "llm",
            "graph",
            "memory_prompt",
            "memory_lookup",
            "external_defs",
        ]
        if failure == "manager":
            expected.append("manager")
        assert events == expected
    finally:
        await scheduler.aclose()


@pytest.mark.asyncio
async def test_resolved_generic_gate_calls_hook_with_exact_state(monkeypatch):
    from primer.toolset._system_tools import ask_user_resume
    from primer.worker import yield_resume_registry

    seen = []

    def _hook(metadata, payload, context):
        seen.append((metadata, payload, context))
        return ask_user_resume(metadata, payload, context)

    monkeypatch.setitem(
        yield_resume_registry._registry, "ask_user", _hook,  # noqa: SLF001
    )
    pool, _engine, provider, _toolset, _sessions, _tasks, scheduler, _wake = await _build_pool(
        outstanding_task_ids=[TASK_ID],
    )
    gate = _resolve_task_gate(
        provider,
        payload={"response": "yes"},
        tool_name="ask_user",
        metadata={"question": "continue?"},
    )

    async def _workspace(_workspace_id):
        raise AssertionError("a resolved ask_user gate must not resolve a workspace")

    async def _executor(_session, _workspace):
        raise AssertionError("a resolved ask_user gate must not build an executor")

    async def _manager(_session, _workspace, *, required_tool_name):
        raise AssertionError(
            f"a resolved ask_user gate must not build a manager for {required_tool_name}"
        )

    pool._workspace_registry.get_workspace = _workspace  # type: ignore[method-assign]
    pool._build_agent_tool_manager = _manager  # type: ignore[method-assign]
    pool._build_agent_executor = _executor  # type: ignore[method-assign]
    try:
        await pool.start()
        task_storage = provider.get_storage(ToolCallTask)
        await _until(
            lambda: task_storage._data[TASK_ID].state is ToolCallTaskState.DONE,  # noqa: SLF001
            "the generic resolved gate did not reach DONE",
        )
        metadata, payload, context = seen.pop()
        assert metadata == {
            "question": "continue?",
            "parked_at_iso": gate.parked_at.isoformat(),
        }
        assert payload == {"response": "yes"}
        assert (context.tool_name, context.tool_call_id, context.session_id) == (
            "ask_user", CALL_ID, SESSION_ID,
        )
        assert "parked_at_iso" not in gate.yielded.resume_metadata
        assert task_storage._data[TASK_ID].result_state["output"] == '{"response": "yes"}'  # noqa: SLF001
    finally:
        await pool.drain_and_stop(timeout=3)
        await scheduler.aclose()


@pytest.mark.asyncio
async def test_resolved_approved_manager_failure_is_redacted_terminal_not_retry(
    monkeypatch,
):
    from primer.worker import engine_handlers

    pool, engine, provider, toolset, _sessions, _tasks, scheduler, _wake = await _build_pool(
        outstanding_task_ids=[TASK_ID],
    )
    _resolve_task_gate(provider, payload={"decision": "approved"})
    secret = "Authorization: Bearer sk-resolved-setup-secret"
    manager_build_calls = 0

    async def _manager(_session, _workspace, *, required_tool_name):
        nonlocal manager_build_calls
        manager_build_calls += 1
        assert required_tool_name == f"{TOOLSET_ID}__do_thing"
        raise RuntimeError(secret)

    async def _executor(_session, _workspace):
        raise AssertionError("manager failure must not build a full executor")

    async def _must_not_retry(*_args, **_kwargs):
        raise AssertionError("a resolved dependency failure reached transient retry")

    pool._build_agent_tool_manager = _manager  # type: ignore[method-assign]
    pool._build_agent_executor = _executor  # type: ignore[method-assign]
    monkeypatch.setattr(
        engine_handlers, "_transient_tool_call_failure", _must_not_retry,
    )
    try:
        await pool.start()
        task_storage = provider.get_storage(ToolCallTask)
        await _until(
            lambda: task_storage._data[TASK_ID].state is ToolCallTaskState.DONE,  # noqa: SLF001
            "the resolved setup failure did not terminate",
        )
        task = task_storage._data[TASK_ID]  # noqa: SLF001
        body = json.loads(task.result_state["output"])
        assert manager_build_calls == 1
        assert toolset.calls == [], "the ordinary tool path must not run"
        assert body["is_error"] is True and body["tool_name"] == "_approval"
        assert "sk-resolved-setup-secret" not in body["reason"]
        assert task.attempts == 0
        assert (ClaimKind.TOOL_CALL, TASK_ID) not in engine._leases
    finally:
        await pool.drain_and_stop(timeout=3)
        await scheduler.aclose()


@pytest.mark.asyncio
async def test_resolved_approval_reyield_gets_a_new_occurrence():
    pool, _engine, provider, _toolset, _sessions, _tasks, scheduler, _wake = await _build_pool(
        outstanding_task_ids=[TASK_ID],
    )
    old = _resolve_task_gate(provider, payload={"decision": "approved"})
    manager = _ResolvedGateToolManager(re_yield=True)

    async def _manager(_session, _workspace, *, required_tool_name):
        assert required_tool_name == f"{TOOLSET_ID}__do_thing"
        return manager

    pool._build_agent_tool_manager = _manager  # type: ignore[method-assign]
    try:
        await pool.start()
        task_storage = provider.get_storage(ToolCallTask)
        await _until(
            lambda: task_storage._data[TASK_ID].state is ToolCallTaskState.GATED,  # noqa: SLF001
            "the resumed task did not re-gate",
        )
        task = task_storage._data[TASK_ID]  # noqa: SLF001
        assert task.gate_seq == 2
        assert task.gate_occurrence_token != old.occurrence_token
        assert len(manager.calls) == 1
    finally:
        await pool.drain_and_stop(timeout=3)
        await scheduler.aclose()


@pytest.mark.asyncio
async def test_resolved_gate_binding_mismatch_executes_nothing():
    pool, _engine, provider, _toolset, sessions, _tasks, scheduler, _wake = await _build_pool(
        outstanding_task_ids=[TASK_ID],
    )
    _resolve_task_gate(provider, payload={"decision": "approved"})
    sessions[SESSION_ID] = sessions[SESSION_ID].model_copy(update={
        "binding_epoch": 2,
    })
    provider.get_storage(WorkspaceSession)._data[SESSION_ID] = sessions[SESSION_ID]  # noqa: SLF001
    manager = _ResolvedGateToolManager()

    async def _manager(_session, _workspace, *, required_tool_name):
        raise AssertionError(
            f"the stale-binding gate reached manager setup for {required_tool_name}"
        )

    pool._build_agent_tool_manager = _manager  # type: ignore[method-assign]
    try:
        await pool.start()
        task_storage = provider.get_storage(ToolCallTask)
        await _until(
            lambda: task_storage._data[TASK_ID].state is ToolCallTaskState.FAILED,  # noqa: SLF001
            "the stale-binding gate did not fail closed",
        )
        assert manager.calls == []
    finally:
        await pool.drain_and_stop(timeout=3)
        await scheduler.aclose()


@pytest.mark.asyncio
async def test_resolved_gate_with_stale_session_park_executes_nothing():
    pool, _engine, provider, _toolset, sessions, _tasks, scheduler, _wake = await _build_pool(
        outstanding_task_ids=[TASK_ID],
    )
    _resolve_task_gate(provider, payload={"decision": "approved"})
    sessions[SESSION_ID] = sessions[SESSION_ID].model_copy(update={
        "parked_state": {"kind": "tool_wait", "outstanding_task_ids": []},
    })
    provider.get_storage(WorkspaceSession)._data[SESSION_ID] = sessions[SESSION_ID]  # noqa: SLF001
    manager = _ResolvedGateToolManager()

    async def _manager(_session, _workspace, *, required_tool_name):
        raise AssertionError(
            f"the stale-session gate reached manager setup for {required_tool_name}"
        )

    pool._build_agent_tool_manager = _manager  # type: ignore[method-assign]
    try:
        await pool.start()
        task_storage = provider.get_storage(ToolCallTask)
        await _until(
            lambda: task_storage._data[TASK_ID].state is ToolCallTaskState.FAILED,  # noqa: SLF001
            "the stale-session gate did not fail closed",
        )
        assert manager.calls == []
    finally:
        await pool.drain_and_stop(timeout=3)
        await scheduler.aclose()


@pytest.mark.asyncio
async def test_approval_audit_failure_does_not_block_resumed_execution():
    from primer.model.tool_approval import ToolApprovalRecord

    pool, _engine, provider, _toolset, _sessions, _tasks, scheduler, _wake = await _build_pool(
        outstanding_task_ids=[TASK_ID],
    )
    _resolve_task_gate(provider, payload={"decision": "approved"})
    audit_storage = provider.get_storage(ToolApprovalRecord)

    async def _fail_audit(_record):
        raise RuntimeError("audit unavailable")

    audit_storage.create = _fail_audit
    manager = _ResolvedGateToolManager()

    async def _manager(_session, _workspace, *, required_tool_name):
        assert required_tool_name == f"{TOOLSET_ID}__do_thing"
        return manager

    pool._build_agent_tool_manager = _manager  # type: ignore[method-assign]
    try:
        await pool.start()
        task_storage = provider.get_storage(ToolCallTask)
        await _until(
            lambda: task_storage._data[TASK_ID].state is ToolCallTaskState.DONE,  # noqa: SLF001
            "the audit failure blocked resolved execution",
        )
        assert len(manager.calls) == 1
    finally:
        await pool.drain_and_stop(timeout=3)
        await scheduler.aclose()


@pytest.mark.asyncio
async def test_resolved_resume_exception_is_redacted_and_terminal():
    pool, _engine, provider, _toolset, _sessions, _tasks, scheduler, _wake = await _build_pool(
        outstanding_task_ids=[TASK_ID],
    )
    _resolve_task_gate(provider, payload={"decision": "approved"})
    secret = "Authorization: Bearer sk-task-gate-secret"
    manager = _ResolvedGateToolManager(error=secret)

    async def _manager(_session, _workspace, *, required_tool_name):
        assert required_tool_name == f"{TOOLSET_ID}__do_thing"
        return manager

    pool._build_agent_tool_manager = _manager  # type: ignore[method-assign]
    try:
        await pool.start()
        task_storage = provider.get_storage(ToolCallTask)
        await _until(
            lambda: task_storage._data[TASK_ID].state is ToolCallTaskState.DONE,  # noqa: SLF001
            "the failed resolved resume did not terminate",
        )
        result = task_storage._data[TASK_ID].result_state  # noqa: SLF001
        body = json.loads(result["output"])
        assert result["error"] is True and body["is_error"] is True
        assert body["tool_name"] == "_approval"
        assert body["reason"].startswith("resume failed: RuntimeError")
        assert "sk-task-gate-secret" not in body["reason"]
        assert len(manager.calls) == 1
    finally:
        await pool.drain_and_stop(timeout=3)
        await scheduler.aclose()


@pytest.mark.asyncio
async def test_post_claim_gate_mutation_uses_json_typed_equality(monkeypatch):
    from primer.model.tool_approval import ToolApprovalRecord

    pool, _engine, provider, _toolset, _sessions, _tasks, scheduler, _wake = await _build_pool(
        outstanding_task_ids=[TASK_ID],
    )
    _resolve_task_gate(
        provider,
        payload={"decision": "approved"},
        metadata={
            "flag": True,
            "original_call": {
                "id": CALL_ID,
                "name": f"{TOOLSET_ID}__do_thing",
                "arguments": {"n": 7},
            },
        },
    )
    task_storage = provider.get_storage(ToolCallTask)
    original_get = task_storage.get
    mutated = False

    async def _mutate_on_post_claim_read(task_id, *, conn=None):
        nonlocal mutated
        row = await original_get(task_id, conn=conn)
        if (
            task_id == TASK_ID
            and row.state is ToolCallTaskState.RUNNING
            and not mutated
        ):
            mutated = True
            gate_state = copy.deepcopy(row.gate_state)
            gate_state["yielded"]["resume_metadata"]["flag"] = 1
            row = row.model_copy(update={"gate_state": gate_state})
            task_storage._data[task_id] = row  # noqa: SLF001
        return row

    monkeypatch.setattr(task_storage, "get", _mutate_on_post_claim_read)
    setup_calls = []

    async def _manager(session, workspace, *, required_tool_name):
        setup_calls.append((session, workspace, required_tool_name))
        raise AssertionError("the changed gate reached manager setup")

    pool._build_agent_tool_manager = _manager  # type: ignore[method-assign]
    try:
        await pool.start()
        await _until(
            lambda: task_storage._data[TASK_ID].state is ToolCallTaskState.FAILED,  # noqa: SLF001
            "the post-claim envelope mutation did not fail closed",
        )
        assert setup_calls == []
        assert provider.get_storage(ToolApprovalRecord)._data == {}  # noqa: SLF001
    finally:
        await pool.drain_and_stop(timeout=3)
        await scheduler.aclose()


@pytest.mark.asyncio
async def test_cancelled_resolved_hook_keeps_receipt_for_takeover(monkeypatch):
    from primer.worker import yield_resume_registry
    from primer.worker.engine_handlers import run_engine_tool_call

    calls = 0

    async def _hook(metadata, payload, context):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise asyncio.CancelledError
        return ToolCallResult(output="resumed-after-takeover", is_error=False)

    monkeypatch.setitem(
        yield_resume_registry._registry, "test_task_gate", _hook,  # noqa: SLF001
    )
    pool, engine, provider, _toolset, _sessions, _tasks, scheduler, _wake = await _build_pool(
        outstanding_task_ids=[TASK_ID],
    )
    gate = _resolve_task_gate(
        provider,
        payload={"answer": "yes"},
        tool_name="test_task_gate",
        metadata={"phase": 1},
    )
    try:
        first = (await engine.claim_due(
            "worker-one", max_count=1, kinds=[ClaimKind.TOOL_CALL],
        ))[0]
        with pytest.raises(asyncio.CancelledError):
            await run_engine_tool_call(pool, first)

        task_storage = provider.get_storage(ToolCallTask)
        retained = task_storage._data[TASK_ID]  # noqa: SLF001
        assert retained.state is ToolCallTaskState.RUNNING
        assert retained.gate_occurrence_token == gate.occurrence_token
        assert retained.gate_resolution == gate.resolution.to_jsonable()
        assert retained.gate_state == gate.to_jsonable()

        lease_row = engine._leases[(ClaimKind.TOOL_CALL, TASK_ID)]  # noqa: SLF001
        lease_row.expires_at = _now() - timedelta(seconds=1)
        second = (await engine.claim_due(
            "worker-two", max_count=1, kinds=[ClaimKind.TOOL_CALL],
        ))[0]
        await run_engine_tool_call(pool, second)

        finished = task_storage._data[TASK_ID]  # noqa: SLF001
        assert calls == 2
        assert finished.state is ToolCallTaskState.DONE
        assert finished.result_state["output"] == "resumed-after-takeover"
        assert finished.gate_state is None and finished.gate_resolution is None
    finally:
        await pool.drain_and_stop(timeout=3)
        await scheduler.aclose()


@pytest.mark.asyncio
async def test_a_gated_tool_call_parks_the_task_not_the_session():
    """The approval/yield gate: the handler parks the TASK (GATED row, bumped
    gate_seq, cleared token, lease dropped) and leaves the session's park
    untouched - one gated call does not block its siblings, and the session is
    never woken by the gate itself. Production decision wiring does not yet advance
    the GATED row back to QUEUED."""
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
        from primer.worker.task_gate_runtime import TaskGateState, decode_task_gate_state

        gate = decode_task_gate_state(task.gate_state, task=task)
        assert isinstance(gate, TaskGateState)
        assert tool_manager.executed == 1
        assert toolset.calls == [], "a gated call must not run the real tool"
        assert task.gate_seq == 1, "the gate branch bumps gate_seq"
        assert len(task.gate_occurrence_token or "") == 32
        assert gate.occurrence_token == task.gate_occurrence_token
        assert gate.gate_seq == task.gate_seq
        assert gate.event_key == task.gate_event_key
        assert gate.tool_call_id == CALL_ID
        assert gate.binding_epoch == session_rows[SESSION_ID].binding_epoch
        assert gate.resolution is None and task.gate_resolution is None
        assert "llm_messages" not in task.gate_state
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
async def test_s0_policy_race_terminalizes_without_persisting_task_gate(
    task_gate_execution_disabled,
) -> None:
    pool, engine, provider, toolset, _session_rows, _tasks, scheduler, wake_calls = await _build_pool(
        outstanding_task_ids=[TASK_ID],
    )
    manager = _GatingToolManager()

    async def _executor(_session, _workspace):
        return _executor_with(manager)

    pool._build_agent_executor = _executor  # type: ignore[method-assign]
    try:
        await pool.start()
        task_storage = provider.get_storage(ToolCallTask)
        await _until(
            lambda: task_storage._data[TASK_ID].state is ToolCallTaskState.FAILED,  # noqa: SLF001
            "the approval-policy race did not terminalize",
        )
        task = task_storage._data[TASK_ID]  # noqa: SLF001
        assert manager.executed == 1
        assert toolset.calls == []
        assert task.gate_state is None and task.gate_resolution is None
        assert task.result_state["error"] is True
        assert "responder ordering P3" in task.result_state["output"]
        assert (ClaimKind.TOOL_CALL, TASK_ID) not in engine._leases  # noqa: SLF001
        await _until(
            lambda: any(flipped for _signal, flipped in wake_calls),
            "the terminal final sibling did not wake its session",
        )
        session = await provider.get_storage(WorkspaceSession).get(SESSION_ID)
        assert session is not None and session.parked_status == "resumable"
        assert any(flipped for _signal, flipped in wake_calls)
    finally:
        await pool.drain_and_stop(timeout=3)
        await scheduler.aclose()


@pytest.mark.asyncio
async def test_s0_unexpected_generic_yield_uses_terminal_backstop(
    task_gate_execution_disabled,
) -> None:
    pool, engine, provider, toolset, _sessions, _tasks, scheduler, wake_calls = await _build_pool(
        outstanding_task_ids=[TASK_ID],
    )
    manager = _UnexpectedYieldToolManager()

    async def _executor(_session, _workspace):
        return _executor_with(manager)

    pool._build_agent_executor = _executor  # type: ignore[method-assign]
    try:
        await pool.start()
        task_storage = provider.get_storage(ToolCallTask)
        await _until(
            lambda: task_storage._data[TASK_ID].state is ToolCallTaskState.FAILED,  # noqa: SLF001
            "the unexpected yield did not terminalize",
        )
        task = task_storage._data[TASK_ID]  # noqa: SLF001
        assert manager.executed == 1
        assert toolset.calls == []
        assert task.gate_state is None and task.gate_resolution is None
        assert task.result_state["error"] is True
        assert (ClaimKind.TOOL_CALL, TASK_ID) not in engine._leases  # noqa: SLF001
        await _until(
            lambda: any(flipped for _signal, flipped in wake_calls),
            "the unexpected-yield terminal release did not wake the session",
        )
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

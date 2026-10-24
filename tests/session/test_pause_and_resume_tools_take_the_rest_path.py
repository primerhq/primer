"""The pause and resume TOOLS take the REST routes' path and reach the worker that runs the session (ticket 01a16d3c-9984).

``workspaces__pause_workspace_session`` and ``workspaces__resume_workspace_session`` used to flip a flag on the in-memory slot handle of the process
that served the call and write nothing to the session row. The probe (the first test below, red on main at 35407869f) showed both reported effects:

* on ONE process the next turn saw the flag, set the slot PAUSED and returned without calling the model, and the dispatch mapped that empty turn
  to ENDED/completed (``_post_turn_status(None, PAUSED)``): a pause that ENDED the session;
* from ANOTHER process the worker never saw it: the turn ran, the model was called, and the session came to rest WAITING, not paused.

The REST pause (the control below) was right in both worlds: it writes ``pause_requested`` on the row, which the worker reads before it builds an
executor. Both tools now go through the REST routes' helpers (``primer.workspace.session_factory.pause_session`` / ``resume_session``), so a tool
resume also starts a session created with ``auto_start: false``, exactly as ``POST .../resume`` does, and answers in the same words in every state.

Every test runs on real SQLite and, where a turn runs, with the REAL dispatcher (``run_one_session_turn``) and the REAL agent executor over a real
local workspace. "Another process" is a second SQLite provider on the same file and a second workspace backend on the same root: the worker's
handles share nothing in memory with the caller's.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from primer.agent.tool_manager import ToolExecutionManager
from primer.agent.workspace_executor import WorkspaceAgentExecutor
from primer.model.chat import Done, TextDelta
from primer.model.except_ import ConflictError, NotFoundError
from primer.model.provider import SqliteConfig
from primer.model.workspace import LocalWorkspaceConfig, WorkspaceProvider, WorkspaceProviderType
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.session.dispatch import SessionDispatchDeps, run_one_session_turn
from primer.storage.sqlite import SqliteStorageProvider
from primer.toolset.workspaces import build_workspaces_toolset
from primer.workspace.factory import WorkspaceBackendFactory
from tests.agent.test_workspace_executor import _agent, _build_session, _FakeLLM, _model, _template
from tests.session.test_dispatch import (  # noqa: F401  (fixtures are used by name)
    FakeWorkspaceIO,
    _make_lease,
    fake_event_bus,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="the workspace state repo needs git")

OK = [TextDelta(text="all good", index=0), Done(stop_reason="stop", raw_reason="stop")]
NOW = datetime(2026, 10, 24, 9, 0, tzinfo=timezone.utc)
#: The request the REST handlers are called with: no actor, and the sessions here name no creator, so the rank rule admits it.
REQ = SimpleNamespace(state=SimpleNamespace(actor=None))


class _Registry:
    def __init__(self, workspace) -> None:
        self._workspace = workspace

    async def get_workspace(self, workspace_id):
        return self._workspace

    async def get_workspace_row(self, workspace_id):
        return None


class _Scheduler:
    def __init__(self) -> None:
        self.enqueued: list[str] = []

    async def enqueue(self, session_id) -> None:
        self.enqueued.append(session_id)


class _Engine:
    def __init__(self) -> None:
        self.upserts: list[str] = []

    async def upsert(self, kind, entity_id, *args, **kwargs) -> None:
        self.upserts.append(entity_id)


async def _sqlite(path) -> SqliteStorageProvider:
    provider = SqliteStorageProvider(SqliteConfig(path=str(path)))
    await provider.initialize()
    await provider.get_event_store().ensure_schema()
    return provider


class _Process:
    """One process's view: its own storage provider over the shared SQLite file and its own workspace handle over the shared root."""

    def __init__(self, sp, workspace) -> None:
        self.sp = sp
        self.workspace = workspace
        self.scheduler = _Scheduler()
        self.engine = _Engine()

    @property
    def sessions(self):
        return self.sp.get_storage(WorkspaceSession)

    def toolset(self, event_bus=None):
        return build_workspaces_toolset(
            storage_provider=self.sp, workspace_registry=_Registry(self.workspace), scheduler=self.scheduler, claim_engine=self.engine,
            event_bus=event_bus,
        )

    def dispatch_deps(self, llm, event_bus) -> SessionDispatchDeps:
        workspace = self.workspace

        async def build(_session):
            slot = await workspace.get_session(_session.id)
            manager = ToolExecutionManager.for_workspace(toolset_providers={}, session=slot)
            return WorkspaceAgentExecutor(agent=_agent(), llm=llm, llm_model=_model(), tool_manager=manager, session=slot)

        return SessionDispatchDeps(
            storage_provider=self.sp, workspace_io=FakeWorkspaceIO(), event_bus=event_bus, build_executor=build,
            scheduler=self.scheduler, claim_engine=self.engine, workspace_registry=_Registry(workspace),
        )


async def _world(tmp_path, *, two_processes: bool = False, **row_fields: Any):
    """The caller (API) process and the worker process (the same process twice unless ``two_processes``), the session's row (RUNNING unless
    ``row_fields`` say otherwise) and the closers."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    backend, workspace, slot = await _build_session(tmp_path)
    db = tmp_path / "sessions.sqlite"
    api = _Process(await _sqlite(db), workspace)
    closers = [backend.aclose, api.sp.aclose]
    if two_processes:
        other = WorkspaceBackendFactory.create(WorkspaceProvider(
            id="local-1", provider=WorkspaceProviderType.LOCAL, config=LocalWorkspaceConfig(root_path=str(tmp_path / "wsroot")),
        ))
        await other.initialize()
        worker = _Process(await _sqlite(db), await other.get(workspace.id, template=_template()))
        assert worker.workspace is not workspace and worker.sp is not api.sp
        closers += [other.aclose, worker.sp.aclose]
    else:
        worker = api
    fields: dict[str, Any] = {"status": SessionStatus.RUNNING, "started_at": NOW, **row_fields}
    row = WorkspaceSession(
        id=slot.session_id, workspace_id=workspace.id, binding=AgentSessionBinding(agent_id="researcher"), created_at=NOW, **fields,
    )
    await api.sessions.create(row)
    return api, worker, row, closers


async def _close(closers) -> None:
    for close in reversed(closers):
        await close()


async def _tool(process: _Process, tool: str, row: WorkspaceSession, event_bus=None) -> tuple[str, Any]:
    """The tool's answer as ``("ok", body)`` or ``(type, message)``."""
    result = await process.toolset(event_bus).call(tool_name=tool, arguments={"workspace_id": row.workspace_id, "session_id": row.id})
    body = json.loads(result.output)
    return (body["type"], body["message"]) if result.is_error else ("ok", body)


async def _rest(process: _Process, verb: str, row: WorkspaceSession) -> tuple[str, Any]:
    """The REST handler's answer as ``("ok", body)`` (``None`` for the pause's 204) or ``(type, message)``, the type the app maps the error to."""
    from primer.api.routers import sessions as routes

    try:
        if verb == "resume":
            out = await routes.resume_session(
                REQ, workspace_id=row.workspace_id, session_id=row.id, sessions=process.sessions, scheduler=process.scheduler,
                engine=process.engine,
            )
        else:
            out = await routes.pause_session(REQ, workspace_id=row.workspace_id, session_id=row.id, sessions=process.sessions)
    except ConflictError as exc:  # the app answers 409
        return "conflict", exc.message
    except NotFoundError as exc:  # the app answers 404
        return "not-found", exc.message
    return "ok", (out.model_dump(mode="json") if out is not None else None)


# ---- the probe: a pause through the tool, then the next turn the worker claims ---------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("two_processes", [False, True], ids=["one process", "another process"])
async def test_a_tool_pause_of_a_running_session_pauses_it_and_never_ends_it(two_processes, tmp_path, fake_event_bus):
    """The tool pauses a RUNNING session; the next turn the worker claims honours it: the session is PAUSED, not ENDED/completed, and the model is
    never called. The worker reads the pause off the row, whichever process served the tool call. On main: (ENDED, completed, 0 model calls) on one
    process, (WAITING, None, 1 model call) from another."""
    api, worker, row, closers = await _world(tmp_path, two_processes=two_processes)
    try:
        kind, body = await _tool(api, "pause_workspace_session", row, fake_event_bus)
        assert kind == "ok", body

        llm = _FakeLLM(scripts=[OK])
        await run_one_session_turn(_make_lease(row.id), worker.dispatch_deps(llm, fake_event_bus))

        after = await worker.sessions.get(row.id)
        assert (after.status, after.ended_reason, len(llm.calls)) == (SessionStatus.PAUSED, None, 0), "the pause ended the session or was not seen"
    finally:
        await _close(closers)


@pytest.mark.asyncio
@pytest.mark.parametrize("two_processes", [False, True], ids=["one process", "another process"])
async def test_the_rest_pause_of_a_running_session_pauses_it_and_never_ends_it(two_processes, tmp_path, fake_event_bus):
    """The probe's control: the REST route's pause, in the same worlds, already behaved (it writes ``pause_requested`` on the row)."""
    api, worker, row, closers = await _world(tmp_path, two_processes=two_processes)
    try:
        kind, body = await _rest(api, "pause", row)
        assert kind == "ok", body

        llm = _FakeLLM(scripts=[OK])
        await run_one_session_turn(_make_lease(row.id), worker.dispatch_deps(llm, fake_event_bus))

        after = await worker.sessions.get(row.id)
        assert (after.status, after.ended_reason, len(llm.calls)) == (SessionStatus.PAUSED, None, 0)
    finally:
        await _close(closers)


@pytest.mark.asyncio
@pytest.mark.parametrize("two_processes", [False, True], ids=["one process", "another process"])
async def test_a_tool_resume_after_a_tool_pause_runs_the_next_turn(two_processes, tmp_path, fake_event_bus):
    """The way back: the tool resumes the paused session on the caller's process, which arms the claim, and the worker's next turn runs."""
    api, worker, row, closers = await _world(tmp_path, two_processes=two_processes)
    try:
        assert (await _tool(api, "pause_workspace_session", row, fake_event_bus))[0] == "ok"
        llm = _FakeLLM(scripts=[OK])
        await run_one_session_turn(_make_lease(row.id), worker.dispatch_deps(llm, fake_event_bus))
        assert (await worker.sessions.get(row.id)).status == SessionStatus.PAUSED

        kind, body = await _tool(api, "resume_workspace_session", row, fake_event_bus)
        assert (kind, body["status"], body["pause_requested"]) == ("ok", "running", False), body
        assert (api.scheduler.enqueued, api.engine.upserts) == ([row.id], [row.id]), "the resume did not arm the session"
        await run_one_session_turn(_make_lease(row.id), worker.dispatch_deps(llm, fake_event_bus))

        after = await worker.sessions.get(row.id)
        assert (after.status, after.ended_reason, len(llm.calls)) == (SessionStatus.WAITING, None, 1), "the resumed session did not run its turn"
    finally:
        await _close(closers)


# ---- the tools answer as the REST routes do, in every state --------------------------------------------------------------------------------

#: state -> the row's fields. "never started" is a session created with ``auto_start: false``: CREATED, no ``started_at``.
STATES: dict[str, dict[str, Any]] = {
    "running": {"status": SessionStatus.RUNNING, "started_at": NOW, "turn_no": 1},
    "paused": {"status": SessionStatus.PAUSED, "started_at": NOW, "turn_no": 1},
    "waiting": {"status": SessionStatus.WAITING, "started_at": NOW, "turn_no": 1},
    "never started": {"status": SessionStatus.CREATED, "started_at": None},
    "ended": {"status": SessionStatus.ENDED, "ended_reason": "completed", "ended_at": NOW, "started_at": NOW, "turn_no": 1},
}
#: (verb, state) -> (the answer's kind, the row's status after, its pause flag after, whether the session was queued and its claim armed).
#: Resume STARTS a never-started session (the REST route's rule, which the tool now follows), and a running one is left exactly as it is.
EXPECTED: dict[tuple[str, str], tuple[str, SessionStatus, bool, bool]] = {
    ("resume", "running"): ("ok", SessionStatus.RUNNING, False, False),
    ("resume", "paused"): ("ok", SessionStatus.RUNNING, False, True),
    ("resume", "waiting"): ("ok", SessionStatus.RUNNING, False, True),
    ("resume", "never started"): ("ok", SessionStatus.RUNNING, False, True),
    ("resume", "ended"): ("conflict", SessionStatus.ENDED, False, False),
    ("pause", "running"): ("ok", SessionStatus.RUNNING, True, False),
    ("pause", "paused"): ("ok", SessionStatus.PAUSED, True, False),
    ("pause", "waiting"): ("ok", SessionStatus.PAUSED, False, False),
    ("pause", "never started"): ("ok", SessionStatus.PAUSED, False, False),
    ("pause", "ended"): ("conflict", SessionStatus.ENDED, False, False),
}


async def _effects(process: _Process, row: WorkspaceSession) -> dict[str, Any]:
    after = await process.sessions.get(row.id)
    return {
        "status": after.status, "pause_requested": after.pause_requested, "ended_reason": after.ended_reason,
        "workspace_refusal": after.workspace_refusal, "started": after.started_at is not None,
        "armed": (process.scheduler.enqueued, process.engine.upserts) == ([row.id], [row.id]),
        "untouched_queue": process.scheduler.enqueued == [] and process.engine.upserts == [],
    }


def _anonymous(answer: tuple[str, Any], row: WorkspaceSession) -> tuple[str, Any]:
    kind, detail = answer
    return kind, (detail.replace(row.id, "<sid>") if isinstance(detail, str) else detail)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", list(STATES))
@pytest.mark.parametrize("verb", ["resume", "pause"])
async def test_the_tool_answers_and_writes_exactly_as_the_rest_route_in_every_state(verb, state, tmp_path):
    """The same session, once through the REST handler and once through the tool: the same answer (an ok, or the same error type in the same
    words), the same row after and the same claim armed (or not). An ok tool answer is the session row as written (the REST resume's body)."""
    rest, _, rest_row, rest_closers = await _world(tmp_path / "rest", **STATES[state])
    tool, _, tool_row, tool_closers = await _world(tmp_path / "tool", **STATES[state])
    try:
        rest_answer = await _rest(rest, verb, rest_row)
        tool_answer = await _tool(tool, f"{verb}_workspace_session", tool_row)

        rest_effects, tool_effects = await _effects(rest, rest_row), await _effects(tool, tool_row)
        assert tool_effects == rest_effects, "the tool did not write (or arm) what the REST route did"
        assert _anonymous(tool_answer, tool_row)[0] == _anonymous(rest_answer, rest_row)[0], (rest_answer, tool_answer)
        if tool_answer[0] == "ok":
            assert tool_answer[1] == (await tool.sessions.get(tool_row.id)).model_dump(mode="json"), "the tool's answer is not the row"
            if verb == "resume":
                assert rest_answer[1] == (await rest.sessions.get(rest_row.id)).model_dump(mode="json")
        else:
            assert _anonymous(tool_answer, tool_row) == _anonymous(rest_answer, rest_row)

        kind, status, paused_flag, armed = EXPECTED[(verb, state)]
        assert (tool_answer[0], tool_effects["status"], tool_effects["pause_requested"], tool_effects["armed"]) == (kind, status, paused_flag, armed)
        assert armed or tool_effects["untouched_queue"]
        if kind == "conflict":
            assert tool_answer[1] == f"Session {tool_row.id!r} has ended"
        if (verb, state) == ("resume", "never started"):
            assert tool_effects["started"], "a started session has no started_at"
    finally:
        await _close(rest_closers)
        await _close(tool_closers)


@pytest.mark.asyncio
@pytest.mark.parametrize("verb", ["resume", "pause"])
async def test_an_unknown_session_is_answered_in_the_rest_route_s_words(verb, tmp_path):
    api, _, row, closers = await _world(tmp_path)
    try:
        ghost = row.model_copy(update={"id": "ses-ghost"})
        assert await _tool(api, f"{verb}_workspace_session", ghost) == await _rest(api, verb, ghost) == (
            "not-found", f"Session 'ses-ghost' does not exist on workspace {row.workspace_id!r}",
        )
    finally:
        await _close(closers)

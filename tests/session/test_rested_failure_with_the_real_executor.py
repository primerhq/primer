"""A session that rests after a failed model call can be continued, with the REAL agent executor and slot (C-024 slice 2, review of PR 623).

``WorkspaceAgentExecutor.invoke`` ends the on-disk slot (``session.json``) on every failed turn. When the row ended too, ``wake_session``'s ENDED
branch reopened the slot; a row that now RESTS takes the in-place branch, which never does. With the slot still ENDED the next send was a 409
("cannot append instruction to ENDED session"), a ``session_append`` trigger dropped its payload, a bare resume raised "cannot invoke ENDED
session" and ENDED the session, and a follow-up queued while the turn failed was deleted before ``wake_session`` raised, so it was lost. The fake
slot of the other tests hid all of it. These tests run the real executor over a real local workspace, for both shapes of an upstream 5xx: the
stream's own fatal Error, and the classified error an adapter RAISES before a stream opens.
"""

from __future__ import annotations

import shutil
from datetime import datetime, timezone

import pytest

from primer.agent.tool_manager import ToolExecutionManager
from primer.agent.workspace_executor import WorkspaceAgentExecutor
from primer.model.chat import Done, Error, TextDelta
from primer.model.except_ import ServerError
from primer.model.storage import OffsetPage
from primer.model.workspace_session import (
    AgentSessionBinding,
    PendingSessionMessage,
    SessionMessageKind,
    SessionStatus,
    WorkspaceSession,
)
from primer.session.dispatch import SessionDispatchDeps, run_one_session_turn
from primer.session.enqueue import SessionWakeDeps, wake_session
from primer.session.pending_messages import store_pending_steer
from tests.agent.test_workspace_executor import _agent, _build_session, _FakeLLM, _model
from tests.session.test_dispatch import (  # noqa: F401  (fixtures are used by name)
    FakeWorkspaceIO,
    _make_lease,
    fake_event_bus,
    fake_storage_provider,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="the workspace state repo needs git")

OK = [TextDelta(text="all good", index=0), Done(stop_reason="stop", raw_reason="stop")]


class _RaisingFirstCall(_FakeLLM):
    """The first model call RAISES a classified error before any stream exists (an upstream 500 once the retries are spent); later calls answer."""

    def stream(self, *, model, messages, **kwargs):
        self.calls.append({"model": model})
        index = self._cursor
        self._cursor += 1
        if index == 0:
            async def boom():
                raise ServerError("upstream 500", code="server_error")
                yield  # pragma: no cover

            return boom()
        return self._stream_impl(OK)


def _llm(shape: str) -> _FakeLLM:
    if shape == "stream_error":
        return _FakeLLM(scripts=[[Error(code="server_error", message="upstream 500", fatal=True)], OK])
    return _RaisingFirstCall(scripts=[[]])


class _Registry:
    def __init__(self, workspace) -> None:
        self._workspace = workspace

    async def get_workspace(self, workspace_id):
        return self._workspace

    async def get_workspace_row(self, workspace_id):
        return None


class _Scheduler:
    async def enqueue(self, session_id) -> None: ...


class _Engine:
    async def upsert(self, *args, **kwargs) -> None: ...


async def _start(tmp_path, fake_storage_provider):
    backend, workspace, slot = await _build_session(tmp_path)
    sid = slot.session_id
    row = WorkspaceSession(
        id=sid, workspace_id=workspace.id, binding=AgentSessionBinding(agent_id="researcher"),
        status=SessionStatus.RUNNING, created_at=datetime.now(timezone.utc), turn_status="running",
    )
    await fake_storage_provider.get_storage(WorkspaceSession).create(row)
    return backend, workspace, row


def _deps(workspace, llm, fake_storage_provider, fake_event_bus) -> SessionDispatchDeps:
    async def build(_session):
        slot = await workspace.get_session(_session.id)
        manager = ToolExecutionManager.for_workspace(toolset_providers={}, session=slot)
        return WorkspaceAgentExecutor(agent=_agent(), llm=llm, llm_model=_model(), tool_manager=manager, session=slot)

    return SessionDispatchDeps(
        storage_provider=fake_storage_provider, workspace_io=FakeWorkspaceIO(), event_bus=fake_event_bus, build_executor=build,
        scheduler=_Scheduler(), claim_engine=_Engine(), workspace_registry=_Registry(workspace),
    )


def _wake_deps(workspace, fake_storage_provider, fake_event_bus) -> SessionWakeDeps:
    return SessionWakeDeps(
        storage_provider=fake_storage_provider, scheduler=_Scheduler(), claim_engine=_Engine(),
        workspace_registry=_Registry(workspace), event_bus=fake_event_bus,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["stream_error", "raised"])
async def test_the_next_send_after_a_rested_failure_is_accepted_and_the_model_answers(shape, tmp_path, fake_storage_provider, fake_event_bus):
    backend, workspace, row = await _start(tmp_path, fake_storage_provider)
    try:
        llm = _llm(shape)
        deps = _deps(workspace, llm, fake_storage_provider, fake_event_bus)
        sessions = fake_storage_provider.get_storage(WorkspaceSession)

        await run_one_session_turn(_make_lease(row.id), deps)
        failed = await sessions.get(row.id)
        assert (failed.status, failed.ended_reason) == (SessionStatus.WAITING, None), "premise: the failure rested the session"
        assert failed.last_turn_error is not None and failed.last_turn_error.code == "server_error"
        assert await (await workspace.get_session(row.id)).status() != SessionStatus.ENDED, "the on-disk slot still says ENDED"

        await wake_session(
            workspace_id=workspace.id, session_id=row.id, instruction="try again", human_intent=True,
            deps=_wake_deps(workspace, fake_storage_provider, fake_event_bus),
        )
        outcome = await run_one_session_turn(_make_lease(row.id), deps)

        assert outcome.success is True and len(llm.calls) == 2, "the model was not called again"
        answered = await sessions.get(row.id)
        assert answered.last_turn_error is None and answered.ended_reason != "failed"
    finally:
        await backend.aclose()


@pytest.mark.asyncio
async def test_the_wake_reopens_a_slot_the_failure_exit_could_not(tmp_path, fake_storage_provider, fake_event_bus, monkeypatch):
    """The failure exit's slot reopen is bounded and best effort (the workspace can be reconnecting). When it did not land, the slot is still ENDED
    while the row rests, and the next send must reopen it itself, or it is a 409 on a session the row says is alive."""
    import primer.session.dispatch as dispatch_module

    async def reopen_that_does_not_land(executor) -> None:
        return None

    monkeypatch.setattr(dispatch_module, "_reopen_agent_session_slot", reopen_that_does_not_land)
    backend, workspace, row = await _start(tmp_path, fake_storage_provider)
    try:
        llm = _llm("raised")
        deps = _deps(workspace, llm, fake_storage_provider, fake_event_bus)
        await run_one_session_turn(_make_lease(row.id), deps)
        assert await (await workspace.get_session(row.id)).status() == SessionStatus.ENDED, "premise: the slot is still ENDED"

        await wake_session(
            workspace_id=workspace.id, session_id=row.id, instruction="try again", human_intent=True,
            deps=_wake_deps(workspace, fake_storage_provider, fake_event_bus),
        )
        outcome = await run_one_session_turn(_make_lease(row.id), deps)

        assert outcome.success is True and len(llm.calls) == 2, "the model was not called again"
    finally:
        await backend.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["stream_error", "raised"])
async def test_a_bare_resume_after_a_rested_failure_runs_the_turn_instead_of_ending_the_session(
    shape, tmp_path, fake_storage_provider, fake_event_bus,
):
    """A resume with no message (and a ``session_append`` trigger with an empty body) gets past the append and meets the slot when the next turn
    runs: with the slot ENDED that raised "cannot invoke ENDED session", which ended the session."""
    backend, workspace, row = await _start(tmp_path, fake_storage_provider)
    try:
        llm = _llm(shape)
        deps = _deps(workspace, llm, fake_storage_provider, fake_event_bus)
        sessions = fake_storage_provider.get_storage(WorkspaceSession)
        await run_one_session_turn(_make_lease(row.id), deps)

        await wake_session(
            workspace_id=workspace.id, session_id=row.id, instruction=None, human_intent=True,
            deps=_wake_deps(workspace, fake_storage_provider, fake_event_bus),
        )
        outcome = await run_one_session_turn(_make_lease(row.id), deps)

        after = await sessions.get(row.id)
        assert outcome.success is True and len(llm.calls) == 2, (outcome, after.status, after.ended_reason)
        assert after.status != SessionStatus.ENDED or after.ended_reason == "completed"
    finally:
        await backend.aclose()


@pytest.mark.asyncio
async def test_a_follow_up_queued_while_the_turn_failed_is_delivered_not_lost(tmp_path, fake_storage_provider, fake_event_bus):
    """A message typed while the failing turn ran is a pending steer, realized at the failure exit (``realize_next_pending`` deletes the pending
    row and then calls ``wake_session``): with the slot ENDED the wake raised AFTER the delete and the follow-up vanished."""
    backend, workspace, row = await _start(tmp_path, fake_storage_provider)
    try:
        llm = _FakeLLM(scripts=[[Error(code="server_error", message="upstream 500", fatal=True)], OK])
        deps = _deps(workspace, llm, fake_storage_provider, fake_event_bus)
        await store_pending_steer(
            storage_provider=fake_storage_provider, session=row, text="FOLLOW-UP typed during the turn",
            workspace_registry=_Registry(workspace),
        )

        await run_one_session_turn(_make_lease(row.id), deps)

        left = await fake_storage_provider.get_storage(PendingSessionMessage).find(None, OffsetPage(offset=0, length=50))
        assert list(left.items) == [], "the pending follow-up is still queued (or was dropped)"
        lines = (await workspace.state_repo.read_state_file(f"sessions/{row.id}/messages.jsonl")).decode()
        assert SessionMessageKind.USER_INPUT.value in lines and "FOLLOW-UP typed during the turn" in lines, (
            "the follow-up was never written as the user's next message"
        )
    finally:
        await backend.aclose()

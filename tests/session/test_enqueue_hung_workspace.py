"""A workspace that never answers is a 503, not "the workspace was removed" (ticket 01a11b58, review of #545).

``wake_session`` writes the instruction's USER_INPUT record through a short-lived ``WorkspaceMessageWriter`` and turns an ``OSError`` into a
404 ("workspace was removed while writing"). The writer's give-up error is a ``TimeoutError``, which IS an ``OSError``, so it was reported
as a missing workspace. It is a ``WorkspaceUnreachableError`` (503) and passes through.
"""

from __future__ import annotations

import asyncio

import pytest

from primer.model.except_ import NotFoundError, WorkspaceUnreachableError
from primer.model.workspace_session import SessionStatus
from primer.session import persistence
from primer.session.enqueue import wake_session
from tests.session.test_enqueue import _deps, _row

HARD_BOUND_S = 30.0


async def test_wake_session_reports_a_workspace_that_never_answers_as_unreachable_not_removed(monkeypatch) -> None:
    monkeypatch.setattr(persistence, "_WRITE_TIMEOUT_S", 0.2, raising=False)
    deps, _slot, _sched, _eng = _deps(_row(SessionStatus.CREATED))
    release = asyncio.Event()

    async def never_answers(session_id: str, line: bytes) -> None:
        await release.wait()

    workspace = await deps.workspace_registry.get_workspace("ws-1")
    workspace.append_message_line = never_answers
    try:
        async with asyncio.timeout(HARD_BOUND_S):
            with pytest.raises(Exception) as raised:
                await wake_session(workspace_id="ws-1", session_id="sess-1", instruction="hello", human_intent=True, deps=deps)
    finally:
        release.set()

    assert not isinstance(raised.value, NotFoundError), f"a workspace that did not answer was reported removed: {raised.value!r}"
    assert isinstance(raised.value, WorkspaceUnreachableError), f"expected the 503 error, got {raised.value!r}"

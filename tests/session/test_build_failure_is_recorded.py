"""A turn whose executor cannot be built says why on the row and in the transcript, instead of ending with a bare "The turn failed."

The first message of a main operator session on a sandbox workspace died with ``NotFoundError: On-disk session slot ... missing``: the worker logged it, the session ended
``failed``, and the console had nothing to show (``last_turn_error`` was null). A build failure is recorded like a refused run: ``last_turn_error`` (code
``executor_build_failed``) and an ERROR record whose message is the exception's own words (``to_problem_details``: credentials redacted, no traceback).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from primer.int.claim import ClaimKind, Lease
from primer.model.except_ import NotFoundError
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.session.dispatch import SessionDispatchDeps, run_one_session_turn
from tests.conftest import _FakeStorageProvider
from tests.session.test_dispatch import FakeWorkspaceIO


async def _run(build):
    sp = _FakeStorageProvider()
    now = datetime.now(UTC)
    await sp.get_storage(WorkspaceSession).create(WorkspaceSession(
        id="s1", workspace_id="w1", binding=AgentSessionBinding(agent_id="ag1"), status=SessionStatus.RUNNING, created_at=now, turn_status="claimable",
    ))
    io = FakeWorkspaceIO()
    await run_one_session_turn(
        Lease(kind=ClaimKind.SESSION, entity_id="s1", claimed_by="w", claimed_at=now, expires_at=now, attempt_count=1, last_error=None),
        SessionDispatchDeps(storage_provider=sp, workspace_io=io, event_bus=None, build_executor=build),
    )
    return await sp.get_storage(WorkspaceSession).get("s1"), [json.loads(line) for line in io.read_lines("s1")]


async def test_a_domain_error_at_build_time_is_recorded_with_its_words():
    async def build(session):
        raise NotFoundError("On-disk session slot for 's1' missing on workspace 'w1'")

    row, records = await _run(build)
    assert (row.status, row.ended_reason) == (SessionStatus.ENDED, "failed")
    assert row.last_turn_error is not None and row.last_turn_error.code == "executor_build_failed"
    [error] = [r for r in records if r["kind"] == "error"]
    assert error["payload"]["code"] == "executor_build_failed"
    assert "Not Found" in error["payload"]["message"] and "On-disk session slot" in error["payload"]["message"]


async def test_an_unexpected_error_at_build_time_shows_its_message_with_credentials_redacted_and_no_traceback():
    async def build(session):
        raise RuntimeError("could not connect to postgres://app:s3cr3tpw@db.internal/primer")

    row, records = await _run(build)
    assert row.last_turn_error is not None and row.last_turn_error.code == "executor_build_failed"
    [error] = [r for r in records if r["kind"] == "error"]
    message = error["payload"]["message"]
    assert "RuntimeError" in message and "could not connect to" in message and "db.internal" in message
    assert "s3cr3tpw" not in message and "Traceback" not in message


async def test_a_builder_that_returns_no_executor_says_so():
    async def build(session):
        return None

    row, records = await _run(build)
    assert row.last_turn_error is not None and row.last_turn_error.code == "executor_unavailable"
    [error] = [r for r in records if r["kind"] == "error"]
    assert "no executor" in error["payload"]["message"]

"""A turn whose run is refused says why (review of #853, N5).

``resolve_run_principal`` refuses a run whose creator was deleted or disabled, or whose API token was revoked or has expired (``RunRefused``). The
turn's executor cannot be built, so the turn ends the session ``failed``, as for any build failure; it now also writes an ERROR record with a fixed
message that names nobody and stamps ``last_turn_error`` (code ``run_refused``), and logs a warning without a traceback: it is not a fault.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from primer.authz import RUN_REFUSED_MESSAGE, RunRefused
from primer.int.claim import ClaimKind, Lease
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.session.dispatch import SessionDispatchDeps, run_one_session_turn
from tests.conftest import _FakeStorageProvider
from tests.session.test_dispatch import FakeWorkspaceIO


async def test_a_refused_turn_writes_an_error_record_and_last_turn_error_and_logs_a_warning(caplog):
    sp = _FakeStorageProvider()
    now = datetime.now(UTC)
    await sp.get_storage(WorkspaceSession).create(WorkspaceSession(
        id="s1", workspace_id="w1", binding=AgentSessionBinding(agent_id="ag1"), status=SessionStatus.RUNNING, created_at=now,
        turn_status="claimable",
    ))
    io = FakeWorkspaceIO()

    async def _refused_build(session):
        raise RunRefused(f"session {session.id!r} does not run: the account or API token that started it is deleted, disabled, revoked or expired")

    with caplog.at_level("WARNING"):
        await run_one_session_turn(
            Lease(kind=ClaimKind.SESSION, entity_id="s1", claimed_by="w", claimed_at=now, expires_at=now, attempt_count=1, last_error=None),
            SessionDispatchDeps(storage_provider=sp, workspace_io=io, event_bus=None, build_executor=_refused_build),
        )

    row = await sp.get_storage(WorkspaceSession).get("s1")
    assert (row.status, row.ended_reason) == (SessionStatus.ENDED, "failed")
    assert row.last_turn_error is not None and row.last_turn_error.code == "run_refused"
    errors = [json.loads(line) for line in io.read_lines("s1") if json.loads(line)["kind"] == "error"]
    assert [e["payload"] for e in errors] == [{"message": RUN_REFUSED_MESSAGE, "code": "run_refused"}]
    assert errors[0]["seq"] > 0
    refused = [r for r in caplog.records if "does not run" in r.getMessage()]
    assert refused and all(r.levelname == "WARNING" and r.exc_info is None for r in refused)
    assert not [r for r in caplog.records if "failed to build executor" in r.getMessage()]

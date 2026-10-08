"""The release marker reserves its seq through the release's OWN transaction, on a live Postgres (ticket 01a11cd8).

``SessionClaimAdapter.on_release`` runs inside the engine's release transaction, which already holds the session row's lock from the field
patch the release itself makes. The marker's seq reservation is one more ``patch_if`` of that row: through the transaction it is part of it;
through another connection it would wait for a lock the release is not going to give up until this very call returns. The in-memory storage
cannot tell the two apart, so this runs on Postgres (gate: ``PRIMER_TEST_POSTGRES_URL``, see ``tests/pg_gate.py``).
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import pytest

from primer.claim.adapters.sessions import SessionClaimAdapter
from primer.claim.postgres import PostgresClaimEngine
from primer.int.claim import ClaimKind, ReleaseOutcome
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from tests.claim.test_postgres_engine import pg_storage  # noqa: F401  (the fixture is used by name)
from tests.pg_gate import needs_postgres
from tests.session.test_dispatch import FakeWorkspaceIO

_needs_pg = needs_postgres("Postgres session release-marker tests")

SID = "s-release-marker-seq"


class _Registry:
    def __init__(self, workspace) -> None:
        self._workspace = workspace

    async def get_workspace(self, workspace_id):
        return self._workspace


@_needs_pg
@pytest.mark.asyncio
async def test_a_failed_release_reserves_the_marker_seq_inside_its_own_transaction(pg_storage):  # noqa: F811
    sessions = pg_storage.get_storage(WorkspaceSession)
    await sessions.delete(SID) if await sessions.get(SID) is not None else None
    await sessions.create(WorkspaceSession(
        id=SID, workspace_id="w1", binding=AgentSessionBinding(agent_id="a1"), status=SessionStatus.RUNNING,
        created_at=datetime.now(UTC), last_seq=4,
    ))
    workspace = FakeWorkspaceIO()
    adapter = SessionClaimAdapter(session_storage=sessions, workspace_registry=_Registry(workspace))
    engine = PostgresClaimEngine(storage_provider=pg_storage, adapters={ClaimKind.SESSION: adapter})
    try:
        await engine.upsert(ClaimKind.SESSION, SID)
        [lease] = await engine.claim_due("worker-A", max_count=1)

        # a wait on the transaction's own row lock would never end: bound the release instead of hanging the lane
        async with asyncio.timeout(20):
            await engine.release(lease, outcome=ReleaseOutcome(success=False, drop_lease=True))

        records = [json.loads(line) for line in workspace.read_lines(SID)]
        assert [(r["seq"], r["kind"]) for r in records] == [(5, "error")], records
        assert records[0]["payload"]["terminal"] is True
        assert (await sessions.get(SID)).last_seq == 5
    finally:
        await sessions.delete(SID)

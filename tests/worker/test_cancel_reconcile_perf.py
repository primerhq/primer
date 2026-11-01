"""PERF-T10 (L8) perf: the cancel reconciler reads the rows of every active scope in one read per interval, not one per scope.

The reconciler's behaviour (a cancel through the row reaches the turn exactly once, a storage error for one session does
not stop the others, no read inside the heartbeat) is pinned by ``tests/worker/test_cancel_reconcile.py`` and stays green
through the change (that per-scope read path remains the fallback when the batched read is unavailable). The perf half is
RED on the per-scope reads (one session get per active scope) and green once the interval issues one read for all scopes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from primer.int.claim import ClaimKind
from primer.model.provider import SqliteConfig
from primer.model.scheduler import WorkerConfig
from primer.model.workspace_session import AgentSessionBinding, SessionStatus, WorkspaceSession
from primer.storage.sqlite import SqliteStorageProvider
from primer.worker.pool import WorkerPool
from primer.worker.turn import _CancelScope
from tests.perf.counting import CountingStorageProvider

SESSION = ClaimKind.SESSION
N = 30


async def test_the_reconcile_of_30_cancels_reads_the_rows_once(tmp_path: Path):
    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "t.sqlite"))
    await sp.initialize()
    sessions = sp.get_storage(WorkspaceSession)
    now = datetime.now(timezone.utc)
    sids = [f"s-perf-{i}" for i in range(N)]
    for sid in sids:
        await sessions.create(
            WorkspaceSession(
                id=sid, workspace_id="ws-perf", binding=AgentSessionBinding(agent_id="ag-perf"),
                status=SessionStatus.RUNNING, cancel_requested=True, created_at=now,
            )
        )

    csp = CountingStorageProvider(sp)
    pool = WorkerPool(
        config=WorkerConfig(concurrency=N, heartbeat_interval_seconds=1),
        scheduler=None,                            # type: ignore[arg-type]
        storage=csp,                               # type: ignore[arg-type]
        workspace_registry=None,                   # type: ignore[arg-type]
        provider_registry=None,                    # type: ignore[arg-type]
        engine=None,                               # type: ignore[arg-type]
    )
    for sid in sids:
        pool._active_scopes[(SESSION, sid)] = _CancelScope()

    with csp.counted() as c:
        reconciled = await pool._reconcile_cancels()

    assert reconciled == N
    # One ``id IN`` read for every active scope, not one get per scope.
    assert c.calls("get", "find", model="WorkspaceSession") <= 1
    await sp.aclose()

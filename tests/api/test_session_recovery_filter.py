"""Tests for ``recover_sessions`` (01a0518a edge #3 sweep finding).

Before the parked flip, "every non-ENDED session" was a reasonable
approximation of "sessions that crashed mid-turn or have pending work" -
a clean turn always ended the session, so anything else non-ENDED was
either RUNNING (crashed mid-turn) or a legitimate, narrow WAITING (an
assistant question, max_tokens). With ``_CLEAN_TURN_RESTS_PARKED``
resting a clean turn at WAITING instead, that same "non-ENDED" filter
would re-arm the claim engine (and, for RUNNING, the scheduler) for
essentially every agent session that ever completed a turn - firing a
genuine, wasted LLM call per row on every single process restart. The
fake-storage tests below pin the narrowed predicate directly: RUNNING
(this function's original purpose) or turn_status="claimable"
(independent evidence of real pending work) get recovered; a
merely-resting WAITING/idle session does not. The ``TestBootRecovery``
class at the bottom re-proves the same behavior through the REAL app
lifespan + a real SQLite DB (formerly a separate, confusingly
near-identically-named file - ``test_startup_session_recovery_filter.py``
- merged here after CI caught it still pinned the OLD, pre-flip
semantics; see d6cce8ad's CI-red report).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI

from primer.api.app import _build_storage_provider, _make_lifespan
from primer.api.config import AppConfig
from primer.model.provider import (
    SqliteConfig,
    StorageProviderConfig,
    StorageProviderType,
)
from primer.model.scheduler import (
    InMemorySchedulerConfig,
    RuntimeMode,
    SchedulerProviderConfig,
    SchedulerProviderType,
)
from primer.model.workspace_session import (
    AgentSessionBinding,
    SessionStatus,
    WorkspaceSession,
)


class _RecordingClaimEngine:
    def __init__(self) -> None:
        self.upserted: list[str] = []

    async def upsert(self, kind: Any, entity_id: str) -> None:
        self.upserted.append(entity_id)


class _RecordingScheduler:
    def __init__(self) -> None:
        self.enqueued: list[str] = []

    async def enqueue(self, session_id: str) -> None:
        self.enqueued.append(session_id)


def _sess(
    id: str, *, status: SessionStatus, turn_status: str = "idle", turn_no: int = 0,
) -> WorkspaceSession:
    return WorkspaceSession(
        id=id, workspace_id="w1",
        binding=AgentSessionBinding(agent_id="ag1"),
        status=status, created_at=datetime.now(UTC),
        turn_status=turn_status, turn_no=turn_no,
    )


@pytest.mark.asyncio
async def test_running_is_recovered_and_enqueued(fake_storage_provider):
    from primer.api._app_lifespan_phases import recover_sessions

    storage = fake_storage_provider.get_storage(WorkspaceSession)
    await storage.create(_sess("s-running", status=SessionStatus.RUNNING, turn_status="running"))

    claim_engine = _RecordingClaimEngine()
    scheduler = _RecordingScheduler()
    await recover_sessions(claim_engine, scheduler, fake_storage_provider)

    assert claim_engine.upserted == ["s-running"]
    assert scheduler.enqueued == ["s-running"]


@pytest.mark.asyncio
async def test_claimable_non_running_is_recovered_without_enqueue(fake_storage_provider):
    """Independent evidence of pending work (a queued steer/resume not
    yet picked up) still needs a lease, regardless of status - but only
    RUNNING gets the extra scheduler nudge."""
    from primer.api._app_lifespan_phases import recover_sessions

    storage = fake_storage_provider.get_storage(WorkspaceSession)
    await storage.create(_sess(
        "s-claimable", status=SessionStatus.WAITING, turn_status="claimable", turn_no=1,
    ))

    claim_engine = _RecordingClaimEngine()
    scheduler = _RecordingScheduler()
    await recover_sessions(claim_engine, scheduler, fake_storage_provider)

    assert claim_engine.upserted == ["s-claimable"]
    assert scheduler.enqueued == []


@pytest.mark.asyncio
async def test_resting_parked_session_is_not_recovered(fake_storage_provider):
    """01a0518a: a session that cleanly finished a turn and now rests at
    WAITING/idle (session_state="parked") has no pending work - it must
    NOT be re-armed on every restart. wake_session already re-arms it
    itself the moment a real new message actually arrives."""
    from primer.api._app_lifespan_phases import recover_sessions

    storage = fake_storage_provider.get_storage(WorkspaceSession)
    await storage.create(_sess(
        "s-parked", status=SessionStatus.WAITING, turn_status="idle", turn_no=1,
    ))

    claim_engine = _RecordingClaimEngine()
    scheduler = _RecordingScheduler()
    await recover_sessions(claim_engine, scheduler, fake_storage_provider)

    assert claim_engine.upserted == []
    assert scheduler.enqueued == []


@pytest.mark.asyncio
async def test_ended_session_is_never_recovered(fake_storage_provider):
    from primer.api._app_lifespan_phases import recover_sessions

    storage = fake_storage_provider.get_storage(WorkspaceSession)
    await storage.create(_sess("s-ended", status=SessionStatus.ENDED, turn_status="idle", turn_no=1))

    claim_engine = _RecordingClaimEngine()
    scheduler = _RecordingScheduler()
    await recover_sessions(claim_engine, scheduler, fake_storage_provider)

    assert claim_engine.upserted == []
    assert scheduler.enqueued == []


# ===========================================================================
# Real lifespan-boot integration - same behavior as above, proven through
# the actual app lifespan + a real SQLite DB rather than fake storage.
# ===========================================================================


def _boot_session(
    sid: str, status: SessionStatus, turn_status: str = "idle",
) -> WorkspaceSession:
    return WorkspaceSession(
        id=sid, workspace_id="ws-1",
        binding=AgentSessionBinding(agent_id="agent-1"),
        status=status, created_at=datetime.now(UTC),
        turn_status=turn_status,
    )


def _boot_app_config(db_path: Path) -> AppConfig:
    return AppConfig(
        runtime_mode=RuntimeMode.API,
        db=StorageProviderConfig(
            provider=StorageProviderType.SQLITE,
            config=SqliteConfig(path=db_path),
        ),
        scheduler=SchedulerProviderConfig(
            provider=SchedulerProviderType.IN_MEMORY,
            config=InMemorySchedulerConfig(),
        ),
    )


async def _seed_boot_matrix(db_path: Path) -> None:
    """One row per (status, turn_status) combination recovery must
    distinguish - the real, on-disk-persisted-then-rebooted counterpart
    of the fake-storage matrix exercised above."""
    provider = _build_storage_provider(_boot_app_config(db_path))
    await provider.initialize()
    storage = provider.get_storage(WorkspaceSession)
    try:
        await storage.create(_boot_session("s-running", SessionStatus.RUNNING, "running"))
        await storage.create(_boot_session("s-claimable", SessionStatus.WAITING, "claimable"))
        await storage.create(_boot_session("s-resting-waiting", SessionStatus.WAITING, "idle"))
        await storage.create(_boot_session("s-paused-idle", SessionStatus.PAUSED, "idle"))
        await storage.create(_boot_session("s-created", SessionStatus.CREATED, "idle"))
        await storage.create(_boot_session("s-ended", SessionStatus.ENDED, "idle"))
    finally:
        await provider.aclose()


class TestBootRecovery:
    @pytest.mark.asyncio
    async def test_uses_find_not_list(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Structural check carried over from the pre-01a0518a version of
        this test: recovery must query via find() (index-friendly,
        paged), never list() (would load the entire session history into
        memory - the exact scaling promise recover_sessions' own
        docstring makes)."""
        monkeypatch.setenv("HOME", str(tmp_path))
        db_path = tmp_path / "recovery.sqlite"
        await _seed_boot_matrix(db_path)

        captured: dict[str, object] = {}
        probe_provider = _build_storage_provider(_boot_app_config(db_path))
        await probe_provider.initialize()
        handle = probe_provider.get_storage(WorkspaceSession)
        orig_find = handle.find
        orig_list = handle.list

        async def spy_find(predicate, page, **kw):  # type: ignore[no-untyped-def]
            captured["found"] = True
            return await orig_find(predicate, page, **kw)

        async def spy_list(page, **kw):  # type: ignore[no-untyped-def]
            captured["list_called"] = True
            return await orig_list(page, **kw)

        handle.find = spy_find  # type: ignore[assignment]
        handle.list = spy_list  # type: ignore[assignment]
        monkeypatch.setattr(
            "primer.api.app._build_storage_provider", lambda cfg: probe_provider,
        )

        app = FastAPI(lifespan=_make_lifespan(_boot_app_config(db_path)))
        async with app.router.lifespan_context(app):
            pass

        assert captured.get("found"), "recovery should call find() on the session storage"
        assert "list_called" not in captured, "recovery must never call list() (unbounded load)"

    @pytest.mark.asyncio
    async def test_rearms_only_running_or_claimable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """01a0518a, proven through the real boot path: narrowed from
        "every non-ENDED status" (pre-flip) to RUNNING or
        turn_status="claimable" only. A resting WAITING/idle session -
        the exact shape a clean, parked-flip turn now leaves behind -
        must NOT be re-armed on every restart, nor must a merely-CREATED
        or idle-PAUSED row that never had a lease to begin with."""
        monkeypatch.setenv("HOME", str(tmp_path))
        db_path = tmp_path / "recovery2.sqlite"
        await _seed_boot_matrix(db_path)

        app = FastAPI(lifespan=_make_lifespan(_boot_app_config(db_path)))
        async with app.router.lifespan_context(app):
            engine = app.state.claim_engine
            from primer.int.claim import ClaimKind

            armed = {
                entity_id
                for (kind, entity_id) in engine._leases  # noqa: SLF001
                if kind == ClaimKind.SESSION
            }

        assert armed == {"s-running", "s-claimable"}, armed

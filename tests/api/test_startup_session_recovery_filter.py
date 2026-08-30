"""Startup session recovery re-arms only genuinely recoverable sessions.

Recovery used to ``list()`` every session row and drop ENDED in Python
(an OOM risk at scale), then ``find()`` with a broad non-ENDED predicate.
Since completed turns rest parked (the clean-turn-rests-parked flip), a
broad predicate would re-arm every session that ever completed a turn on
every restart -- one wasted LLM call each. Recovery now selects only
``status == RUNNING`` (a worker died mid-turn) or
``turn_status == "claimable"`` (real pending work).

Per-case unit coverage of that predicate lives in
``test_session_recovery_filter.py``. These tests keep the FULL-LIFESPAN
angle: boot the real lifespan against a tmp SQLite DB with an in-memory
scheduler, seed a mix of statuses, and assert:

* recovery filters in the database via ``find`` (never ``list``), and
* end to end, exactly the recoverable sessions get re-armed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

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


def _session(
    sid: str, status: SessionStatus, turn_status: str = "idle"
) -> WorkspaceSession:
    return WorkspaceSession(
        id=sid,
        workspace_id="ws-1",
        binding=AgentSessionBinding(agent_id="agent-1"),
        status=status,
        turn_status=turn_status,
        created_at=datetime.now(UTC),
    )


def _app_config(db_path: Path) -> AppConfig:
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


async def _seed(db_path: Path) -> None:
    """Populate the DB with one session per status before lifespan runs."""
    provider = _build_storage_provider(_app_config(db_path))
    await provider.initialize()
    storage = provider.get_storage(WorkspaceSession)
    try:
        await storage.create(_session("s-created", SessionStatus.CREATED))
        await storage.create(_session("s-running", SessionStatus.RUNNING))
        await storage.create(_session("s-waiting", SessionStatus.WAITING))
        await storage.create(_session("s-paused", SessionStatus.PAUSED))
        await storage.create(_session("s-ended", SessionStatus.ENDED))
        await storage.create(
            _session(
                "s-claimable", SessionStatus.WAITING, turn_status="claimable"
            )
        )
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_recovery_uses_find_with_live_status_predicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    db_path = tmp_path / "recovery.sqlite"
    await _seed(db_path)

    captured: dict[str, object] = {}

    # Spy on the WorkspaceSession handle's find/list. The provider hands the
    # same cached handle to the lifespan, so wrapping it here observes the
    # exact calls recovery makes.
    probe_provider = _build_storage_provider(_app_config(db_path))
    await probe_provider.initialize()
    handle = probe_provider.get_storage(WorkspaceSession)
    orig_find = handle.find
    orig_list = handle.list

    async def spy_find(predicate, page, **kw):  # type: ignore[no-untyped-def]
        captured.setdefault("find_predicates", []).append(predicate)  # type: ignore[union-attr]
        return await orig_find(predicate, page, **kw)

    async def spy_list(page, **kw):  # type: ignore[no-untyped-def]
        captured["list_called"] = True
        return await orig_list(page, **kw)

    handle.find = spy_find  # type: ignore[assignment]
    handle.list = spy_list  # type: ignore[assignment]

    # Make create_app's lifespan reuse our probe provider so the spy sticks.
    monkeypatch.setattr(
        "primer.api.app._build_storage_provider",
        lambda cfg: probe_provider,
    )

    app = FastAPI(lifespan=_make_lifespan(_app_config(db_path)))
    async with app.router.lifespan_context(app):
        pass

    # Recovery queried via find(), never list(), for sessions: the filter
    # runs in the database, not in Python. The exact predicate values are
    # covered behaviorally below and per-case in
    # test_session_recovery_filter.py; asserting on predicate internals
    # here would just re-pin the Q object structure.
    preds = captured.get("find_predicates")
    assert preds, "recovery should call find() on the session storage"
    assert "list_called" not in captured, (
        "recovery must not list() the whole session table"
    )


@pytest.mark.asyncio
async def test_recovery_rearms_only_live_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    db_path = tmp_path / "recovery2.sqlite"
    await _seed(db_path)

    app = FastAPI(lifespan=_make_lifespan(_app_config(db_path)))
    async with app.router.lifespan_context(app):
        engine = app.state.claim_engine
        from primer.int.claim import ClaimKind

        # Only genuinely recoverable sessions are re-armed: a RUNNING row
        # (worker died mid-turn) and a claimable one (pending work). A
        # resting/idle WAITING row rests parked - re-arming it on every
        # boot would fire a wasted LLM call per historical session.
        # InMemoryClaimEngine holds armed leases keyed by (kind, entity_id).
        armed = {
            entity_id
            for (kind, entity_id) in engine._leases  # noqa: SLF001
            if kind == ClaimKind.SESSION
        }

    assert armed == {"s-running", "s-claimable"}, (
        f"expected exactly the recoverable sessions armed, got {armed}"
    )
    assert "s-ended" not in armed, "ENDED session must not be re-armed"

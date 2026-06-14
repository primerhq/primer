"""Startup session recovery filters to LIVE (non-ENDED) sessions.

Recovery used to ``list()`` every session row and drop ENDED in Python
(an OOM risk at scale). It now ``find()``s with a status-IN predicate so
the database only returns sessions that could still need work -- and the
new ``sessions.status`` B-tree index keeps that scan cheap.

These tests boot the real lifespan against a tmp SQLite DB (backend
agnostic: the predicate is the same on both backends) with an in-memory
scheduler so a claim engine exists, seed a mix of statuses, and assert:

* recovery calls ``find`` (not ``list``) with a predicate that selects
  every non-ENDED status, and
* only the live sessions get re-armed on the claim engine.
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


def _session(sid: str, status: SessionStatus) -> WorkspaceSession:
    return WorkspaceSession(
        id=sid,
        workspace_id="ws-1",
        binding=AgentSessionBinding(agent_id="agent-1"),
        status=status,
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

    # Recovery queried via find(), never list(), for sessions.
    preds = captured.get("find_predicates")
    assert preds, "recovery should call find() on the session storage"

    # The predicate must select every NON-ended status (fail-safe filter).
    def _collect_values(p):  # type: ignore[no-untyped-def]
        vals: set[str] = set()
        right = getattr(p, "right", None)
        rv = getattr(right, "value", None)
        if isinstance(rv, list):
            vals.update(str(v) for v in rv)
        elif rv is not None:
            vals.add(str(rv))
        return vals

    status_pred = next(
        (
            p
            for p in preds
            if getattr(getattr(p, "left", None), "name", None) == "status"
        ),
        None,
    )
    assert status_pred is not None, "recovery must filter on the status field"
    selected = _collect_values(status_pred)
    expected_live = {
        s.value for s in SessionStatus if s != SessionStatus.ENDED
    }
    assert selected == expected_live
    assert SessionStatus.ENDED.value not in selected


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

        # The four live sessions are claimable; the ENDED one is not re-armed.
        # InMemoryClaimEngine holds armed leases keyed by (kind, entity_id).
        armed = {
            entity_id
            for (kind, entity_id) in engine._leases  # noqa: SLF001
            if kind == ClaimKind.SESSION
        }

    live = {"s-created", "s-running", "s-waiting", "s-paused"}
    assert live <= armed, f"expected all live sessions armed, got {armed}"
    assert "s-ended" not in armed, "ENDED session must not be re-armed"

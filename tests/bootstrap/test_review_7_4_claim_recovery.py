"""Review 7.4 (round 1, O-3): a boot that dies holding the default-workspace claim, or a holder whose materialise fails, must not leave the
install without its default workspace until somebody restarts it."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import pytest

from primer.bootstrap import seed
from primer.bootstrap.defaults import RESERVED_LOCAL_WORKSPACE_TEMPLATE, RESERVED_WORKSPACE_TEMPLATES
from primer.bootstrap.seed import (
    SeedMarker, _claim_default_workspace, _release_claim, cancel_default_workspace_retries, ensure_default_workspace, run_ensure_pass,
    start_default_workspace_retry,
)
from primer.model.model_profile import ModelProfile
from primer.model.workspace import Workspace, WorkspaceRuntimeMeta, WorkspaceTemplate

CLAIM = "default-workspace-prime"


class _Live:
    def __init__(self, workspace_id):
        self.id = workspace_id
        self.runtime_meta = WorkspaceRuntimeMeta(url="ws://127.0.0.1:5959", token="stub-token")


class _Registry:
    def __init__(self, fail_first: bool = False) -> None:
        self.calls = 0
        self._fail_first = fail_first

    async def materialise(self, *, template, overrides=None, workspace_id=None):
        self.calls += 1
        if self._fail_first and self.calls == 1:
            await asyncio.sleep(0.02)
            raise RuntimeError("init command failed")
        return _Live(workspace_id)

    async def get_workspace(self, workspace_id):
        raise NotImplementedError


async def _template(sp):
    await sp.get_storage(WorkspaceTemplate).create(
        WorkspaceTemplate(**RESERVED_WORKSPACE_TEMPLATES[RESERVED_LOCAL_WORKSPACE_TEMPLATE])
    )


async def _row(sp):
    return await sp.get_storage(Workspace).get("prime")


@pytest.fixture(autouse=True)
def _short_ttl(monkeypatch):
    monkeypatch.setattr(seed, "DEFAULT_WORKSPACE_CLAIM_TTL", timedelta(seconds=0.4))


async def test_a_boot_that_died_holding_the_claim_is_recovered_once_the_claim_is_stale(fake_storage_provider, caplog):
    sp = fake_storage_provider
    await _template(sp)
    await sp.get_storage(SeedMarker).create(SeedMarker(id=CLAIM, claimed_at=datetime.now(timezone.utc)))     # its holder was killed
    registry = _Registry()
    with caplog.at_level(logging.WARNING, logger="primer.bootstrap.seed"):
        assert await ensure_default_workspace(sp, workspace_registry=registry) is None
    assert any("not created yet" in r.getMessage() and r.levelno == logging.WARNING for r in caplog.records), "a deferral is loud"
    assert registry.calls == 0 and await _row(sp) is None

    task = start_default_workspace_retry(sp, workspace_registry=registry, interval=timedelta(seconds=0.1))
    await asyncio.wait_for(task, timeout=10)
    assert await _row(sp) is not None, "the retry took the stale claim over and created the workspace"
    assert registry.calls == 1
    assert await sp.get_storage(SeedMarker).get(CLAIM) is None, "the claim is released"


async def test_a_deferring_boot_retries_after_the_winner_fails_to_materialise(fake_storage_provider):
    sp = fake_storage_provider
    await _template(sp)
    registry = _Registry(fail_first=True)
    winner, loser = await asyncio.gather(
        ensure_default_workspace(sp, workspace_registry=registry),
        ensure_default_workspace(sp, workspace_registry=registry),
        return_exceptions=True,
    )
    assert sum(isinstance(r, RuntimeError) for r in (winner, loser)) == 1
    assert await _row(sp) is None, "neither created the row this boot"
    task = start_default_workspace_retry(sp, workspace_registry=registry, interval=timedelta(seconds=0.05))
    await asyncio.wait_for(task, timeout=10)
    assert await _row(sp) is not None


async def test_run_ensure_pass_schedules_the_retry_when_it_defers(fake_storage_provider):
    sp = fake_storage_provider
    sp.get_storage(ModelProfile)._data.clear()
    await _template(sp)
    await sp.get_storage(SeedMarker).create(SeedMarker(id=CLAIM, claimed_at=datetime.now(timezone.utc)))
    result = await run_ensure_pass(sp, workspace_registry=_Registry())
    assert "prime" not in result.created
    assert seed._retry_tasks, "a deferred default workspace is retried in the background"
    await cancel_default_workspace_retries()
    assert not seed._retry_tasks, "the lifespan can stop it at shutdown"


async def test_a_release_deletes_only_its_own_claim(fake_storage_provider):
    claims = fake_storage_provider.get_storage(SeedMarker)
    mine, _ = await _claim_default_workspace(claims, CLAIM)
    assert mine is not None
    # The holder outlived the TTL and a taker now holds the claim (a different stamp).
    taken = await claims.patch_if(CLAIM, {"claimed_at": (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat()}, where={"claimed_at": [mine]})
    assert taken is not None
    await _release_claim(claims, CLAIM, mine)
    assert await claims.get(CLAIM) is not None, "the taker's claim survives the slow holder's release"
    from primer.storage import raw_generation

    await _release_claim(claims, CLAIM, raw_generation(taken, "claimed_at"))
    assert await claims.get(CLAIM) is None, "a release with the current holder's own stamp deletes it"


@pytest.mark.asyncio
async def test_the_lifespan_shutdown_cancels_a_pending_default_workspace_retry(tmp_path):
    """Round 1b: the retry task is owned by the lifespan. Booting and shutting down the real app while one is pending must leave nothing
    running (the teardown cancels it via ``cancel_default_workspace_retries``)."""
    from pathlib import Path

    from tests.bootstrap.test_lifespan_integration import _boot_app

    async with _boot_app(auto_bootstrap=True, db_path=Path(tmp_path) / "p.sqlite") as handle:
        # The boot's own ensure pass ran; simulate the deferral it logs about by planting a pending retry task (its first attempt sleeps
        # a long interval away, so nothing runs while the app is being torn down).
        task = start_default_workspace_retry(
            handle.app.state.storage_provider,
            workspace_registry=handle.app.state.workspace_registry,
            interval=timedelta(seconds=300),
        )
        assert task in seed._retry_tasks and not task.done()

    # The real lifespan teardown has finished: the pending retry is cancelled and untracked.
    assert task.cancelled(), "shutdown cancelled the retry task"
    assert not seed._retry_tasks, "no retry task outlives the app"

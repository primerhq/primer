"""The real lifespan wires the url-source guard from the config (lead review of #473, round 2).

``configure_allow_private_destinations(config.workspace_allow_private_url_sources)`` is the only thing that sets the
process-wide switch. Nothing else checked it: a lifespan that passed ``True`` would turn the guard off on every
deployment and every other test would stay green. This boots the real lifespan (as test_lifespan_teardown.py does)
twice: with the default config the guard is ON, with the setting true it is OFF. Before each boot the switch is put in
the OPPOSITE state, so only the lifespan can have put it where the assertion finds it.
"""

from __future__ import annotations

import pytest

from primer.api.app import create_app
from primer.api.config import AppConfig
from primer.common import ssrf
from primer.common.ssrf import BlockedDestinationError, refuse_private_literal
from primer.model.scheduler import RuntimeMode
from tests.api.conftest import _FakeStorageProvider

_LOOPBACK = "http://127.0.0.1:36471/seed-content"


@pytest.fixture(autouse=True)
def _restore_the_switch():
    yield
    ssrf.configure_allow_private_destinations(False)


async def _boot(monkeypatch: pytest.MonkeyPatch, cfg: AppConfig, *, check) -> None:
    storage = _FakeStorageProvider()
    monkeypatch.setattr("primer.api.app._build_storage_provider", lambda _cfg: storage)
    app = create_app(cfg)
    async with app.router.lifespan_context(app):
        check()


@pytest.mark.asyncio
async def test_the_default_config_boots_with_the_guard_on(monkeypatch: pytest.MonkeyPatch) -> None:
    ssrf.configure_allow_private_destinations(True)  # the opposite of what the boot must leave

    def _check() -> None:
        with pytest.raises(BlockedDestinationError):
            refuse_private_literal(_LOOPBACK)

    await _boot(monkeypatch, AppConfig(runtime_mode=RuntimeMode.API_PLUS_WORKER, scheduler=None), check=_check)


@pytest.mark.asyncio
async def test_the_opt_in_boots_with_the_guard_off(monkeypatch: pytest.MonkeyPatch) -> None:
    ssrf.configure_allow_private_destinations(False)  # the opposite of what the boot must leave

    def _check() -> None:
        refuse_private_literal(_LOOPBACK)  # does not raise

    cfg = AppConfig(
        runtime_mode=RuntimeMode.API_PLUS_WORKER, scheduler=None, workspace_allow_private_url_sources=True,
    )
    await _boot(monkeypatch, cfg, check=_check)

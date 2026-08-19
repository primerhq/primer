"""S6 P3: the inbound channel path carries live session collaborators.

Spec: docs/superpowers/ux-revamp/10-s6-design.md section 5. A thread that
creates a session needs a workspace_registry to allocate its on-disk slot,
and a thread that steers one needs it again to append the instruction. The
adapters used to carry neither, so the whole thread path would have failed
with dispatch_failed in production while every unit test injected a stub.
"""

from __future__ import annotations

from primer.channel.correlation import CorrelationStore
from primer.channel.inbound_router import ChannelInboundRouter
from tests.conftest import _FakeStorageProvider


class _Registry:
    async def get_workspace(self, workspace_id: str):
        return None


class _Scheduler:
    async def enqueue(self, session_id: str) -> None:
        return None


def test_router_accepts_and_stores_the_session_wiring():
    sp = _FakeStorageProvider()
    registry, scheduler, artifacts = _Registry(), _Scheduler(), object()
    router = ChannelInboundRouter(
        sp,
        CorrelationStore(sp),
        workspace_registry=registry,
        scheduler=scheduler,
        artifact_registry=artifacts,
    )
    assert router._workspace_registry is registry
    assert router._scheduler is scheduler
    assert router._artifacts is artifacts


def test_adapter_declares_the_session_wiring_attributes():
    from primer.channel.adapter import ChannelAdapter

    for attr in ("_workspace_registry", "_scheduler"):
        assert attr in ChannelAdapter.__annotations__, (
            f"ChannelAdapter must declare {attr} for the shared helpers"
        )


def test_channel_registry_late_binds_the_session_wiring():
    from primer.api.registries.channel_registry import ChannelRegistry

    reg = ChannelRegistry(
        channel_storage=None, channel_provider_storage=None, inbox=None,
    )
    registry, scheduler = _Registry(), _Scheduler()
    reg.set_session_wiring(workspace_registry=registry, scheduler=scheduler)
    assert reg._workspace_registry is registry
    assert reg._scheduler is scheduler


async def test_build_adapter_forwards_the_session_wiring(monkeypatch):
    import primer.channel.factory as factory
    from primer.model.channel import ChannelProviderType

    captured: dict = {}

    async def _fake(provider_row, channel_row, inbox, **kw):
        captured.update(kw)
        return object()

    monkeypatch.setitem(
        factory._FACTORIES, ChannelProviderType.TELEGRAM, _fake,
    )

    class _P:
        provider = ChannelProviderType.TELEGRAM

    await factory.build_adapter(
        _P(), object(), object(),
        workspace_registry="WR", scheduler="SCH",
    )
    assert captured["workspace_registry"] == "WR"
    assert captured["scheduler"] == "SCH"

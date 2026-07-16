"""Tests for ContainerWorkspaceBackend using a fake adapter.

The real runtime adapters (Docker / Podman / containerd) live in
:mod:`primer.workspace.runtime` and have their own gated contract
tests. The backend itself is unit-testable against a fake adapter that
returns :class:`FakeSandbox` instances.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Literal

import pytest

from primer.int.sandbox import Sandbox
from primer.model.except_ import ConfigError, NotFoundError
from primer.model.workspace import (
    ContainerConnectionSocket,
    ContainerReachabilityConfig,
    ContainerReachabilityHostPort,
    ContainerTemplateConfig,
    ContainerWorkspaceConfig,
    ResourceLimits,
    VolumeMount,
    WorkspaceTemplate,
)
from primer.workspace.container.backend import ContainerWorkspaceBackend
from primer.workspace.runtime.adapter import ContainerRuntimeAdapter
from primer.workspace.sandbox.fake import FakeSandbox


pytestmark = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git CLI not available on PATH (SandboxStateRepo needs it)",
)


class _TrackingFakeSandbox(FakeSandbox):
    """FakeSandbox that notifies its adapter when removed."""

    def __init__(self, root: Path, *, sandbox_id: str, adapter: "_FakeAdapter") -> None:
        super().__init__(root=root, sandbox_id=sandbox_id)
        self._adapter_ref = adapter

    async def remove(self) -> None:
        self._adapter_ref._sandboxes.pop(self._id, None)
        await super().remove()


class _FakeAdapter(ContainerRuntimeAdapter):
    """Adapter that hands out FakeSandbox handles backed by tempdir slots."""

    def __init__(self, tmp_path: Path) -> None:
        self._tmp = tmp_path
        self._sandboxes: dict[str, _TrackingFakeSandbox] = {}
        self._volumes: set[str] = set()

    async def initialize(self) -> None:
        return None

    async def aclose(self) -> None:
        return None

    async def create_sandbox(
        self, *,
        name: str,
        image: str,
        command: list[str],
        env: dict[str, str],
        workdir: str,
        volume_name: str,
        volume_target: str,
        extra_mounts: list[VolumeMount],
        user: str | None,
        resources: ResourceLimits,
        network: Literal["none", "egress", "full"],
        pull_policy: Literal["always", "if_missing", "never"],
        reachability: ContainerReachabilityConfig,
        token: str,
    ) -> Sandbox:
        root = self._tmp / name
        root.mkdir(parents=True, exist_ok=True)
        sb = _TrackingFakeSandbox(root=root, sandbox_id=name, adapter=self)
        # The backend reads ``mapped_host_port`` off the returned sandbox
        # for host_port reachability; the FakeSandbox represents both
        # modes here.
        sb.mapped_host_port = 32100
        self._sandboxes[name] = sb
        self._volumes.add(volume_name)
        return sb

    async def get_sandbox(self, name: str) -> Sandbox | None:
        return self._sandboxes.get(name)

    async def list_sandboxes(self) -> list[str]:
        return list(self._sandboxes)

    async def remove_volume(self, name: str) -> None:
        self._volumes.discard(name)


def _template() -> WorkspaceTemplate:
    return WorkspaceTemplate(
        id="t1", provider_id="c1", description="",
        backend=ContainerTemplateConfig(image="alpine:latest"),
    )


def _config() -> ContainerWorkspaceConfig:
    return ContainerWorkspaceConfig(
        runtime="docker",
        connection=ContainerConnectionSocket(
            socket_path="/var/run/docker.sock",
        ),
        reachability=ContainerReachabilityHostPort(),
    )


@pytest.mark.asyncio
async def test_create_then_get(tmp_path: Path) -> None:
    adapter = _FakeAdapter(tmp_path)
    backend = ContainerWorkspaceBackend(_config(), adapter=adapter)
    await backend.initialize()
    ws = await backend.create(_template())
    fetched = await backend.get(ws.id)
    assert fetched is ws
    await backend.aclose()


@pytest.mark.asyncio
async def test_destroy_removes_sandbox_and_volume(tmp_path: Path) -> None:
    adapter = _FakeAdapter(tmp_path)
    backend = ContainerWorkspaceBackend(_config(), adapter=adapter)
    await backend.initialize()
    ws = await backend.create(_template())
    wid = ws.id
    await backend.destroy(wid)
    assert wid not in await backend.list()
    # Volume removed by destroy.
    assert len(adapter._volumes) == 0
    await backend.aclose()


@pytest.mark.asyncio
async def test_create_with_wrong_template_kind_raises(tmp_path: Path) -> None:
    adapter = _FakeAdapter(tmp_path)
    backend = ContainerWorkspaceBackend(_config(), adapter=adapter)
    await backend.initialize()
    bad = WorkspaceTemplate(
        id="t1", provider_id="c1", description="",
    )  # default backend = local
    with pytest.raises(ConfigError, match="container"):
        await backend.create(bad)
    await backend.aclose()


@pytest.mark.asyncio
async def test_destroy_nonexistent_raises(tmp_path: Path) -> None:
    adapter = _FakeAdapter(tmp_path)
    backend = ContainerWorkspaceBackend(_config(), adapter=adapter)
    await backend.initialize()
    with pytest.raises(NotFoundError):
        await backend.destroy("ws-nonexistent")
    await backend.aclose()


@pytest.mark.asyncio
async def test_image_required(tmp_path: Path) -> None:
    """Empty image is refused at create time (template parses but is unusable)."""
    adapter = _FakeAdapter(tmp_path)
    backend = ContainerWorkspaceBackend(_config(), adapter=adapter)
    await backend.initialize()
    template = WorkspaceTemplate(
        id="t1", provider_id="c1", description="",
        backend=ContainerTemplateConfig(image=""),
    )
    with pytest.raises(ConfigError, match="image"):
        await backend.create(template)
    await backend.aclose()


@pytest.mark.asyncio
async def test_init_command_failure_rolls_back(tmp_path: Path) -> None:
    adapter = _FakeAdapter(tmp_path)
    backend = ContainerWorkspaceBackend(_config(), adapter=adapter)
    await backend.initialize()
    template = WorkspaceTemplate(
        id="t1", provider_id="c1", description="",
        backend=ContainerTemplateConfig(image="alpine:latest"),
        init_commands=["false"],  # fails immediately
    )
    with pytest.raises(ConfigError, match="init command failed"):
        await backend.create(template)
    # Nothing should remain registered.
    assert await backend.list() == []
    await backend.aclose()


@pytest.mark.asyncio
async def test_get_evicts_gone_cached_handle_and_reattaches(
    tmp_path: Path,
) -> None:
    """Regression for the dead-handle cache bug: once a cached workspace's
    runtime client goes ``gone`` (the runtime self-evicts on a 404
    handshake), ``get()`` must evict it from the cache and re-attach a
    fresh handle rather than returning the dead one.
    """
    adapter = _FakeAdapter(tmp_path)
    backend = ContainerWorkspaceBackend(_config(), adapter=adapter)
    await backend.initialize()
    template = _template()
    ws = await backend.create(template)
    wid = ws.id

    # Warm path: the cached handle is live and returned as-is.
    assert await backend.get(wid, template=template) is ws

    # Simulate the runtime self-evicting: the underlying sandbox reports
    # gone, so SandboxWorkspace.gone is True for the cached handle.
    ws.sandbox.gone = True
    assert ws.gone is True

    # The adapter still has a live (non-gone) sandbox under the same name
    # (e.g. a fresh pod/container that came back); a fresh re-attach picks
    # it up. Swap in a brand-new sandbox so we can prove a new handle.
    name = f"workspace-{wid}"
    fresh = _TrackingFakeSandbox(
        root=tmp_path / name, sandbox_id=name, adapter=adapter,
    )
    fresh.mapped_host_port = 32100
    adapter._sandboxes[name] = fresh

    # get() must NOT return the gone handle: it evicts + re-attaches.
    reattached = await backend.get(wid, template=template)
    assert reattached is not None
    assert reattached is not ws, "get() returned the dead, gone handle"
    assert reattached.gone is False
    # The fresh handle is now cached and stable on the next call.
    assert await backend.get(wid, template=template) is reattached
    await backend.aclose()


@pytest.mark.asyncio
async def test_create_injects_strict_write_locking_env_when_enabled(
    tmp_path: Path,
) -> None:
    """When the template opts into strict write locking, the container env
    passed to ``create_sandbox`` carries ``PRIMER_STRICT_WRITE_LOCKING=1``."""
    adapter = _FakeAdapter(tmp_path)
    backend = ContainerWorkspaceBackend(_config(), adapter=adapter)
    await backend.initialize()

    captured_env: dict[str, str] = {}
    orig_create_sandbox = adapter.create_sandbox

    async def _spy_create_sandbox(*, env, **kwargs):
        captured_env.update(env)
        return await orig_create_sandbox(env=env, **kwargs)

    adapter.create_sandbox = _spy_create_sandbox

    template = WorkspaceTemplate(
        id="t1", provider_id="c1", description="",
        backend=ContainerTemplateConfig(image="alpine:latest"),
        strict_write_locking=True,
    )
    await backend.create(template)
    assert captured_env.get("PRIMER_STRICT_WRITE_LOCKING") == "1"
    await backend.aclose()


@pytest.mark.asyncio
async def test_create_omits_strict_write_locking_env_by_default(
    tmp_path: Path,
) -> None:
    """When the template does not opt in, the env var is absent entirely."""
    adapter = _FakeAdapter(tmp_path)
    backend = ContainerWorkspaceBackend(_config(), adapter=adapter)
    await backend.initialize()

    captured_env: dict[str, str] = {}
    orig_create_sandbox = adapter.create_sandbox

    async def _spy_create_sandbox(*, env, **kwargs):
        captured_env.update(env)
        return await orig_create_sandbox(env=env, **kwargs)

    adapter.create_sandbox = _spy_create_sandbox

    await backend.create(_template())
    assert "PRIMER_STRICT_WRITE_LOCKING" not in captured_env
    await backend.aclose()


@pytest.mark.asyncio
async def test_get_returns_none_when_gone_handle_cannot_reattach(
    tmp_path: Path,
) -> None:
    """If the cached handle is gone AND the backing sandbox is also gone
    (adapter no longer has it), get() returns None after eviction."""
    adapter = _FakeAdapter(tmp_path)
    backend = ContainerWorkspaceBackend(_config(), adapter=adapter)
    await backend.initialize()
    template = _template()
    ws = await backend.create(template)
    wid = ws.id

    ws.sandbox.gone = True
    # Remove the backing sandbox so re-attach finds nothing.
    adapter._sandboxes.pop(f"workspace-{wid}", None)

    assert await backend.get(wid, template=template) is None
    # The dead handle was evicted from the cache.
    assert wid not in backend._workspaces
    await backend.aclose()

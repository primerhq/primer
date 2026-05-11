"""Tests for ContainerWorkspaceBackend using a fake adapter.

The real runtime adapters (Docker / Podman / containerd) live in
:mod:`matrix.workspace.runtime` and have their own gated contract
tests. The backend itself is unit-testable against a fake adapter that
returns :class:`FakeSandbox` instances.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Literal

import pytest

from matrix.int.sandbox import Sandbox
from matrix.model.except_ import ConfigError, NotFoundError
from matrix.model.workspace import (
    ContainerWorkspaceConfig,
    ResourceLimits,
    VolumeMount,
    WorkspaceTemplate,
    _ContainerTemplateConfig,
    _DockerRuntimeConfig,
)
from matrix.workspace.container.backend import ContainerWorkspaceBackend
from matrix.workspace.runtime.adapter import ContainerRuntimeAdapter
from matrix.workspace.sandbox.fake import FakeSandbox


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
    ) -> Sandbox:
        root = self._tmp / name
        root.mkdir(parents=True, exist_ok=True)
        sb = _TrackingFakeSandbox(root=root, sandbox_id=name, adapter=self)
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
        backend=_ContainerTemplateConfig(image="alpine:latest"),
    )


def _config() -> ContainerWorkspaceConfig:
    return ContainerWorkspaceConfig(runtime=_DockerRuntimeConfig())


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
    """Pydantic itself rejects empty image at parse time."""
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        WorkspaceTemplate(
            id="t1", provider_id="c1", description="",
            backend={"kind": "container", "image": ""},  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_init_command_failure_rolls_back(tmp_path: Path) -> None:
    adapter = _FakeAdapter(tmp_path)
    backend = ContainerWorkspaceBackend(_config(), adapter=adapter)
    await backend.initialize()
    template = WorkspaceTemplate(
        id="t1", provider_id="c1", description="",
        backend=_ContainerTemplateConfig(image="alpine:latest"),
        init_commands=["false"],  # fails immediately
    )
    with pytest.raises(ConfigError, match="init command failed"):
        await backend.create(template)
    # Nothing should remain registered.
    assert await backend.list() == []
    await backend.aclose()

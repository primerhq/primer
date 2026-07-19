"""Regression tests for caller-pinned ``workspace_id`` passthrough.

Creating a workspace with a caller-supplied id must pin the *live*
instance to that id -- not just the persisted row. Before the fix the
passthrough was dropped between the API/registry layer and the backend:
``create`` materialised the instance under an auto-generated id while the
row was stored under the caller id, so once the in-memory cache was
evicted ``get(caller_id)`` re-attached to a *different* object (or None)
and the API 404'd with "row exists but the backend has no live instance".

These tests reproduce that gap end-to-end against the LOCAL backend
(no container/k8s needed) plus the registry forwarding point.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from primer.api.registries.workspace_registry import WorkspaceRegistry
from primer.model.workspace import ResourceLimits, WorkspaceTemplate
from primer.workspace import LocalWorkspaceBackend


# LocalWorkspace.materialise stands up a StateRepo, which shells out to git.
pytestmark = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git CLI not available on PATH (StateRepo needs it)",
)


def _template(provider_id: str = "local-1") -> WorkspaceTemplate:
    return WorkspaceTemplate(
        id="dev",
        description="local dev template",
        provider_id=provider_id,
        files=[],
        init_commands=[],
        env={},
        resources=ResourceLimits(),
    )


@pytest.fixture
async def backend(tmp_path: Path) -> LocalWorkspaceBackend:
    p = LocalWorkspaceBackend(tmp_path / "provider_root")
    await p.initialize()
    return p


async def test_create_pins_live_instance_to_caller_id(
    backend: LocalWorkspaceBackend,
) -> None:
    """A pinned ``workspace_id`` becomes the live workspace's id."""
    ws = await backend.create(_template(), workspace_id="psx-financials")
    assert ws.id == "psx-financials"


async def test_pinned_id_reattaches_after_cache_eviction(
    backend: LocalWorkspaceBackend,
) -> None:
    """After eviction, ``get(pinned_id)`` re-attaches to the SAME instance.

    This is the exact production failure: the row is keyed by the caller
    id, so re-attach looks the backend up by that id. If ``create`` had
    materialised the instance under an auto-generated id instead, the
    on-disk workspace directory would live elsewhere and re-attach would
    return None.
    """
    tpl = _template()
    ws = await backend.create(tpl, workspace_id="psx-financials")
    assert ws.id == "psx-financials"

    # Simulate the in-memory cache being evicted (process restart / LRU).
    async with backend._lock:  # noqa: SLF001 -- deliberate white-box eviction
        backend._workspaces.clear()

    reattached = await backend.get("psx-financials", template=tpl)
    assert reattached is not None
    assert reattached.id == "psx-financials"


class _RecordingBackend:
    """Captures the kwargs the registry forwards to ``backend.create``."""

    def __init__(self) -> None:
        self.received_workspace_id: str | None = "<<unset>>"

    async def create(
        self, template, *, overrides=None, workspace_id=None, resolvers=None
    ):
        self.received_workspace_id = workspace_id
        return "workspace-handle"


async def test_registry_materialise_forwards_workspace_id(monkeypatch) -> None:
    """``WorkspaceRegistry.materialise`` plumbs the pinned id to the backend."""
    rec = _RecordingBackend()
    reg = WorkspaceRegistry(storage_provider=object())

    async def _fake_get_backend(provider_id):
        return rec

    monkeypatch.setattr(reg, "get_backend", _fake_get_backend)

    class _Tpl:
        provider_id = "prov1"

    result = await reg.materialise(template=_Tpl(), workspace_id="psx-financials")
    assert result == "workspace-handle"
    assert rec.received_workspace_id == "psx-financials"

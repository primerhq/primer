"""Workspace ``runtime_meta`` persistence + token redaction.

Task 6.3 makes ``runtime_meta`` a required field on the persisted
:class:`primer.model.workspace.Workspace` row. This module pins down:

* every concrete backend's live :class:`Workspace` exposes a
  ``runtime_meta`` (sentinel for local, real URL+token for
  container/k8s once they're wired); and
* the token serialises as the pydantic ``SecretStr`` default redaction
  on GET, so the raw bearer never leaks through the REST surface.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import SecretStr

from primer.model.workspace import (
    Workspace,
    WorkspaceRuntimeMeta,
)


# ===========================================================================
# Token redaction
# ===========================================================================


def test_runtime_meta_token_redacted_in_json_dump() -> None:
    """SecretStr token is redacted (``"**********"``) on ``model_dump``."""
    w = Workspace(
        id="ws-1",
        description=None,
        template_id="tpl-1",
        provider_id="prov-1",
        created_at=datetime(2026, 5, 29, tzinfo=timezone.utc),
        runtime_meta=WorkspaceRuntimeMeta(
            url="ws://workspace-x:5959/",
            token=SecretStr("real-secret-token"),
        ),
    )
    dumped = w.model_dump(mode="json")
    # The raw token must never appear in a JSON dump.
    assert "real-secret-token" not in str(dumped)
    # And the redaction marker pydantic uses must be present.
    assert dumped["runtime_meta"]["token"] == "**********"


def test_runtime_meta_token_round_trips_back_to_get_secret_value() -> None:
    """Even though dump redacts, ``get_secret_value`` still gives the bearer."""
    meta = WorkspaceRuntimeMeta(
        url="ws://x:5959/",
        token=SecretStr("real-secret-token"),
    )
    assert meta.token.get_secret_value() == "real-secret-token"
    # And repr() also redacts, so logger.info(meta) doesn't leak it.
    assert "real-secret-token" not in repr(meta)


# ===========================================================================
# Per-backend live runtime_meta
# ===========================================================================


@pytest.mark.asyncio
async def test_local_workspace_runtime_meta_is_sentinel(tmp_path) -> None:
    """LocalWorkspace returns a ``local://<id>`` sentinel with empty token.

    The local backend has no real WS runtime — files/exec are in-process.
    The sentinel keeps the persisted Workspace row's required field
    populated without inventing a fake URL we'd later mistake for a real
    endpoint.
    """
    import shutil

    if shutil.which("git") is None:
        pytest.skip("git CLI not available (LocalStateRepo needs it)")

    from primer.model.workspace import (
        LocalTemplateConfig,
        WorkspaceTemplate,
    )
    from primer.workspace.local.workspace import LocalWorkspace

    ws_root = tmp_path / "ws-local-1"
    ws_root.mkdir()
    template = WorkspaceTemplate(
        id="tpl-local",
        description="",
        provider_id="prov-local",
        backend=LocalTemplateConfig(),
    )
    ws = await LocalWorkspace.materialise(
        workspace_id="ws-local-1",
        root=ws_root,
        template=template,
        env={},
    )
    meta = ws.runtime_meta
    assert isinstance(meta, WorkspaceRuntimeMeta)
    assert meta.url == "local://ws-local-1"
    # Token is empty SecretStr; still serialises as redaction in JSON
    assert meta.token.get_secret_value() == ""
    assert meta.mapped_host_port is None
    assert meta.k8s_object_name is None


@pytest.mark.asyncio
async def test_sandbox_workspace_carries_supplied_runtime_meta(tmp_path) -> None:
    """SandboxWorkspace (Container/K8s) surfaces the runtime_meta the backend
    constructed at create time, untouched."""
    import shutil

    if shutil.which("git") is None:
        pytest.skip("git CLI not available (SandboxStateRepo needs it)")

    from primer.model.workspace import (
        ContainerTemplateConfig,
        WorkspaceTemplate,
    )
    from primer.workspace.sandbox.fake import FakeSandbox
    from primer.workspace.sandbox.workspace import SandboxWorkspace

    template = WorkspaceTemplate(
        id="tpl-c",
        description="",
        provider_id="prov-c",
        backend=ContainerTemplateConfig(image="python:3.13"),
    )
    sb = FakeSandbox(root=tmp_path)
    supplied = WorkspaceRuntimeMeta(
        url="ws://127.0.0.1:32100/",
        token=SecretStr("super-secret"),
        mapped_host_port=32100,
    )
    ws = await SandboxWorkspace.materialise(
        workspace_id="ws-sand-1",
        template=template,
        sandbox=sb,
        backend_kind="container",
        runtime_meta=supplied,
    )
    assert ws.runtime_meta is supplied
    assert ws.runtime_meta.url == "ws://127.0.0.1:32100/"
    assert ws.runtime_meta.mapped_host_port == 32100
    assert ws.runtime_meta.token.get_secret_value() == "super-secret"

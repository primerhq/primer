"""An install or sync requested over ``/v1/mcp`` records the MCP caller as the requester (security review 2026-10-08).

The MCP endpoint dispatches tool handlers with no ``ToolContext``, so ``harness__install`` / ``harness__sync`` recorded no
requester and the worker's toolset admin rule (fail closed on an unknown requester) refused an ADMIN's install of a bundle with
a stdio toolset. The handlers now fall back to the MCP caller the endpoint resolved (``primer.mcp.server.current_actor``). A
caller below admin never reaches the handler: the RBAC floor in ``invoke_exposed`` refuses it in-band.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from primer.mcp.dispatch import invoke_exposed
from primer.mcp.exposure import ExposureDeps, update_exposure
from primer.mcp.server import current_actor
from primer.model.harness import Harness, HarnessOperation, HarnessStatus
from primer.model.principal import Principal
from primer.toolset.harness import build_harness_toolset_provider
from tests.mcp.conftest import FakeProviderRegistry

pytestmark = pytest.mark.asyncio


async def _call(fake_storage_provider, tool: str, actor: Principal):
    provider = build_harness_toolset_provider(storage_provider=fake_storage_provider)
    deps = ExposureDeps(storage_provider=fake_storage_provider, provider_registry=FakeProviderRegistry({"harness": provider}))
    scoped = f"harness__{tool}"
    await update_exposure(enabled=True, allowed_tools=[scoped], updated_by="admin", deps=deps)
    token = current_actor.set(actor)
    try:
        return await invoke_exposed(scoped_id=scoped, arguments={"id": "hns_1"}, principal=actor.id, actor=actor, deps=deps)
    finally:
        current_actor.reset(token)


async def _seed(fake_storage_provider, status: HarnessStatus) -> None:
    await fake_storage_provider.get_storage(Harness).create(
        Harness(
            id="hns_1", slug="mcp-harness", name="x", git_url="https://h.example/r", status=status,
            overrides_schema={"type": "object"}, available_bundle_hash="bh", created_at=datetime.now(timezone.utc),
        ),
    )


@pytest.mark.parametrize(
    ("tool", "status", "operation"),
    [
        ("harness__install", HarnessStatus.READY, HarnessOperation.INSTALL),
        ("harness__sync", HarnessStatus.INSTALLED, HarnessOperation.SYNC),
    ],
)
async def test_an_admin_over_mcp_is_recorded_as_the_requester(fake_storage_provider, tool, status, operation):
    await _seed(fake_storage_provider, status)
    admin = Principal(type="user", id="u-admin", display="admin", role="admin", source="local")

    result = await _call(fake_storage_provider, tool, admin)

    assert result.is_error is False, result.output
    stored = await fake_storage_provider.get_storage(Harness).get("hns_1")
    assert stored.pending_operation == operation
    assert stored.operation_requested_by is not None
    assert (stored.operation_requested_by.id, stored.operation_requested_by.role) == ("u-admin", "admin")


async def test_a_user_over_mcp_is_refused_and_nothing_is_enqueued(fake_storage_provider):
    await _seed(fake_storage_provider, HarnessStatus.READY)
    user = Principal(type="user", id="u-user", display="user", role="user", source="local")

    result = await _call(fake_storage_provider, "harness__install", user)

    assert result.is_error is True
    assert "requires the 'admin' role" in result.output
    stored = await fake_storage_provider.get_storage(Harness).get("hns_1")
    assert stored.pending_operation is None
    assert stored.operation_requested_by is None

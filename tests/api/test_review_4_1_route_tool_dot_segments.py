"""Review 2026-11-23 finding 4.1: a ``..`` or ``.`` path argument (or any
value containing ``/``) must not re-route a generated route tool at another
REST operation.

``quote(..., safe="")`` leaves ``.``/``..`` untouched and a request URL is
normalized before routing, so ``token_id=".."`` on
``admin__admin_revoke_user_token`` lands on ``DELETE
/v1/admin/users/{user_id}`` -- the delete-USER operation -- and every
tool-level control (approval card, MCP allowlist, audit scoped id) judges the
named tool, not the operation that runs. The fix refuses such path values
before any request is built, so a refusal may surface as a raised exception
OR an error result; both count as the call not having run the parent
operation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

# Convention: shared API test fixtures.
from tests.api.conftest import app  # noqa: F401

from primer.model.access import Access
from primer.model.principal import PrincipalRef
from primer.model.user import User
from primer.model.workspace import (
    LocalWorkspaceConfig,
    Workspace as WorkspaceRow,
    WorkspaceProvider,
    WorkspaceProviderType,
    WorkspaceRuntimeMeta,
)
from primer.model.yield_ import ToolContext


def _admin_ctx(tool_call_id: str) -> ToolContext:
    return ToolContext(
        tool_call_id=tool_call_id,
        session_id=None,
        workspace_id=None,
        initiated_by=PrincipalRef(
            type="user",
            id="user-admin",
            display="adminactor",
            role="admin",
            source="local",
            user_id="user-admin",
        ),
    )


async def _call(app, toolset: str, tool_name: str, arguments: dict[str, Any]) -> tuple[BaseException | None, Any]:
    """One generated route tool call, wrapped so a refusal that RAISES or
    one that RETURNS an error result both come back as (refused, result)."""
    provider = app.state.generated_route_toolsets.providers[toolset]
    try:
        result = await provider.call(
            tool_name=tool_name, arguments=arguments, ctx=_admin_ctx(f"call-review-4-1-{tool_name}")
        )
    except BaseException as exc:  # a refusal may raise; either way nothing must run
        return exc, None
    return None, result


@pytest.mark.asyncio
async def test_revoke_user_token_dotdot_token_id_does_not_delete_the_user(app):
    """A token id of ``..`` must not run the parent delete-user operation:
    the call is refused (raise or error result) and the victim user still
    exists afterwards."""
    storage = app.state.storage_provider.get_storage(User)
    await storage.create(
        User(
            id="user-admin",
            username="adminactor",
            created_at=datetime.now(timezone.utc),
            role="admin",
        )
    )
    victim = User(
        id="user-victim",
        username="victim",
        created_at=datetime.now(timezone.utc),
        role="user",
    )
    await storage.create(victim)

    raised, result = await _call(
        app, "admin", "admin_revoke_user_token",
        {"user_id": victim.id, "token_id": ".."},
    )

    # The named operation revokes a TOKEN. Whatever the refusal looks like,
    # the user account must still exist.
    row = await storage.get(victim.id)
    assert row is not None, (
        "the .. path argument re-routed the tool at DELETE /v1/admin/users/{user_id} "
        f"and deleted the user (raised: {raised!r}, tool result: {result!r})"
    )
    assert raised is not None or (result is not None and result.is_error), (
        f"token_id='..' was not refused before the request (raised: {raised!r}, "
        f"result: {result!r})"
    )


@pytest.mark.asyncio
async def test_delete_session_dotdot_session_id_does_not_delete_the_workspace(app):
    """A session id of ``..`` on the workspace-session delete tool must not
    run the parent delete-workspace operation: the call is refused and the
    workspace still exists afterwards."""
    sp = app.state.storage_provider
    await sp.get_storage(User).create(
        User(
            id="user-admin",
            username="adminactor",
            created_at=datetime.now(timezone.utc),
            role="admin",
        )
    )
    await sp.get_storage(WorkspaceProvider).create(
        WorkspaceProvider(
            id="wp-1",
            provider=WorkspaceProviderType.LOCAL,
            config=LocalWorkspaceConfig(root_path="/tmp/primer-review-4-1"),
            access=Access(public=True),
        )
    )
    ws = WorkspaceRow(
        id="ws-41",
        template_id="tpl-1",
        provider_id="wp-1",
        created_at=datetime.now(timezone.utc),
        phase="running",
        access=Access(owners=["user-admin"]),
        runtime_meta=WorkspaceRuntimeMeta(url="ws://127.0.0.1:5959/", token="t"),
    )
    await sp.get_storage(WorkspaceRow).create(ws)

    raised, result = await _call(
        app, "workspace_sessions", "delete_session",
        {"workspace_id": ws.id, "session_id": ".."},
    )

    row = await sp.get_storage(WorkspaceRow).get(ws.id)
    assert row is not None, (
        "the .. session id re-routed the tool at DELETE /v1/workspaces/{workspace_id} "
        f"and deleted the whole workspace (raised: {raised!r}, tool result: {result!r})"
    )
    assert raised is not None or (result is not None and result.is_error), (
        f"session_id='..' was not refused before the request (raised: {raised!r}, "
        f"result: {result!r})"
    )


@pytest.mark.asyncio
async def test_dotted_but_not_dotdot_token_id_still_reaches_the_named_route(app):
    """A normal id containing dots (``v1.2``) is a legal path segment: the
    call must NOT be refused and must land on the named revoke-token route
    (a 404 for the missing token, not the parent operation)."""
    storage = app.state.storage_provider.get_storage(User)
    await storage.create(
        User(
            id="user-admin",
            username="adminactor",
            created_at=datetime.now(timezone.utc),
            role="admin",
        )
    )
    victim = User(
        id="user-victim",
        username="victim",
        created_at=datetime.now(timezone.utc),
        role="user",
    )
    await storage.create(victim)

    raised, result = await _call(
        app, "admin", "admin_revoke_user_token",
        {"user_id": victim.id, "token_id": "v1.2"},
    )

    assert raised is None, f"the dotted id raised unexpectedly: {raised!r}"
    # The named route answers 404 token_not_found for a missing token; that
    # body proves the request landed where the tool names it.
    assert result is not None and result.is_error and "token_not_found" in result.output, (
        f"the v1.2 token id did not reach the named revoke-token route (result: {result!r})"
    )
    assert await storage.get(victim.id) is not None, (
        f"the v1.2 token id re-routed the tool and deleted the user (result: {result!r})"
    )


# --- Review round 1 part 2 (4.1 broke a documented tool) ------------------
#
# The first fix refused EVERY "/" in every path argument, which broke the
# {path:path} parameter of /svc/{name}/{path:path}: its value is a whole
# asset path and may lawfully contain "/". Parameters whose route convertor
# is "path" now allow "/" between segments, while every segment must still
# be non-empty and not "." or ".."; every other path parameter keeps the
# single-segment rule.


@pytest.mark.asyncio
async def test_svc_serve_multi_segment_asset_paths_reach_the_named_route(app):
    """``assets/main.js`` and ``a/b/c.css`` are legal {path:path} values:
    the calls must NOT be refused and must land on the named svc_serve
    operation (the 404 for a service that is not registered)."""
    for asset in ("assets/main.js", "a/b/c.css"):
        raised, result = await _call(
            app, "svc", "svc_serve", {"name": "demo", "path": asset},
        )
        assert raised is None, (
            f"path={asset!r} raised (the {{path:path}} convertor takes "
            f"whole segments): {raised!r}"
        )
        assert result is not None and result.is_error, (
            f"path={asset!r} was not answered by the named route (result: {result!r})"
        )
        # No service named "demo" is registered: the 404 body of the named
        # route proves the request landed where the tool names it, with the
        # multi-segment value intact.
        assert "no such service" in result.output, (
            f"path={asset!r} did not reach the named svc_serve route "
            f"(result: {result!r})"
        )
        assert "route-tool-invalid-path-argument" not in result.output, (
            f"path={asset!r} was refused as an invalid path argument "
            f"(result: {result!r})"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    ["a/../b", "./x", "a//b", "..", "a/b/../c"],
    ids=["dotdot-segment", "dot-segment", "empty-segment", "bare-dotdot", "dotdot-mid"],
)
async def test_svc_serve_dot_segments_are_refused(app, bad):
    """A {path:path} value may contain "/" but no segment may be empty,
    "." or ".." (URL normalisation would re-route the call at another
    operation or another asset): each is refused before any request is
    built."""
    raised, result = await _call(
        app, "svc", "svc_serve", {"name": "demo", "path": bad},
    )
    assert raised is not None or (result is not None and result.is_error), (
        f"path={bad!r} was not refused before the request (raised: {raised!r}, "
        f"result: {result!r})"
    )
    if result is not None:
        assert "route-tool-invalid-path-argument" in result.output, (
            f"path={bad!r} was refused with the wrong error (result: {result!r})"
        )


@pytest.mark.asyncio
async def test_non_path_parameter_still_refuses_slash(app):
    """The single-segment rule survives for ordinary (non {path:path})
    parameters: a '/' in a one-segment path argument is still a refusal
    (the pilot rule), so a converted value cannot widen where it must not."""
    storage = app.state.storage_provider.get_storage(User)
    await storage.create(
        User(
            id="user-admin",
            username="adminactor",
            created_at=datetime.now(timezone.utc),
            role="admin",
        )
    )
    victim = User(
        id="user-victim",
        username="victim",
        created_at=datetime.now(timezone.utc),
        role="user",
    )
    await storage.create(victim)

    raised, result = await _call(
        app, "admin", "admin_revoke_user_token",
        {"user_id": victim.id, "token_id": "a/b"},
    )

    assert raised is not None or (result is not None and result.is_error), (
        f"token_id='a/b' was not refused before the request (raised: {raised!r}, "
        f"result: {result!r})"
    )
    assert await storage.get(victim.id) is not None, (
        f"token_id='a/b' re-routed the tool and deleted the user (result: {result!r})"
    )

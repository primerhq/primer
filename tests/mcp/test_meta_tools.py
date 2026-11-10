"""Tools Universal M2 MCP meta-tool contracts."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from primer.mcp.exposure import ExposureDeps, update_exposure
from primer.mcp.meta_tools import (
    call_tool,
    find_tools,
    list_tools,
)
from primer.mcp.server import current_actor, current_api_token_scopes, current_principal
from primer.model.mcp_exposure import McpExposure
from primer.model.principal import Principal
from tests.mcp.conftest import FakeProviderRegistry, FakeToolsetProvider, _make_tool


def _actor(role: str = "user") -> Principal:
    return Principal(
        type="user",
        id="u-1",
        user_id="u-1",
        display="User One",
        role=role,
        source="local",
    )


def _deps(storage, *, semantic_search=None) -> ExposureDeps:
    tools = [
        _make_tool("misc", "alpha", "Alpha reader."),
        _make_tool("misc", "beta", "Beta writer."),
        _make_tool("misc", "root", "Admin reader."),
    ]
    tools[0] = tools[0].model_copy(update={"read_only": True})
    tools[2] = tools[2].model_copy(update={"read_only": True})
    provider = FakeToolsetProvider(
        "misc",
        tools,
        roles={"alpha": "user", "beta": "user", "root": "admin"},
    )
    return ExposureDeps(
        storage_provider=storage,
        provider_registry=FakeProviderRegistry({"misc": provider}),
        semantic_search=semantic_search,
    )


@pytest.mark.asyncio
async def test_rbac_catalogue_is_caller_aware_and_keyset_paginated(
    fake_storage_provider,
) -> None:
    deps = _deps(fake_storage_provider)
    await update_exposure(
        enabled=True,
        mode="rbac",
        allowed_tools=None,
        updated_by="admin",
        deps=deps,
    )
    actor_token = current_actor.set(_actor())
    scope_token = current_api_token_scopes.set(["mcp"])
    principal_token = current_principal.set("user@example.com")
    try:
        first = await list_tools(deps, {"limit": 1, "include_schema": False})
        second = await list_tools(
            deps,
            {"limit": 10, "cursor": first["next_cursor"], "include_schema": True},
        )
    finally:
        current_principal.reset(principal_token)
        current_api_token_scopes.reset(scope_token)
        current_actor.reset(actor_token)

    assert [item["scoped_id"] for item in first["items"]] == ["misc__alpha"]
    assert [item["scoped_id"] for item in second["items"]] == ["misc__beta"]
    assert "schema" not in first["items"][0]
    assert second["items"][0]["schema"]["type"] == "object"
    assert all(item["scoped_id"] != "misc__root" for item in first["items"] + second["items"])


@pytest.mark.asyncio
async def test_allowlist_mode_stays_a_narrowing_filter(fake_storage_provider) -> None:
    deps = _deps(fake_storage_provider)
    await update_exposure(
        enabled=True,
        mode="allowlist",
        allowed_tools=["misc__beta"],
        updated_by="admin",
        deps=deps,
    )
    actor_token = current_actor.set(_actor())
    scope_token = current_api_token_scopes.set(["mcp"])
    try:
        result = await list_tools(deps, {"limit": 20})
    finally:
        current_api_token_scopes.reset(scope_token)
        current_actor.reset(actor_token)

    assert [item["scoped_id"] for item in result["items"]] == ["misc__beta"]


@pytest.mark.asyncio
async def test_find_lexical_always_works_and_semantic_hits_are_intersected(
    fake_storage_provider,
) -> None:
    async def semantic(_query: str, _limit: int) -> list[str]:
        if _query == "alpha":
            return []
        return [
            "reference/tools/misc/root",
            "reference/tools/misc/beta",
            "reference/tools/ghost/secret",
        ]

    deps = _deps(fake_storage_provider, semantic_search=semantic)
    await update_exposure(
        enabled=True,
        mode="rbac",
        allowed_tools=None,
        updated_by="admin",
        deps=deps,
    )
    actor_token = current_actor.set(_actor())
    scope_token = current_api_token_scopes.set(["mcp"])
    try:
        lexical = await find_tools(deps, {"query": "alpha", "limit": 10})
        semantic_result = await find_tools(deps, {"query": "writer", "limit": 10})
    finally:
        current_api_token_scopes.reset(scope_token)
        current_actor.reset(actor_token)

    assert [item["scoped_id"] for item in lexical["items"]] == ["misc__alpha"]
    assert "misc__beta" in [item["scoped_id"] for item in semantic_result["items"]]
    assert "misc__root" not in [item["scoped_id"] for item in semantic_result["items"]]
    assert all("ghost" not in item["scoped_id"] for item in semantic_result["items"])


@pytest.mark.asyncio
async def test_discovery_without_mcp_scope_is_empty(fake_storage_provider) -> None:
    deps = _deps(fake_storage_provider)
    await update_exposure(
        enabled=True,
        mode="rbac",
        allowed_tools=None,
        updated_by="admin",
        deps=deps,
    )
    actor_token = current_actor.set(_actor())
    scope_token = current_api_token_scopes.set([])
    try:
        listed = await list_tools(deps, {"limit": 20})
        found = await find_tools(deps, {"query": "alpha", "limit": 20})
    finally:
        current_api_token_scopes.reset(scope_token)
        current_actor.reset(actor_token)

    assert listed["items"] == []
    assert found["items"] == []


@pytest.mark.asyncio
async def test_call_tool_uses_invoke_exposed_and_hides_unknown_target(
    fake_storage_provider,
    monkeypatch,
) -> None:
    deps = _deps(fake_storage_provider)
    await fake_storage_provider.get_storage(McpExposure).create(
        McpExposure(
            enabled=True,
            mode="rbac",
            allowed_tools=[],
            updated_at=datetime.now(timezone.utc),
        )
    )
    seen = {}

    async def fake_invoke(**kwargs):
        seen.update(kwargs)
        from primer.model.chat import ToolCallResult

        return ToolCallResult(output="ok", is_error=False)

    monkeypatch.setattr("primer.mcp.meta_tools.invoke_exposed", fake_invoke)
    actor_token = current_actor.set(_actor())
    scope_token = current_api_token_scopes.set(["mcp"])
    principal_token = current_principal.set("user@example.com")
    try:
        result = await call_tool(
            deps,
            {"scoped_id": "misc__alpha", "arguments": {"value": 1}},
        )
    finally:
        current_principal.reset(principal_token)
        current_api_token_scopes.reset(scope_token)
        current_actor.reset(actor_token)

    assert result == {"ok": True, "output": "ok"}
    assert seen["scoped_id"] == "misc__alpha"
    assert seen["arguments"] == {"value": 1}


def test_missing_mode_is_legacy_allowlist_but_explicit_new_mode_is_rbac() -> None:
    base = {
        "enabled": True,
        "allowed_tools": [],
        "updated_at": datetime.now(timezone.utc),
    }
    assert McpExposure.model_validate(base).mode == "allowlist"
    assert McpExposure.model_validate({**base, "mode": "rbac"}).mode == "rbac"


# ---- Opaque versioned filter/query-bound cursors (seam spec §1) ------------


def _bad_cursor(code="invalid_cursor") -> dict:
    return {"ok": False, "error": {"code": code, "message": "Cursor is invalid for this request."}}


async def _list(deps: ExposureDeps, arguments: dict, actor: Principal = _actor()) -> dict:
    actor_token = current_actor.set(actor)
    scope_token = current_api_token_scopes.set(["mcp"])
    principal_token = current_principal.set("user@example.com")
    try:
        return await list_tools(deps, arguments)
    finally:
        current_principal.reset(principal_token)
        current_api_token_scopes.reset(scope_token)
        current_actor.reset(actor_token)


async def _find(deps: ExposureDeps, arguments: dict, actor: Principal = _actor()) -> dict:
    actor_token = current_actor.set(actor)
    scope_token = current_api_token_scopes.set(["mcp"])
    principal_token = current_principal.set("user@example.com")
    try:
        return await find_tools(deps, arguments)
    finally:
        current_principal.reset(principal_token)
        current_api_token_scopes.reset(scope_token)
        current_actor.reset(actor_token)


async def _rbac_deps(fake_storage_provider) -> ExposureDeps:
    deps = _deps(fake_storage_provider)
    await update_exposure(enabled=True, mode="rbac", allowed_tools=None, updated_by="admin", deps=deps)
    return deps


@pytest.mark.asyncio
async def test_cursor_is_opaque_and_keyset(fake_storage_provider) -> None:
    deps = await _rbac_deps(fake_storage_provider)

    first = await _list(deps, {"limit": 1})

    cursor = first["next_cursor"]
    assert isinstance(cursor, str)
    assert cursor != "misc__alpha"
    assert "misc__alpha" not in cursor
    second = await _list(deps, {"limit": 1, "cursor": cursor})
    assert [item["scoped_id"] for item in second["items"]] == ["misc__beta"]
    # Only two items are authorized for a user actor: page two is the end.
    assert second["next_cursor"] is None


@pytest.mark.asyncio
async def test_cursor_is_bound_to_list_filters(fake_storage_provider) -> None:
    deps = await _rbac_deps(fake_storage_provider)

    issued = await _list(deps, {"toolset_id": "misc", "limit": 1})
    refused = await _list(deps, {"limit": 1, "cursor": issued["next_cursor"]})

    assert refused == _bad_cursor()


@pytest.mark.asyncio
async def test_cursor_is_bound_to_find_query(fake_storage_provider) -> None:
    deps = await _rbac_deps(fake_storage_provider)

    issued = await _find(deps, {"query": "misc", "limit": 1})
    refused = await _find(deps, {"query": "beta", "limit": 1, "cursor": issued["next_cursor"]})
    continued = await _find(deps, {"query": "misc", "limit": 1, "cursor": issued["next_cursor"]})

    assert refused == _bad_cursor()
    assert [item["scoped_id"] for item in continued["items"]] == ["misc__beta"]


@pytest.mark.asyncio
async def test_next_cursor_none_at_end(fake_storage_provider) -> None:
    deps = await _rbac_deps(fake_storage_provider)

    whole = await _list(deps, {"limit": 100})

    assert [item["scoped_id"] for item in whole["items"]] == ["misc__alpha", "misc__beta"]
    assert whole["next_cursor"] is None
    page_one = await _list(deps, {"limit": 1})
    page_two = await _list(deps, {"limit": 1, "cursor": page_one["next_cursor"]})
    assert page_two["next_cursor"] is None


@pytest.mark.asyncio
async def test_garbage_cursor_rejected(fake_storage_provider) -> None:
    import base64
    import json

    from primer.mcp.meta_tools import _fingerprint

    deps = await _rbac_deps(fake_storage_provider)
    fingerprint = _fingerprint(
        {"toolset_id": None, "resource": None, "read_only": None, "required_role": None, "text_prefix": ""},
    )
    legacy_version = base64.urlsafe_b64encode(
        json.dumps({"v": 0, "f": fingerprint, "k": "misc__alpha"}, sort_keys=True, separators=(",", ":")).encode(),
    ).rstrip(b"=").decode()
    wrong_fingerprint = base64.urlsafe_b64encode(
        json.dumps({"v": 1, "f": "deadbeef", "k": "misc__alpha"}, sort_keys=True, separators=(",", ":")).encode(),
    ).rstrip(b"=").decode()

    for garbage in ("!!not-base64!!", legacy_version, wrong_fingerprint):
        listed = await _list(deps, {"limit": 1, "cursor": garbage})
        found = await _find(deps, {"query": "misc", "limit": 1, "cursor": garbage})
        assert listed == _bad_cursor()
        assert found == _bad_cursor()
        assert "items" not in listed and "items" not in found


@pytest.mark.asyncio
async def test_filter_and_cursor_combo(fake_storage_provider) -> None:
    deps = await _rbac_deps(fake_storage_provider)

    page_one = await _list(deps, {"toolset_id": "misc", "limit": 1})
    page_two = await _list(deps, {"toolset_id": "misc", "limit": 1, "cursor": page_one["next_cursor"]})

    assert [item["scoped_id"] for item in page_one["items"]] == ["misc__alpha"]
    assert [item["scoped_id"] for item in page_two["items"]] == ["misc__beta"]
    assert page_two["next_cursor"] is None

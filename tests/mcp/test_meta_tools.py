"""Tools Universal M2 MCP meta-tool contracts."""

from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import datetime, timezone
from typing import Any

import jsonschema
import pytest

from primer.mcp.exposure import ExposureDeps, update_exposure
from primer.mcp.meta_tools import (
    call_tool,
    find_tools,
    invoke_meta_tool,
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


# ---- Bounded argument validation (review 7.1b) ----------------------------
#
# ``call_tool`` validates the target's arguments against the target's own
# ``args_schema`` (a remote MCP server's, for an external toolset), so the
# validation runs bounded and off the event loop: a backtracking ``pattern``
# must not hold the API loop for every request on the replica.


_TOOL_UNAVAILABLE = {
    "ok": False,
    "error": {"code": "tool_unavailable", "message": "Tool is unavailable."},
}


async def _expose_rbac(fake_storage_provider) -> None:
    await fake_storage_provider.get_storage(McpExposure).create(
        McpExposure(
            enabled=True,
            mode="rbac",
            allowed_tools=[],
            updated_at=datetime.now(timezone.utc),
        )
    )


def _deps_with_schema(
    storage, schema: dict,
) -> tuple[ExposureDeps, FakeToolsetProvider]:
    tool = _make_tool("misc", "alpha", "Alpha reader.", args_schema=schema).model_copy(
        update={"read_only": True},
    )
    provider = FakeToolsetProvider("misc", [tool], roles={"alpha": "user"})
    return (
        ExposureDeps(
            storage_provider=storage,
            provider_registry=FakeProviderRegistry({"misc": provider}),
        ),
        provider,
    )


async def _call(deps: ExposureDeps, arguments: dict) -> dict:
    actor_token = current_actor.set(_actor())
    scope_token = current_api_token_scopes.set(["mcp"])
    principal_token = current_principal.set("user@example.com")
    try:
        return await call_tool(deps, {"scoped_id": "misc__alpha", "arguments": arguments})
    finally:
        current_principal.reset(principal_token)
        current_api_token_scopes.reset(scope_token)
        current_actor.reset(actor_token)


@pytest.mark.asyncio
async def test_backtracking_schema_refuses_promptly_without_stalling_the_loop(
    fake_storage_provider,
) -> None:
    """A remote schema whose pattern backtracks must not hold the event
    loop: the call answers within seconds and a 10 ms heartbeat never
    misses by more than 1 s (the old inline ``jsonschema.validate``
    stalled the loop for tens of seconds on this payload)."""
    schema = {"type": "object", "properties": {"q": {"type": "string", "pattern": "^(a|aa)+$"}}}
    deps, _provider = _deps_with_schema(fake_storage_provider, schema)
    await _expose_rbac(fake_storage_provider)

    gaps: list[float] = []
    started = time.perf_counter()

    async def heartbeat() -> None:
        while True:
            last = time.perf_counter()
            await asyncio.sleep(0.01)
            gaps.append(time.perf_counter() - last)

    hb = asyncio.create_task(heartbeat())
    try:
        result = await _call(deps, {"q": "a" * 40 + "b"})
    finally:
        hb.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await hb
    elapsed = time.perf_counter() - started

    assert result == _TOOL_UNAVAILABLE, result
    assert gaps, "the heartbeat never ran"
    assert max(gaps) < 1.0, f"the event loop stalled for {max(gaps):.3f} s"
    assert elapsed < 5.0, f"the call took {elapsed:.1f} s"


@pytest.mark.asyncio
async def test_overlong_pattern_answers_tool_unavailable(fake_storage_provider) -> None:
    """A stored pattern the engine cannot price (over the size cap) is a
    broken schema: the same non-oracular refusal, and no match ever runs."""
    schema = {"type": "object", "properties": {"q": {"type": "string", "pattern": "a" * 1500}}}
    deps, _provider = _deps_with_schema(fake_storage_provider, schema)
    await _expose_rbac(fake_storage_provider)

    result = await _call(deps, {"q": "x"})

    assert result == _TOOL_UNAVAILABLE


@pytest.mark.asyncio
async def test_valid_arguments_still_dispatch(
    fake_storage_provider, monkeypatch,
) -> None:
    """The bounded validator must not refuse a legitimate call."""
    schema = {
        "type": "object",
        "properties": {"n": {"type": "integer"}},
        "required": ["n"],
    }
    deps, provider = _deps_with_schema(fake_storage_provider, schema)
    await _expose_rbac(fake_storage_provider)

    seen = {}

    async def fake_invoke(**kwargs):
        seen.update(kwargs)
        from primer.model.chat import ToolCallResult

        return ToolCallResult(output="ok", is_error=False)

    monkeypatch.setattr("primer.mcp.meta_tools.invoke_exposed", fake_invoke)

    result = await _call(deps, {"n": 1})

    assert result == {"ok": True, "output": "ok"}
    assert seen["arguments"] == {"n": 1}
    assert provider.calls == []


@pytest.mark.asyncio
async def test_invalid_arguments_keep_the_existing_message(
    fake_storage_provider,
) -> None:
    """Ordinary invalid input keeps the existing invalid_arguments
    envelope (message template unchanged); the tool never runs."""
    schema = {
        "type": "object",
        "properties": {"n": {"type": "integer"}},
        "required": ["n"],
    }
    deps, provider = _deps_with_schema(fake_storage_provider, schema)
    await _expose_rbac(fake_storage_provider)

    result = await _call(deps, {"n": "not-an-int"})

    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_arguments"
    assert result["error"]["message"].startswith(
        "Arguments do not match the tool schema:",
    )
    assert "not-an-int" in result["error"]["message"]
    assert provider.calls == []


@pytest.mark.asyncio
async def test_malformed_target_schema_answers_tool_unavailable(
    fake_storage_provider,
) -> None:
    """A target schema that is not a valid JSON Schema is the tool's
    problem: the existing tool_unavailable answer, not a 500."""
    schema = {"type": "not-a-real-type"}
    deps, _provider = _deps_with_schema(fake_storage_provider, schema)
    await _expose_rbac(fake_storage_provider)

    result = await _call(deps, {"n": 1})

    assert result == _TOOL_UNAVAILABLE


@pytest.mark.asyncio
async def test_invoke_meta_tool_refuses_an_overdeep_instance(
    fake_storage_provider,
) -> None:
    """The meta schemas are fixed, but the instance is the caller's: a
    payload too deep to validate refuses as a validation error (bounded,
    off the loop) instead of stalling the loop or raising RecursionError.

    The deep value sits under call_tool's ``arguments`` field (an object
    the schema does not constrain further): it is structurally valid, so
    the refusal is the depth bound, not a type mismatch - the unbounded
    ``jsonschema.validate`` the original code ran would have validated it
    fine and moved on to dispatch."""
    nested: Any = None
    for _ in range(500):
        nested = {"q": nested}
    deps = ExposureDeps(
        storage_provider=fake_storage_provider,
        provider_registry=FakeProviderRegistry({}),
    )

    with pytest.raises(jsonschema.exceptions.ValidationError):
        await invoke_meta_tool(
            "call_tool", {"scoped_id": "misc__alpha", "arguments": {"args": nested}}, deps,
        )


# --- Fix round 1: the size bound is the app's request-body cap --------------
#
# The body-cap middleware admits MCP posts up to the app's
# ``limits.max_body_bytes`` (32 MiB by default), so ``call_tool`` must not
# refuse a body it let through at the historical 1 MiB default. The deps
# here carry that cap exactly as the MCP mount wires it (``_app_mcp.py``
# reads ``app.state.config.limits.max_body_bytes``); ``AppConfig()`` is
# the same default config the test app installs.


def _app_cap() -> int:
    from primer.api.config import AppConfig

    return AppConfig().limits.max_body_bytes


def _write_like_deps(
    fake_storage_provider,
) -> tuple[ExposureDeps, FakeToolsetProvider]:
    """A write-like target (a single string property) with the app's cap."""
    schema = {"type": "object", "properties": {"content": {"type": "string"}}}
    tool = _make_tool("misc", "alpha", "Alpha writer.", args_schema=schema)
    provider = FakeToolsetProvider("misc", [tool], roles={"alpha": "user"})
    return (
        ExposureDeps(
            storage_provider=fake_storage_provider,
            provider_registry=FakeProviderRegistry({"misc": provider}),
            max_body_bytes=_app_cap(),
        ),
        provider,
    )


async def _dispatch_stub(monkeypatch) -> dict:
    seen: dict = {}

    async def fake_invoke(**kwargs):
        seen.update(kwargs)
        from primer.model.chat import ToolCallResult

        return ToolCallResult(output="ok", is_error=False)

    monkeypatch.setattr("primer.mcp.meta_tools.invoke_exposed", fake_invoke)
    return seen


@pytest.mark.asyncio
async def test_a_two_mib_tool_argument_validates_and_dispatches(
    fake_storage_provider, monkeypatch,
) -> None:
    """2 MiB of content on a write-like target: over the 1 MiB default,
    under the app's 32 MiB cap - it validates, dispatches, and the tool
    receives the full arguments."""
    deps, provider = _write_like_deps(fake_storage_provider)
    await _expose_rbac(fake_storage_provider)
    seen = await _dispatch_stub(monkeypatch)

    content = "a" * (2 * 1024 * 1024)
    result = await _call(deps, {"content": content})

    assert result == {"ok": True, "output": "ok"}, result
    assert seen["arguments"] == {"content": content}
    assert provider.calls == []


@pytest.mark.asyncio
async def test_a_400_000_cjk_character_argument_validates_and_dispatches(
    fake_storage_provider, monkeypatch,
) -> None:
    """400,000 CJK characters: 1.2 MiB of real UTF-8, but 2.4 MiB under
    the old ASCII-escaped measurement (``ensure_ascii=True`` inflated
    every character to six bytes) - the size is the instance's real
    UTF-8 JSON, so it validates and dispatches."""
    deps, _provider = _write_like_deps(fake_storage_provider)
    await _expose_rbac(fake_storage_provider)
    await _dispatch_stub(monkeypatch)

    content = "字" * 400_000
    assert len(content.encode()) == 1_200_000

    result = await _call(deps, {"content": content})

    assert result == {"ok": True, "output": "ok"}, result


@pytest.mark.asyncio
async def test_an_instance_over_the_body_cap_is_still_refused(
    fake_storage_provider, monkeypatch,
) -> None:
    """The cap is real: over the app's request-body cap the arguments are
    still refused as invalid_arguments and the tool never runs."""
    deps, provider = _write_like_deps(fake_storage_provider)
    await _expose_rbac(fake_storage_provider)
    await _dispatch_stub(monkeypatch)

    content = "a" * (33 * 1024 * 1024)  # over the 32 MiB app cap
    result = await _call(deps, {"content": content})

    assert result["ok"] is False, result
    assert result["error"]["code"] == "invalid_arguments"
    assert provider.calls == []


@pytest.mark.asyncio
async def test_a_deadline_stopped_schema_answers_tool_unavailable(
    fake_storage_provider, monkeypatch,
) -> None:
    """A remote schema the validation deadline stops (a uniqueItems over
    many objects, or heavy branching) answers the tool-side refusal, not
    invalid_arguments: the schema's slowness is the tool's problem, and
    the caller's arguments are not to blame. The deadline is the module
    default (read at call time), so a zero budget expires on the first
    subschema step of any validation."""
    import primer.common.bounded_jsonschema as bounded

    monkeypatch.setattr(bounded, "OUTPUT_SCHEMA_MATCH_SECONDS", 0)
    schema = {"type": "object", "properties": {"q": {"type": "string"}}}
    deps, provider = _deps_with_schema(fake_storage_provider, schema)
    await _expose_rbac(fake_storage_provider)

    result = await _call(deps, {"q": "x"})

    assert result == _TOOL_UNAVAILABLE, result
    assert provider.calls == []

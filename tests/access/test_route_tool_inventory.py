"""Static inventory (final review items 28 and 29): no route or tool of an access-controlled kind ships without an access check.

A route or tool that touches an owned, inherited or grantable kind must apply the access policy. This test walks every REST route
(``app.routes``) and every built-in tool, and fails on one that names a kind but whose handler shows no access check and that is not
in an allowlist WITH A REASON. A new door on a kind therefore has to be reviewed on purpose: add the check, or add the entry below and
say why none is needed. The detection is by name (the handler calls one of the access functions, or the CRUD factory was built
``owned``/``grantable``/with an inherited-access guard); it cannot prove the check is right, which the behavioural tests do.
"""
from __future__ import annotations

import ast
import inspect
import re
import textwrap
from typing import Any

import pytest
from fastapi.routing import APIRoute, APIWebSocketRoute, _IncludedRouter

#: The URL collections of the kinds that carry access (owned, grantable) or inherit it (sessions from their workspace, documents from
#: their collection, runs and approvals from theirs).
ACCESS_KINDS = frozenset({
    "agents", "graphs", "toolsets", "workspace_templates", "channels", "workspaces", "collections", "triggers", "harnesses",
    "sessions", "documents", "graph_threads", "tool_approvals", "external_tool_calls",
    "model_profiles", "llm_providers", "embedding_providers", "cross_encoder_providers", "semantic_search_providers",
    "workspace_providers", "channel_providers", "event_subscriptions",
})

#: The names that show a handler applying the access policy (a call or an attribute on one).
ACCESS_CALL = re.compile(
    r"^(person_from_request|person_for_tool|person_from_ref|require_\w+|_require\w*|level_of|visible_predicate|catalogue_predicate|"
    r"member_predicate|_?readable\w*|scope_to_readable_workspaces|workspace_level|may_use\w*|\w*_in_reach|set_access|get_access_view|"
    r"lookup_users|lookup_user_names|_owned_person|_visibility|owned_guard|collection_for|_workspace_guard|_access_guard|"
    r"\w*access_(refusal|check|guard)\w*|\w*read_refusal|toolset_use|view_for|may_manage_trigger|ensure_default_for_request|make_access_router|enforce_approvers)$"
)

#: (method, path) -> why the route needs no per-user access check. An entry is a review decision, not a shortcut.
ROUTE_ALLOWLIST: dict[tuple[str, str], str] = {
    ("GET", "/v1/toolsets/builtin"): "static catalogue of the built-in toolset ids and descriptions; no per-user row is read",
    ("POST", "/v1/workspaces/{workspace_id}/pause"): "reserved: answers 501 and reads no row",
    ("POST", "/v1/workspaces/{workspace_id}/resume"): "reserved: answers 501 and reads no row",
}

#: tool id -> why the tool needs no per-user access check (a tool of an access kind whose handler shows none).
TOOL_ALLOWLIST: dict[str, str] = {
    "workspace_ext/invoke_graph": "the child graph is resolved by the run's graph_resolver (worker/executor_builders.py), which answers a graph the run's person holds no level on as not found",
    "workspaces/message_session": "the send goes through session.peer_messages._send_from_session, which asks FULL on the target's workspace and answers an unreadable one as unknown",
}


def _identifiers(fn: Any) -> set[str]:
    """Every name and attribute the function body mentions (comments and docstrings are not code)."""
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    except (OSError, TypeError, SyntaxError):
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    return names


def _closure(fn: Any) -> dict[str, Any]:
    code = getattr(fn, "__code__", None)
    cells = getattr(fn, "__closure__", None) or ()
    out: dict[str, Any] = {}
    for name, cell in zip(code.co_freevars if code else (), cells):
        try:
            out[name] = cell.cell_contents
        except ValueError:
            continue
    return out


def _guarded(fn: Any, *, depth: int = 0) -> bool:
    """Whether the handler (or a helper it is built from) shows an access check."""
    if any(ACCESS_CALL.match(name) for name in _identifiers(fn)):
        return True
    closure = _closure(fn)
    # make_crud_router / _crud_tools_for: the factory was built with the access flags or an inherited-access hook.
    if any(closure.get(flag) for flag in ("owned", "grantable", "row_guard", "list_filter", "guard")):
        return True
    if depth < 2:
        return any(_guarded(v, depth=depth + 1) for v in closure.values() if inspect.isfunction(v))
    return False


def _dependency_names(deps: Any) -> set[str]:
    names: set[str] = set()
    for dep in deps:
        call = getattr(dep, "dependency", None) or getattr(dep, "call", None)
        if call is not None:
            names.add(getattr(call, "__name__", str(call)))
    return names


def _route_dependency_names(route: Any) -> set[str]:
    names: set[str] = set()

    def walk(dependant: Any) -> None:
        for sub in dependant.dependencies:
            if sub.call is not None:
                names.add(getattr(sub.call, "__name__", str(sub.call)))
            walk(sub)

    if hasattr(route, "dependant"):
        walk(route.dependant)
    return names


def _walk_routes(routes: Any, prefix: str = "", inherited: frozenset[str] = frozenset()):
    for route in routes:
        if isinstance(route, _IncludedRouter):
            ctx = route.include_context
            yield from _walk_routes(route.original_router.routes, prefix + ctx.prefix, inherited | _dependency_names(ctx.dependencies))
        elif isinstance(route, (APIRoute, APIWebSocketRoute)):
            methods = sorted(getattr(route, "methods", None) or ["WS"])
            yield prefix + route.path, methods, route.endpoint, inherited | _route_dependency_names(route)


@pytest.fixture(scope="module")
def routes():
    from primer.api.app import create_test_app
    from primer.api.registries import ProviderRegistry
    from tests.conftest import _FakeStorageProvider

    sp = _FakeStorageProvider()
    registry = ProviderRegistry(
        sp, llm_factory=lambda p: object(), embedder_factory=lambda p: object(), cross_encoder_factory=lambda p: object(),
        toolset_factory=lambda p: object(),
    )
    app = create_test_app(storage_provider=sp, provider_registry=registry)
    return list(_walk_routes(app.routes))


def _kind_of(path: str) -> str | None:
    parts = [p for p in path.split("/") if p]
    return parts[1] if len(parts) > 1 and parts[0] == "v1" and parts[1] in ACCESS_KINDS else None


def test_every_route_of_an_access_kind_applies_the_access_policy_or_is_allowlisted(routes):
    unreviewed = []
    for path, methods, endpoint, deps in routes:
        # An admin holds owner on everything, so an admin-tier route (``require_admin`` or a gate named after it) needs no per-user check.
        if _kind_of(path) is None or any("require_admin" in name for name in deps):
            continue
        if _guarded(endpoint) or any((m, path) in ROUTE_ALLOWLIST for m in methods):
            continue
        unreviewed.append(f"{','.join(methods)} {path} ({endpoint.__module__}:{endpoint.__name__})")
    assert not unreviewed, (
        "routes of an access-controlled kind with no visible access check; apply the policy or add a ROUTE_ALLOWLIST entry with a reason:\n"
        + "\n".join(unreviewed)
    )


def test_every_allowlisted_route_still_exists_and_carries_a_reason(routes):
    live = {(m, path) for path, methods, _, _ in routes for m in methods}
    stale = [key for key in ROUTE_ALLOWLIST if key not in live]
    assert not stale, f"allowlist entries for routes that no longer exist: {stale}"
    assert all(len(reason) >= 10 for reason in ROUTE_ALLOWLIST.values())


def test_the_inventory_actually_sees_the_routes(routes):
    """A guard against a walker that silently finds nothing."""
    seen = {k for path, _, _, _ in routes if (k := _kind_of(path))}
    assert {"triggers", "workspaces", "sessions", "collections", "agents"} <= seen
    assert len(routes) > 300


# -- tools --------------------------------------------------------------------------------------------------------------------------------------

#: The words in a tool id that name an access-controlled kind (a tool of one of these answers for its rows).
TOOL_KIND_WORDS = re.compile(
    r"(agent|graph|toolset|template|channel|workspace|collection|document|trigger|harness|session|approval|model_profile|provider|"
    r"subscription|thread|event|tool_call)"
)


def _all_tools() -> dict[str, tuple[Any, Any]]:
    """Every built-in tool of the toolsets an agent or an MCP client can call, as ``{toolset/tool: (tool, handler)}``."""
    import asyncio
    import pathlib
    import tempfile
    from unittest.mock import MagicMock

    from primer.api.registries.workspace_registry import WorkspaceRegistry
    from primer.model.provider import SqliteConfig
    from primer.storage.sqlite import SqliteStorageProvider
    from primer.toolset.canvas import build_canvas_toolset
    from primer.toolset.collections import build_collections_toolset
    from primer.toolset.crud import build_crud_toolset
    from primer.toolset.harness import build_harness_toolset_provider
    from primer.toolset.misc import build_misc_toolset
    from primer.toolset.system import build_system_toolset
    from primer.toolset.trigger import build_trigger_toolset_provider
    from primer.toolset.workspace_ext import build_workspace_ext_toolset
    from primer.toolset.workspaces import build_workspaces_toolset

    async def build() -> dict[str, tuple[Any, Any]]:
        sp = SqliteStorageProvider(SqliteConfig(path=pathlib.Path(tempfile.mkdtemp()) / "t.sqlite"))
        await sp.initialize()
        toolsets = {
            "system": build_system_toolset(storage_provider=sp, provider_registry=MagicMock()),
            "collections": build_collections_toolset(storage_provider=sp),
            "crud": build_crud_toolset(storage_provider=sp),
            "misc": build_misc_toolset(),
            "workspace_ext": build_workspace_ext_toolset(storage_provider=sp),
            "workspaces": build_workspaces_toolset(storage_provider=sp, workspace_registry=WorkspaceRegistry(sp)),
            "canvas": build_canvas_toolset(storage_provider=sp),
            "trigger": build_trigger_toolset_provider(storage_provider=sp),
            "harness": build_harness_toolset_provider(storage_provider=sp),
        }
        out: dict[str, tuple[Any, Any]] = {}
        for name, toolset in toolsets.items():
            registry = getattr(toolset, "_registry", None) or getattr(toolset, "_tools", None)
            if registry is None:
                registry = next(
                    (v for v in vars(toolset).values() if isinstance(v, dict) and v and isinstance(next(iter(v.values())), tuple)), {}
                )
            for tool_id, pair in registry.items():
                out[f"{name}/{tool_id}"] = pair
        await sp.aclose()
        return out

    return asyncio.run(build())


@pytest.fixture(scope="module")
def tools():
    return _all_tools()


def test_every_tool_of_an_access_kind_applies_the_access_policy_or_is_allowlisted(tools):
    unreviewed = []
    for key, (tool, handler) in sorted(tools.items()):
        tool_id = key.split("/", 1)[1]
        if not TOOL_KIND_WORDS.search(tool_id) or getattr(tool, "required_role", None) == "admin":
            continue  # not about a kind, or admin-only (an admin holds owner on everything)
        if _guarded(handler) or key in TOOL_ALLOWLIST or tool_id in TOOL_ALLOWLIST:
            continue
        unreviewed.append(key)
    assert not unreviewed, (
        "tools of an access-controlled kind with no visible access check; apply the policy or add a TOOL_ALLOWLIST entry with a reason:\n"
        + "\n".join(unreviewed)
    )


def test_every_allowlisted_tool_still_exists_and_carries_a_reason(tools):
    ids = set(tools) | {key.split("/", 1)[1] for key in tools}
    assert not [key for key in TOOL_ALLOWLIST if key not in ids]
    assert all(len(reason) >= 10 for reason in TOOL_ALLOWLIST.values())


def test_the_tool_inventory_actually_sees_the_tools(tools):
    assert len(tools) > 150 and any(key.startswith("system/") for key in tools) and any(key.startswith("trigger/") for key in tools)


def test_the_detector_tells_a_guarded_handler_from_an_unguarded_one():
    from primer.access.person import person_from_request

    async def unguarded(request, row_id):
        return await request.app.state.storage.get(row_id)

    async def guarded(request, row_id):
        person_from_request(request)
        return await request.app.state.storage.get(row_id)

    assert not _guarded(unguarded) and _guarded(guarded)

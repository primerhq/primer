"""The additive console coverage contract and its REST-derived visibility."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import APIRouter, Depends, FastAPI, Request
from httpx import ASGITransport, AsyncClient

from primer.api.route_tools import (
    admin_operation_keys, build_generated_route_toolsets, coverage_projection, route_tool,
)
from primer.mcp.exposure import ExposureDeps, list_available_tools, update_exposure
from primer.model.chat import Tool
from primer.model.provider import Toolset
from primer.model.principal import Principal
from primer.toolset.internal import InternalToolsetProvider


@pytest.fixture
async def user(client, app):
    created = await client.post("/v1/admin/users", json={
        "username": "catalogue-user", "password": "catalogue-password", "role": "user",
    })
    assert created.status_code == 201, created.text
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as other:
        login = await other.post("/v1/auth/login", json={
            "username": "catalogue-user", "password": "catalogue-password",
        })
        assert login.status_code == 200, login.text
        yield other


async def _catalogue(client):
    response = await client.get("/v1/tools/catalogue")
    assert response.status_code == 200, response.text
    return response.json()


async def test_admin_fields_match_projection_and_all_nine_curated_pairs(client, app):
    body = await _catalogue(client)
    items = {item["id"]: item for item in body["items"]}
    rows = coverage_projection(app)
    for item in items.values():
        toolset, bare = item["id"].split("__", 1)
        assert item["origin"] == (
            "generated" if toolset in app.state.generated_route_toolsets.providers else "curated"
        )
        assert item["reference_path"] == f"reference/tools/{toolset}/{bare}"
        assert "operation" in item and "default_approval" in item
    generated = [row for row in rows if row.state == "generated"]
    assert len(generated) == 261
    for row in generated:
        assert items[row.tool_ids[0]]["operation"] == {
            "operation_id": row.operation_id, "method": row.method, "path": row.path,
        }
    pairs = [row for row in rows if len(row.tool_ids) == 2]
    assert len(pairs) == 9
    for row in pairs:
        left, right = (items[scoped] for scoped in row.tool_ids)
        assert left["operation"] == right["operation"] == {
            "operation_id": row.operation_id, "method": row.method, "path": row.path,
        }
        assert left["origin"] == right["origin"] == "curated"


async def test_user_keeps_picker_ids_but_gets_no_admin_operation_or_exposure(client, user, app):
    admin = await _catalogue(client)
    regular = await _catalogue(user)
    assert [i["id"] for i in regular["items"]] == [i["id"] for i in admin["items"]]
    assert "coverage_counts" not in regular
    hidden = admin_operation_keys(app)
    manifest = json.loads(Path("scripts/data/tools_universal_m0_manifest.json").read_text())
    expected_hidden = {(r["method"], r["path"]) for r in manifest["rows"] if r["mount_auth"] == "admin"}
    assert hidden == expected_hidden and len(hidden) == 154
    admin_items = {i["id"]: i for i in admin["items"]}
    redacted = 0
    for item in regular["items"]:
        source = admin_items[item["id"]]
        operation = source["operation"]
        if operation and (operation["method"], operation["path"]) in hidden:
            assert "operation" not in item
            redacted += 1
        else:
            assert item["operation"] == operation
        assert item["exposure"] is None
        assert item["default_approval"] == source["default_approval"]
    assert redacted > 100
    # These reads have stricter curated tool roles but user-visible REST mounts.
    assert next(i for i in regular["items"] if i["id"] == "system__get_canvas_settings")["operation"] is not None


async def test_reviewed_skips_counts_and_shipped_approval(client, user):
    body = await _catalogue(client)
    manifest = json.loads(Path("scripts/data/tools_universal_m0_manifest.json").read_text())
    expected = {(r["operation_id"], r["method"], r["path"], r["skip_code"])
                for r in manifest["rows"] if r["state"] == "skip"}
    assert len(body["skips"]) == 8
    assert {(r["operation_id"], r["method"], r["path"], r["skip_code"])
            for r in body["skips"]} == expected
    assert all(row["reason"] == "human sign in" for row in body["skips"])
    assert (await _catalogue(user))["skips"] == body["skips"]
    assert body["coverage_counts"] == {"operations": 404, "generated": 261, "curated": 135, "skip": 8}
    items = {i["id"]: i for i in body["items"]}
    assert items["internal_collections__put_config"]["default_approval"] == "required"
    assert items["internal_collections__get_config"]["default_approval"] is None
    assert items["system__create_toolset"]["default_approval"] == "policy"


@pytest.mark.parametrize("mode", ["rbac", "allowlist"])
async def test_exposure_uses_the_same_source_as_available(client, app, mode):
    deps = ExposureDeps(app.state.storage_provider, app.state.provider_registry)
    await update_exposure(enabled=True, mode=mode, allowed_tools=["crud__list_python_tools"], updated_by="test", deps=deps)
    available = {i["scoped_id"]: i for i in await list_available_tools(deps)}
    body = await _catalogue(client)
    for item in body["items"]:
        other = available[item["id"]]
        assert item["exposure"] == {key: other[key] for key in (
            "exposable", "reason", "currently_allowed",
        )} | {"mode": mode}
    items = {i["id"]: i for i in body["items"]}
    assert items["system__ask_user"]["exposure"]["reason"] == "yielding_unsupported"
    assert items["crud__list_python_tools"]["exposure"]["currently_allowed"] is True


async def test_user_toolset_has_no_operation_and_cannot_be_exposed(client, app, monkeypatch):
    row = Toolset(id="m6-user-tools", provider="python", config={"source": "", "source_version": 1})
    await app.state.storage_provider.get_storage(Toolset).create(row)
    tool = Tool(id="probe", toolset_id=row.id, description="Probe", args_schema={})

    async def call(arguments):
        raise AssertionError("catalogue enumeration must never call a tool")

    provider = InternalToolsetProvider(row.id, {"probe": (tool, call)})
    registry = app.state.provider_registry
    original = registry.get_toolset

    async def resolve(toolset_id, **kwargs):
        return provider if toolset_id == row.id else await original(toolset_id, **kwargs)

    monkeypatch.setattr(registry, "get_toolset", resolve)
    item = next(i for i in (await _catalogue(client))["items"] if i["id"] == f"{row.id}__probe")
    assert item["origin"] == "user" and item["operation"] is None
    assert item["reference_path"] == "reference/tools/m6-user-tools/probe"
    assert item["exposure"]["exposable"] is False
    assert item["exposure"]["reason"] == "not_system_toolset"


async def test_catalogue_bytes_are_deterministic(client):
    first = await client.get("/v1/tools/catalogue")
    second = await client.get("/v1/tools/catalogue")
    assert first.status_code == second.status_code == 200
    assert first.content == second.content


async def test_future_admin_skip_is_hidden_from_users(app):
    from primer.api.deps import require_admin
    from primer.api.routers.tools import list_tools, tools_router

    probe = FastAPI()
    router = APIRouter()

    @router.get("/v1/m6-admin-skip", dependencies=[Depends(require_admin)])
    @route_tool(mapping_state="skip", skip_code="future_admin_skip")
    async def future_skip():
        return {}

    probe.include_router(router)
    probe.include_router(tools_router, prefix="/v1")
    build_generated_route_toolsets(probe)

    async def read(role):
        request = Request({"type": "http", "app": probe, "state": {
            "actor": Principal(type="user", id="m6-reader", user_id="m6-reader", role=role,
                               display="M6 reader", source="local"),
        }})
        return await list_tools(request, principal=None, registry=app.state.provider_registry,
                                storage_provider=app.state.storage_provider)

    assert len((await read("admin"))["skips"]) == 1
    regular = await read("user")
    assert regular["skips"] == []
    assert "coverage_counts" not in regular


async def test_full_lifespan_catalogue_covers_the_reviewed_tool_inventory(tmp_path, monkeypatch):
    from primer.api.app import create_app
    from primer.api.config import AppConfig
    from primer.model.scheduler import RuntimeMode

    monkeypatch.setenv("PRIMER_WIKI_DIR", str(tmp_path))
    app = create_app(AppConfig(
        runtime_mode=RuntimeMode.API, auto_bootstrap=False,
        auth={"enabled": False},
        db={"provider": "sqlite", "config": {"path": tmp_path / "m6.sqlite"}},
    ))
    manifest = json.loads(Path("scripts/data/tools_universal_m0_manifest.json").read_text())
    reviewed = {tool["scoped_id"] for tool in manifest["tool_catalogue"]}
    assert len(reviewed) == 487
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            body = await _catalogue(client)
        items = {item["id"]: item for item in body["items"]}
        assert reviewed <= items.keys()
        for row in coverage_projection(app):
            for scoped in row.tool_ids:
                assert items[scoped]["operation"] == {
                    "operation_id": row.operation_id, "method": row.method, "path": row.path,
                }
        assert all(item["origin"] in {"generated", "curated"} for item in items.values())


def test_admin_reader_uses_nested_mounts_and_real_dependencies_not_tool_role():
    async def require_admin():
        return None

    async def wrapped_admin(value=Depends(require_admin)):
        return value

    app = FastAPI()
    router = APIRouter()

    @router.get("/hidden/{item:path}")
    @route_tool(mapping_state="skip", skip_code="future_admin_skip")
    async def hidden(item: str):
        return item

    app.include_router(router, prefix="/v1", dependencies=[Depends(wrapped_admin)])

    @app.get("/v1/visible")
    @route_tool(scoped_id="m6__visible", required_role="admin")
    async def visible():
        return {}

    build_generated_route_toolsets(app)
    assert admin_operation_keys(app) == frozenset({("GET", "/v1/hidden/{item}")})
    assert admin_operation_keys(app) is admin_operation_keys(app)

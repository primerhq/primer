"""Focused contracts for generated REST route tools."""

from __future__ import annotations

import asyncio
import base64
import gc
import json
import logging
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi import APIRouter, Depends, FastAPI, File, Form, Header, Request, Security, UploadFile
from fastapi.responses import Response, StreamingResponse
from fastapi.security import OAuth2PasswordBearer
from starlette.middleware.gzip import GZipMiddleware
from pydantic import BaseModel

import primer.api.route_tools as route_tools_module
from primer.access.acting import acting_as, acting_person
from primer.access.person import Person
from primer.auth.tokens import sign_session
from primer.api.config import AppConfig
from primer.api.app import create_app, create_test_app
from primer.api.deps import require_admin, require_user
from primer.api.errors import register_error_handlers
from primer.api.middleware.auth import AuthMiddleware
from primer.api.registries.provider_registry import ProviderRegistry
from primer.api.route_tools import (
    RouteToolMetadata,
    build_generated_route_toolsets,
    derive_argument_schema,
    route_tool,
)
from primer.model.except_ import ConflictError, NotFoundError
from primer.model.except_ import ConfigError
from primer.model.user import User
from primer.model.tool_approval import ToolApprovalPolicy
from primer.bootstrap.seed import ensure_crud_approval_policies
from primer.agent.approval import ApprovalResolver
from primer.toolset import crud as crud_toolset
from primer.toolset._system_guards import AGENT_GUARDS, refuse_create
from primer.toolset.tool_catalog import collect_tool_entries
from tests.conftest import _FakeStorageProvider


def test_argument_schema_derives_path_query_header_file_alias_union_and_defs() -> None:
    operation = {
        "parameters": [
            {"in": "path", "name": "item_id", "required": True, "schema": {"type": "string"}},
            {
                "in": "query",
                "name": "limit",
                "required": False,
                "schema": {"anyOf": [{"type": "integer"}, {"type": "null"}], "default": 10},
            },
            {"in": "header", "name": "X-Trace", "required": False, "schema": {"type": "string"}},
        ],
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "upload": {"type": "string", "format": "binary"},
                            "model": {"$ref": "#/components/schemas/Payload"},
                        },
                        "required": ["upload"],
                    }
                }
            },
        },
    }
    metadata = RouteToolMetadata(
        argument_overrides=(
            ("body", "form"),
            ("header.X-Trace", "trace_id"),
            ("path.item_id", "id"),
        )
    )

    schema, bindings = derive_argument_schema(
        operation,
        {
            "schemas": {
                "Payload": {
                    "type": "object",
                    "properties": {"inner": {"$ref": "#/components/schemas/Inner"}},
                },
                "Inner": {"type": "object", "properties": {"name": {"type": "string"}}},
                "Unused": {"type": "string"},
            }
        },
        metadata,
    )

    assert schema["required"] == ["form", "id"]
    assert schema["properties"]["limit"]["anyOf"][-1] == {"type": "null"}
    assert schema["properties"]["limit"]["default"] == 10
    assert schema["properties"]["form"]["properties"]["upload"]["format"] == "binary"
    assert schema["properties"]["form"]["properties"]["upload"]["contentEncoding"] == "base64"
    assert schema["properties"]["form"]["properties"]["upload"]["contentMediaType"] == "application/octet-stream"
    assert schema["properties"]["form"]["properties"]["model"]["$ref"] == "#/$defs/Payload"
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$defs"]["Payload"]["properties"]["inner"]["$ref"] == "#/$defs/Inner"
    assert schema["$defs"]["Inner"]["properties"]["name"]["type"] == "string"
    assert "Unused" not in schema["$defs"]
    assert {(item.argument, item.wire_name, item.location) for item in bindings} == {
        ("id", "item_id", "path"),
        ("limit", "limit", "query"),
        ("trace_id", "X-Trace", "header"),
        ("form", "body", "body"),
    }


def test_no_body_operation_has_closed_empty_object_schema() -> None:
    schema, bindings = derive_argument_schema({}, {}, RouteToolMetadata())
    assert schema == {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    assert bindings == ()


def test_multipart_binding_ref_resolution_rejects_component_cycles() -> None:
    operation = {
        "requestBody": {
            "content": {
                "multipart/form-data": {
                    "schema": {"$ref": "#/components/schemas/First"}
                }
            }
        }
    }
    components = {
        "schemas": {
            "First": {"$ref": "#/components/schemas/Second"},
            "Second": {"$ref": "#/components/schemas/First"},
        }
    }
    with pytest.raises(ConfigError, match="component reference cycle"):
        derive_argument_schema(operation, components, RouteToolMetadata())


def test_metadata_is_immutable_and_collisions_require_a_stable_override() -> None:
    metadata = RouteToolMetadata(resource_id="widgets", tool_id="read")
    with pytest.raises(FrozenInstanceError):
        metadata.tool_id = "changed"  # type: ignore[misc]

    app = FastAPI()

    @app.get("/v1/one")
    @route_tool(resource_id="widgets", tool_id="read")
    async def one() -> dict[str, int]:
        return {"value": 1}

    @app.get("/v1/two")
    @route_tool(resource_id="widgets", tool_id="read")
    async def two() -> dict[str, int]:
        return {"value": 2}

    with pytest.raises(ConfigError, match="generated scoped id collision"):
        build_generated_route_toolsets(app)

    route_tool(resource_id="widgets", tool_id="read_two")(two)
    # Remove the earlier declaration from the same endpoint before applying
    # the explicit stable override: the public decorator refuses ambiguity.
    two.__primer_route_tool_metadata__ = (two.__primer_route_tool_metadata__[-1],)  # type: ignore[attr-defined]
    snapshot = build_generated_route_toolsets(app)
    assert {
        f"{operation.tool.toolset_id}__{operation.tool.id}"
        for operation in snapshot.operations
    } == {"widgets__read", "widgets__read_two"}
    assert build_generated_route_toolsets(app) is snapshot
    assert all(
        operation.metadata.scoped_id is not None
        or (
            operation.metadata.resource_id is not None
            and operation.metadata.tool_id is not None
        )
        for operation in snapshot.operations
    )


def test_crud_declaration_shares_the_curated_tool_guard_object_and_verb_split() -> None:
    from primer.api.routers.compute import AGENT_ROUTE_TOOLS

    assert AGENT_ROUTE_TOOLS.guards is AGENT_GUARDS
    assert AGENT_ROUTE_TOOLS.generated_operations == {"find"}
    assert AGENT_ROUTE_TOOLS.curated_operations == {
        "create",
        "get",
        "update",
        "delete",
        "list",
    }
    assert AGENT_ROUTE_TOOLS.metadata_for("find").mapping_state == "generated"
    assert AGENT_ROUTE_TOOLS.metadata_for("create").mapping_state == "curated"
    refusal = refuse_create(AGENT_ROUTE_TOOLS.guards, SimpleNamespace(harness_id="managed"))
    assert refusal is not None and refusal.is_error


class _Body(BaseModel):
    value: int


def _dispatch_app(*, include_slow: bool = False) -> tuple[FastAPI, object]:
    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    app.state.config = AppConfig(auth={"enabled": True})
    storage_provider = _FakeStorageProvider()
    app.state.storage_provider = storage_provider
    users = storage_provider.get_storage(User)
    users._data.update(  # noqa: SLF001 - production-shaped auth storage fixture
        {
            "alice": User(
                id="alice",
                username="alice",
                password_hash="!test",
                role="user",
                created_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
            ),
            "root": User(
                id="root",
                username="root",
                password_hash="!test",
                role="admin",
                created_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
            ),
        }
    )

    @app.post("/v1/probes/{item_id}", dependencies=[Depends(require_user)])
    @route_tool(scoped_id="dispatch_probe__probe")
    async def probe(
        request: Request,
        item_id: str,
        body: _Body,
        x_trace: str | None = Header(default=None),
    ) -> dict[str, object]:
        request.app.state.seen_person = acting_person()
        if item_id == "missing":
            raise NotFoundError("probe is invisible")
        if item_id == "taken":
            raise ConflictError("probe conflicts")
        return {
            "actor": getattr(request.state.actor, "user_id", None),
            "value": body.value,
            "trace": x_trace,
        }

    @app.get("/v1/admin-probe", dependencies=[Depends(require_admin)])
    @route_tool(scoped_id="dispatch_probe__admin_probe")
    async def admin_probe() -> dict[str, bool]:
        return {"ok": True}

    if include_slow:
        slow_router = APIRouter()

        @slow_router.get("/v1/slow")
        @route_tool(scoped_id="slow_probe__slow")
        async def slow() -> dict[str, bool]:
            await asyncio.sleep(0.1)
            return {"ok": True}

        @slow_router.get("/v1/large")
        @route_tool(scoped_id="slow_probe__large")
        async def large() -> Response:
            return Response(b"x" * 1_048_577, media_type="application/octet-stream")

        app.state.exact_stream_chunks = 0
        app.state.oversize_stream_chunks = 0
        app.state.oversize_stream_cleaned = False

        async def exact_chunks():
            for _ in range(4):
                app.state.exact_stream_chunks += 1
                yield b"e" * 262_144

        async def oversize_chunks():
            try:
                for _ in range(8):
                    app.state.oversize_stream_chunks += 1
                    yield b"o" * 300_000
            finally:
                app.state.oversize_stream_cleaned = True

        @slow_router.get("/v1/stream-exact")
        @route_tool(scoped_id="slow_probe__stream_exact")
        async def stream_exact() -> StreamingResponse:
            return StreamingResponse(
                exact_chunks(), media_type="application/octet-stream"
            )

        @slow_router.get("/v1/stream-oversize")
        @route_tool(scoped_id="slow_probe__stream_oversize")
        async def stream_oversize() -> StreamingResponse:
            return StreamingResponse(
                oversize_chunks(), media_type="application/octet-stream"
            )

        app.include_router(slow_router)

    register_error_handlers(app)
    snapshot = build_generated_route_toolsets(app)
    return app, snapshot


async def _call(provider, name: str, arguments: dict, person: Person):
    with acting_as(person):
        return await provider.call(tool_name=name, arguments=arguments)


def _tool_response(result) -> tuple[int, object]:
    payload = json.loads(result.output)
    return (int(payload["status"]) if result.is_error else 200, payload)


async def _rest_response(
    app: FastAPI,
    *,
    cookie: str,
    method: str,
    path: str,
    body: object | None = None,
) -> tuple[int, object]:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://rest",
        cookies={"primer_session": cookie},
    ) as client:
        response = await client.request(method, path, json=body)
    return response.status_code, response.json()


@pytest.mark.asyncio
async def test_trusted_dispatch_uses_exact_person_and_real_validation_errors() -> None:
    app, snapshot = _dispatch_app()
    provider = snapshot.providers["dispatch_probe"]

    secret = "route-tool-parity-secret-32-bytes"
    storage_provider = app.state.storage_provider
    app.state.session_secret = secret
    users = storage_provider.get_storage(User)
    alice = await users.get("alice")
    root = await users.get("root")
    assert alice is not None and root is not None
    cookies = {
        "alice": sign_session(user_id=alice.id, username=alice.username, secret=secret),
        "root": sign_session(user_id=root.id, username=root.username, secret=secret),
    }

    exact_person = Person("alice", "user")
    ok = await _call(
        provider,
        "probe",
        {"item_id": "one", "body": {"value": 7}},
        exact_person,
    )
    assert not ok.is_error
    assert json.loads(ok.output) == {"actor": "alice", "value": 7, "trace": None}
    assert app.state.seen_person is exact_person
    assert _tool_response(ok) == await _rest_response(
        app,
        cookie=cookies["alice"],
        method="POST",
        path="/v1/probes/one",
        body={"value": 7},
    )

    invalid = await _call(
        provider,
        "probe",
        {"item_id": "one", "body": {"value": "not-an-integer"}},
        Person("alice", "user"),
    )
    assert invalid.is_error and json.loads(invalid.output)["status"] == 422
    assert _tool_response(invalid) == await _rest_response(
        app,
        cookie=cookies["alice"],
        method="POST",
        path="/v1/probes/one",
        body={"value": "not-an-integer"},
    )

    missing = await _call(
        provider,
        "probe",
        {"item_id": "missing", "body": {"value": 1}},
        Person("alice", "user"),
    )
    assert missing.is_error and json.loads(missing.output)["status"] == 404
    assert _tool_response(missing) == await _rest_response(
        app,
        cookie=cookies["alice"],
        method="POST",
        path="/v1/probes/missing",
        body={"value": 1},
    )

    conflict = await _call(
        provider,
        "probe",
        {"item_id": "taken", "body": {"value": 1}},
        Person("alice", "user"),
    )
    assert conflict.is_error and json.loads(conflict.output)["status"] == 409
    assert _tool_response(conflict) == await _rest_response(
        app,
        cookie=cookies["alice"],
        method="POST",
        path="/v1/probes/taken",
        body={"value": 1},
    )

    refused = await _call(provider, "admin_probe", {}, Person("alice", "user"))
    assert refused.is_error and json.loads(refused.output)["status"] == 403
    assert _tool_response(refused) == await _rest_response(
        app,
        cookie=cookies["alice"],
        method="GET",
        path="/v1/admin-probe",
    )
    admitted = await _call(provider, "admin_probe", {}, Person("root", "admin"))
    assert not admitted.is_error and json.loads(admitted.output) == {"ok": True}
    assert _tool_response(admitted) == await _rest_response(
        app,
        cookie=cookies["root"],
        method="GET",
        path="/v1/admin-probe",
    )


@pytest.mark.asyncio
async def test_real_fastapi_multipart_upload_and_form_dispatch() -> None:
    app = FastAPI()

    @app.post("/v1/upload")
    @route_tool(scoped_id="upload_probe__upload")
    async def upload(
        document: UploadFile = File(...),
        label: str = Form(...),
    ) -> dict[str, object]:
        content = await document.read()
        return {
            "content_base64": base64.b64encode(content).decode("ascii"),
            "filename": document.filename,
            "label": label,
        }

    register_error_handlers(app)
    snapshot = build_generated_route_toolsets(app)
    operation = next(
        item for item in snapshot.operations if item.tool.id == "upload"
    )
    body_schema = operation.tool.args_schema["properties"]["body"]
    assert body_schema["$ref"].startswith("#/$defs/")
    component = operation.tool.args_schema["$defs"][
        body_schema["$ref"].removeprefix("#/$defs/")
    ]
    assert component["properties"]["document"]["contentEncoding"] == "base64"

    raw = b"real multipart bytes\x00\xff"
    result = await _call(
        snapshot.providers["upload_probe"],
        "upload",
        {
            "body": {
                "document": base64.b64encode(raw).decode("ascii"),
                "label": "sibling form field",
            }
        },
        Person("alice", "user"),
    )

    assert not result.is_error, result.output
    assert json.loads(result.output) == {
        "content_base64": base64.b64encode(raw).decode("ascii"),
        "filename": "document.bin",
        "label": "sibling form field",
    }


@pytest.mark.asyncio
async def test_generated_provider_joins_the_normal_catalogue_and_exposure_contract() -> None:
    app, snapshot = _dispatch_app()
    entries = await collect_tool_entries(snapshot.providers)
    probe = next(entry for entry in entries if entry.scoped_id == "dispatch_probe__probe")
    provider = snapshot.providers[probe.toolset_id]
    assert probe.required_role == "user"
    assert probe.default_gate is None

    def _unexpected_rebuild():
        raise AssertionError("dispatch rebuilt OpenAPI instead of using the compiled provider")

    app.openapi = _unexpected_rebuild  # type: ignore[method-assign]
    result = await _call(
        provider,
        "probe",
        {"item_id": "cached", "body": {"value": 2}},
        Person("alice", "user"),
    )
    assert not result.is_error


@pytest.mark.asyncio
async def test_registry_uses_one_fixed_and_generated_provider_mapping(monkeypatch) -> None:
    _app, snapshot = _dispatch_app()
    fixed = snapshot.providers["dispatch_probe"]
    generated = snapshot.providers["dispatch_probe"]
    kwargs = {
        "system_toolset_provider": fixed,
        "workspaces_toolset_provider": fixed,
        "misc_toolset_provider": fixed,
        "collections_toolset_provider": fixed,
        "web_toolset_provider": fixed,
        "harness_toolset_provider": fixed,
        "trigger_toolset_provider": fixed,
        "workspace_ext_toolset_provider": fixed,
        "crud_toolset_provider": fixed,
        "canvas_toolset_provider": fixed,
    }
    registry = ProviderRegistry(
        _FakeStorageProvider(),
        generated_api_toolset_providers={"dispatch_probe": generated},
        **kwargs,
    )
    assert registry._invalidation_subs == []  # noqa: SLF001 - constructor invariant
    assert set(registry._builtin_toolset_providers) == {  # noqa: SLF001
        "system",
        "workspaces",
        "misc",
        "collections",
        "web",
        "harness",
        "trigger",
        "workspace_ext",
        "crud",
        "canvas",
        "dispatch_probe",
    }
    async def broken_optional_settings() -> frozenset[str]:
        raise RuntimeError("canvas settings unreadable")

    monkeypatch.setattr(registry, "toolsets_off_by_design", broken_optional_settings)
    assert await registry.get_toolset("_system") is fixed
    assert await registry.get_toolset("dispatch_probe") is generated
    with pytest.raises(RuntimeError, match="canvas settings unreadable"):
        await registry.get_toolset("canvas")

    async def canvas_disabled() -> frozenset[str]:
        return frozenset({"canvas"})

    monkeypatch.setattr(registry, "toolsets_off_by_design", canvas_disabled)
    with pytest.raises(NotFoundError, match="canvas"):
        await registry.get_toolset("canvas")

    bare = ProviderRegistry(_FakeStorageProvider())
    assert bare._invalidation_subs == []  # noqa: SLF001


@pytest.mark.asyncio
async def test_create_test_app_replaces_its_generated_providers_on_reused_registry(
    fake_storage_provider, fake_provider_registry,
) -> None:
    first_app = create_test_app(
        storage_provider=fake_storage_provider,
        provider_registry=fake_provider_registry,
    )
    first = first_app.state.generated_route_toolsets.providers["sessions"]
    assert await fake_provider_registry.get_toolset("sessions") is first

    second_app = create_test_app(
        storage_provider=fake_storage_provider,
        provider_registry=fake_provider_registry,
    )
    second = second_app.state.generated_route_toolsets.providers["sessions"]

    assert second is not first
    assert await fake_provider_registry.get_toolset("sessions") is second
    assert fake_provider_registry._generated_api_toolset_ids == set(  # noqa: SLF001
        second_app.state.generated_route_toolsets.providers
    )


def test_independent_app_factories_compile_one_route_contract_without_sharing(
    monkeypatch,
    fake_storage_provider,
    fake_provider_registry,
) -> None:
    route_tools_module._clear_compiled_route_tool_cache()  # noqa: SLF001
    original_openapi = FastAPI.openapi
    original_blueprint_compile = route_tools_module._compile_route_tool_blueprints  # noqa: SLF001
    original_route_part = route_tools_module._route_contract_part  # noqa: SLF001
    openapi_calls = 0
    blueprint_compiles = 0
    route_part_compiles = 0

    def counted_openapi(app: FastAPI):
        nonlocal openapi_calls
        openapi_calls += 1
        return original_openapi(app)

    def counted_blueprint_compile(schema, walked_routes):
        nonlocal blueprint_compiles
        blueprint_compiles += 1
        return original_blueprint_compile(schema, walked_routes)

    def counted_route_part(full_path, route, inherited_dependency_contracts):
        nonlocal route_part_compiles
        route_part_compiles += 1
        return original_route_part(
            full_path,
            route,
            inherited_dependency_contracts,
        )

    monkeypatch.setattr(FastAPI, "openapi", counted_openapi)
    monkeypatch.setattr(
        route_tools_module,
        "_compile_route_tool_blueprints",
        counted_blueprint_compile,
    )
    monkeypatch.setattr(
        route_tools_module,
        "_route_contract_part",
        counted_route_part,
    )
    production = create_app(AppConfig())
    cold_route_parts = route_part_compiles
    test_app = create_test_app(
        storage_provider=fake_storage_provider,
        provider_registry=fake_provider_registry,
    )
    test_factory_route_parts = route_part_compiles - cold_route_parts
    second_production = create_app(AppConfig())
    second_factory_route_parts = (
        route_part_compiles - cold_route_parts - test_factory_route_parts
    )

    first_snapshot = production.state.generated_route_toolsets
    second_snapshot = test_app.state.generated_route_toolsets
    third_snapshot = second_production.state.generated_route_toolsets
    factory_count = 3
    operation_id = first_snapshot.operations[0].operation_id
    first_operation = next(
        item for item in first_snapshot.operations if item.operation_id == operation_id
    )
    second_operation = next(
        item for item in second_snapshot.operations if item.operation_id == operation_id
    )
    third_operation = next(
        item for item in third_snapshot.operations if item.operation_id == operation_id
    )
    provider_id = first_operation.tool.toolset_id
    tool_id = first_operation.tool.id

    assert openapi_calls == 1
    assert blueprint_compiles == 1
    assert openapi_calls * 3 <= factory_count
    assert cold_route_parts == 404
    # The 380 module-level declarations hit the identity cache. Only the 24
    # fresh CRUD/access closure routes derive their structural parts again.
    assert test_factory_route_parts == second_factory_route_parts == 24
    assert first_snapshot.operations == second_snapshot.operations
    assert first_snapshot.operations == third_snapshot.operations
    assert first_snapshot.providers[provider_id] is not second_snapshot.providers[provider_id]
    assert first_snapshot.providers[provider_id] is not third_snapshot.providers[provider_id]
    assert list(first_snapshot.providers["admin_users"]._registry) == [  # noqa: SLF001
        "create_admin_user",
        "list_admin_users",
        "get_admin_user",
        "update_admin_user",
        "delete_admin_user",
    ]
    assert first_operation.tool is not second_operation.tool
    assert first_operation.tool.args_schema is not second_operation.tool.args_schema
    assert (
        first_operation.tool.args_schema["properties"]
        is not second_operation.tool.args_schema["properties"]
    )
    assert (
        first_snapshot.providers[provider_id]._registry[tool_id][1]  # noqa: SLF001
        is not second_snapshot.providers[provider_id]._registry[tool_id][1]  # noqa: SLF001
    )

    first_operation.tool.args_schema["cache_mutation_probe"] = True
    fourth_app = create_app(AppConfig())
    fourth_operation = next(
        item
        for item in fourth_app.state.generated_route_toolsets.operations
        if item.operation_id == operation_id
    )
    assert "cache_mutation_probe" not in second_operation.tool.args_schema
    assert "cache_mutation_probe" not in fourth_operation.tool.args_schema
    assert openapi_calls == 1
    assert blueprint_compiles == 1
    assert route_part_compiles == cold_route_parts + 3 * 24
    assert openapi_calls * 4 <= factory_count + 1


def test_stable_route_fast_path_never_equates_distinct_closure_endpoints(
    monkeypatch,
) -> None:
    route_tools_module._clear_compiled_route_tool_cache()  # noqa: SLF001
    original_route_part = route_tools_module._route_contract_part  # noqa: SLF001
    route_part_compiles = 0

    def counted_route_part(full_path, route, inherited_dependency_contracts):
        nonlocal route_part_compiles
        route_part_compiles += 1
        return original_route_part(
            full_path,
            route,
            inherited_dependency_contracts,
        )

    monkeypatch.setattr(
        route_tools_module,
        "_route_contract_part",
        counted_route_part,
    )

    def app_for(marker: str) -> FastAPI:
        app = FastAPI()

        @app.get("/closure")
        @route_tool(scoped_id="closure_identity__read")
        async def closure() -> dict[str, str]:
            return {"marker": marker}

        return app

    app_refs = []
    route_refs = []
    for marker in ("first", "second"):
        app = app_for(marker)
        route = next(
            route
            for route in app.routes
            if isinstance(route, route_tools_module.APIRoute)
        )
        app_refs.append(weakref.ref(app))
        route_refs.append(weakref.ref(route))
        build_generated_route_toolsets(
            app,
            _stable_route_graph=True,
        )
    del app, route
    gc.collect()
    gc.collect()

    assert route_part_compiles == 2
    assert all(ref() is None for ref in app_refs)
    assert all(ref() is None for ref in route_refs)
    assert len(route_tools_module._STABLE_ROUTE_CONTRACT_PARTS) == 0  # noqa: SLF001


def test_custom_bound_openapi_methods_are_isolated_by_app_owner() -> None:
    route_tools_module._clear_compiled_route_tool_cache()  # noqa: SLF001
    router = APIRouter()

    @router.get("/stateful")
    @route_tool(scoped_id="stateful_openapi__read")
    async def stateful(value: str) -> dict[str, str]:
        return {"value": value}

    openapi_calls = 0

    class StatefulOpenapiApp(FastAPI):
        def openapi(self):
            nonlocal openapi_calls
            openapi_calls += 1
            schema = super().openapi()
            value_schema = schema["paths"]["/stateful"]["get"]["parameters"][0][
                "schema"
            ]
            value_schema["enum"] = [self.state.schema_marker]
            return schema

    def app_for(marker: str) -> FastAPI:
        app = StatefulOpenapiApp()
        app.state.schema_marker = marker
        app.include_router(router)
        return app

    first = build_generated_route_toolsets(app_for("FIRST"))
    second = build_generated_route_toolsets(app_for("SECOND"))

    assert first.operations[0].tool.args_schema["properties"]["value"]["enum"] == [
        "FIRST"
    ]
    assert second.operations[0].tool.args_schema["properties"]["value"]["enum"] == [
        "SECOND"
    ]
    assert openapi_calls == 2


def test_nested_security_scopes_invalidate_the_route_schema_cache() -> None:
    route_tools_module._clear_compiled_route_tool_cache()  # noqa: SLF001
    oauth = OAuth2PasswordBearer(
        tokenUrl="/token",
        scopes={"read": "Read access", "write": "Write access"},
    )

    def app_for(scope: str) -> FastAPI:
        app = FastAPI()
        router = APIRouter()

        async def authorised(
            _token: str = Security(oauth, scopes=[scope]),
        ) -> None:
            return None

        @router.get("/secure", dependencies=[Depends(authorised)])
        @route_tool(scoped_id="security_scope__read")
        async def secure() -> dict[str, bool]:
            return {"ok": True}

        app.include_router(router)
        return app

    def compiled_schema(app: FastAPI) -> dict:
        walked = tuple(
            route_tools_module._walk_api_routes(app.routes)  # noqa: SLF001
        )
        return route_tools_module._compiled_route_schema(app, walked)  # noqa: SLF001

    read_schema = compiled_schema(app_for("read"))
    write_schema = compiled_schema(app_for("write"))

    assert read_schema["paths"]["/secure"]["get"]["security"] == [
        {"OAuth2PasswordBearer": ["read"]}
    ]
    assert write_schema["paths"]["/secure"]["get"]["security"] == [
        {"OAuth2PasswordBearer": ["write"]}
    ]
    assert len(route_tools_module._COMPILED_ROUTE_CONTRACTS) == 2  # noqa: SLF001


def test_source_route_path_invalidates_descriptor_blueprints() -> None:
    route_tools_module._clear_compiled_route_tool_cache()  # noqa: SLF001

    @route_tool(scoped_id="path_contract__read")
    async def endpoint() -> dict[str, bool]:
        return {"ok": True}

    def app_for(source_path: str, prefix: str) -> FastAPI:
        router = APIRouter()
        router.add_api_route(
            source_path,
            endpoint,
            methods=["GET"],
            operation_id="path_contract_read",
        )
        app = FastAPI()
        app.include_router(router, prefix=prefix)
        return app

    short_source = build_generated_route_toolsets(app_for("/x", "/p"))
    full_source = build_generated_route_toolsets(app_for("/p/x", ""))

    assert short_source.operations[0].path == full_source.operations[0].path == "/p/x"
    assert "matching /x REST operation" in short_source.operations[0].tool.description
    assert "matching /p/x REST operation" in full_source.operations[0].tool.description
    assert len(route_tools_module._COMPILED_ROUTE_CONTRACTS) == 2  # noqa: SLF001


@pytest.mark.parametrize("dependency_location", ["route", "inherited"])
def test_dependency_names_invalidate_required_role_blueprints(
    dependency_location: str,
) -> None:
    route_tools_module._clear_compiled_route_tool_cache()  # noqa: SLF001

    @route_tool(scoped_id="role_contract__read")
    async def endpoint() -> dict[str, bool]:
        return {"ok": True}

    def dependency_for(name: str):
        async def same_code() -> None:
            return None

        same_code.__name__ = name
        return same_code

    def app_for(dependency_name: str) -> FastAPI:
        dependency = Depends(dependency_for(dependency_name))
        router = APIRouter()
        router.add_api_route(
            "/role",
            endpoint,
            methods=["GET"],
            operation_id="role_contract_read",
            dependencies=[dependency] if dependency_location == "route" else None,
        )
        app = FastAPI()
        app.include_router(
            router,
            dependencies=[dependency] if dependency_location == "inherited" else None,
        )
        return app

    ordinary = build_generated_route_toolsets(app_for("ordinary"))
    admin = build_generated_route_toolsets(app_for("require_admin"))

    assert ordinary.operations[0].tool.required_role == "user"
    assert admin.operations[0].tool.required_role == "admin"
    assert len(route_tools_module._COMPILED_ROUTE_CONTRACTS) == 2  # noqa: SLF001


@pytest.mark.asyncio
async def test_cached_descriptors_still_dispatch_through_the_owning_app() -> None:
    route_tools_module._clear_compiled_route_tool_cache()  # noqa: SLF001
    router = APIRouter()

    @router.get("/marker")
    @route_tool(scoped_id="cache_probe__marker")
    async def marker(request: Request) -> dict[str, str]:
        return {"marker": request.app.state.marker}

    def app_for(value: str) -> tuple[FastAPI, object]:
        app = FastAPI()
        app.state.marker = value
        app.include_router(router)
        return app, build_generated_route_toolsets(app)

    first_app, first_snapshot = app_for("first")
    second_app, second_snapshot = app_for("second")
    assert first_app is not second_app
    assert first_snapshot.providers["cache_probe"] is not second_snapshot.providers["cache_probe"]

    first = await _call(
        first_snapshot.providers["cache_probe"],
        "marker",
        {},
        Person("alice", "user"),
    )
    second = await _call(
        second_snapshot.providers["cache_probe"],
        "marker",
        {},
        Person("alice", "user"),
    )
    assert json.loads(first.output) == {"marker": "first"}
    assert json.loads(second.output) == {"marker": "second"}


@pytest.mark.asyncio
async def test_latest_catalogue_snapshot_does_not_pin_its_owning_app() -> None:
    route_tools_module._clear_compiled_route_tool_cache()  # noqa: SLF001
    app = FastAPI()

    @app.get("/lifetime")
    @route_tool(scoped_id="cache_lifetime__read")
    async def lifetime() -> dict[str, bool]:
        return {"alive": True}

    snapshot = build_generated_route_toolsets(app)
    provider = snapshot.providers["cache_lifetime"]
    app_ref = weakref.ref(app)

    del snapshot, app
    gc.collect()
    gc.collect()

    assert app_ref() is None
    assert set(route_tools_module.generated_route_toolset_providers()) == {
        "cache_lifetime"
    }
    result = await _call(provider, "read", {}, Person("alice", "user"))
    assert result.is_error is True
    assert json.loads(result.output) == {
        "type": "route-tool-app-unavailable",
        "message": "the generated REST tool's application is no longer running",
    }


def test_route_dependency_and_unique_id_changes_invalidate_compilation(
    monkeypatch,
) -> None:
    route_tools_module._clear_compiled_route_tool_cache()  # noqa: SLF001
    original_openapi = FastAPI.openapi
    openapi_calls = 0

    def counted_openapi(app: FastAPI):
        nonlocal openapi_calls
        openapi_calls += 1
        return original_openapi(app)

    monkeypatch.setattr(FastAPI, "openapi", counted_openapi)
    router = APIRouter()

    @router.get("/value")
    @route_tool(scoped_id="cache_contract__value")
    async def value(limit: int = 1) -> dict[str, int]:
        return {"value": limit}

    async def dependency_one() -> None:
        return None

    async def dependency_two() -> None:
        return None

    def unique_id_one(_route) -> str:
        return "cache_contract_v1"

    def unique_id_two(_route) -> str:
        return "cache_contract_v2"

    def compile_app(
        *,
        prefix: str = "/first",
        dependency=None,
        unique_id_function=unique_id_one,
    ):
        app = FastAPI(generate_unique_id_function=unique_id_function)
        dependencies = [Depends(dependency)] if dependency is not None else []
        app.include_router(router, prefix=prefix, dependencies=dependencies)
        return build_generated_route_toolsets(app)

    first = compile_app()
    compile_app()
    compile_app(prefix="/changed")
    compile_app(dependency=dependency_one)
    compile_app(dependency=dependency_two)
    assert openapi_calls == 4

    changed_id = compile_app(unique_id_function=unique_id_two)
    assert first.operations[0].operation_id == "cache_contract_v1"
    assert changed_id.operations[0].operation_id == "cache_contract_v2"
    assert openapi_calls == 5


def test_concurrent_compilation_misses_publish_one_template(monkeypatch) -> None:
    route_tools_module._clear_compiled_route_tool_cache()  # noqa: SLF001
    router = APIRouter()

    @router.get("/value")
    @route_tool(scoped_id="concurrent_cache__value")
    async def value() -> dict[str, int]:
        return {"value": 1}

    apps = [FastAPI(), FastAPI()]
    for app in apps:
        app.include_router(router)
    walked = [
        tuple(route_tools_module._walk_api_routes(app.routes))  # noqa: SLF001
        for app in apps
    ]
    original_compile = route_tools_module._compile_route_schema  # noqa: SLF001
    barrier = threading.Barrier(2)
    candidates = []
    candidates_lock = threading.Lock()

    def racing_compile(app, routes):
        candidate = original_compile(app, routes)
        with candidates_lock:
            candidates.append(candidate)
        barrier.wait(timeout=10)
        return candidate

    monkeypatch.setattr(
        route_tools_module,
        "_compile_route_schema",
        racing_compile,
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(route_tools_module._compiled_route_schema, app, routes)  # noqa: SLF001
            for app, routes in zip(apps, walked, strict=True)
        ]
        published = [future.result(timeout=15) for future in futures]

    assert len(candidates) == 2
    assert published[0] == published[1]
    assert published[0] is not published[1]
    assert len(route_tools_module._COMPILED_ROUTE_CONTRACTS) == 1  # noqa: SLF001
    published_template = next(  # noqa: SLF001
        iter(route_tools_module._COMPILED_ROUTE_CONTRACTS.values())
    )
    assert sum(published_template is candidate for candidate in candidates) == 1


def test_concurrent_blueprint_misses_publish_one_immutable_tuple(monkeypatch) -> None:
    route_tools_module._clear_compiled_route_tool_cache()  # noqa: SLF001
    router = APIRouter()

    @router.get("/value")
    @route_tool(scoped_id="concurrent_blueprint__value")
    async def value() -> dict[str, int]:
        return {"value": 1}

    apps = [FastAPI(), FastAPI()]
    for app in apps:
        app.include_router(router)
    walked = [
        tuple(route_tools_module._walk_api_routes(app.routes))  # noqa: SLF001
        for app in apps
    ]
    key = route_tools_module._route_compilation_key(  # noqa: SLF001
        apps[0],
        walked[0],
    )
    assert key == route_tools_module._route_compilation_key(  # noqa: SLF001
        apps[1],
        walked[1],
    )
    compiled = route_tools_module._compiled_route_schema_entry(  # noqa: SLF001
        apps[0],
        walked[0],
        key,
    )
    assert compiled.blueprints is None

    original_compile = route_tools_module._compile_route_tool_blueprints  # noqa: SLF001
    barrier = threading.Barrier(2)
    candidates = []
    candidates_lock = threading.Lock()

    def racing_compile(schema, routes):
        candidate = original_compile(schema, routes)
        with candidates_lock:
            candidates.append(candidate)
        barrier.wait(timeout=10)
        return candidate

    monkeypatch.setattr(
        route_tools_module,
        "_compile_route_tool_blueprints",
        racing_compile,
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                route_tools_module._compiled_route_tool_blueprints,  # noqa: SLF001
                key,
                compiled,
                routes,
            )
            for routes in walked
        ]
        published = [future.result(timeout=15) for future in futures]

    assert len(candidates) == 2
    assert published[0] is published[1]
    published_entry = route_tools_module._COMPILED_ROUTE_CONTRACTS[key]  # noqa: SLF001
    assert published_entry.blueprints is published[0]
    assert sum(published[0] is candidate for candidate in candidates) == 1


def test_generated_provider_reinstall_keeps_fixed_and_foreign_collisions_hard() -> None:
    registry = ProviderRegistry(_FakeStorageProvider())
    generated = object()

    with pytest.raises(ConfigError, match="fixed built-ins.*system"):
        registry.install_generated_api_toolsets({"system": generated})  # type: ignore[dict-item]

    foreign = object()
    registry._set_fixed_toolset("foreign", foreign)  # noqa: SLF001 - unproven canonical occupant
    with pytest.raises(ConfigError, match="unrelated installed providers.*foreign"):
        registry.install_generated_api_toolsets({"foreign": generated})  # type: ignore[dict-item]

    assert registry._builtin_toolset_providers["foreign"] is foreign  # noqa: SLF001
    assert registry._generated_api_toolset_ids == set()  # noqa: SLF001


def test_generated_provider_reinstall_replaces_and_retires_only_proven_entries() -> None:
    registry = ProviderRegistry(_FakeStorageProvider())
    first = object()
    second = object()
    replacement = object()

    registry.install_generated_api_toolsets(  # type: ignore[arg-type]
        {"first": first, "second": second}
    )
    registry.install_generated_api_toolsets(  # type: ignore[arg-type]
        {"second": replacement}
    )

    assert "first" not in registry._builtin_toolset_providers  # noqa: SLF001
    assert registry._builtin_toolset_providers["second"] is replacement  # noqa: SLF001
    assert registry._generated_api_toolset_ids == {"second"}  # noqa: SLF001


@pytest.mark.asyncio
async def test_generated_mutators_share_live_default_approval_source(monkeypatch) -> None:
    monkeypatch.setattr(crud_toolset, "_GENERATED_DEFAULT_GATED_TOOLS", frozenset())
    app, snapshot = _dispatch_app()
    assert build_generated_route_toolsets(app, publish_default_gates=True) is snapshot
    assert ("dispatch_probe", "probe") in crud_toolset.DEFAULT_GATED_TOOLS
    assert ("dispatch_probe", "admin_probe") not in crud_toolset.DEFAULT_GATED_TOOLS

    storage_provider = _FakeStorageProvider()
    await ensure_crud_approval_policies(storage_provider)
    resolver = ApprovalResolver(storage_provider.get_storage(ToolApprovalPolicy))
    policy = await resolver.find(toolset_id="dispatch_probe", tool_name="probe")
    assert policy is not None and policy.approval.type.value == "required"

    policy.enabled = False
    await storage_provider.get_storage(ToolApprovalPolicy).update(policy)
    resolver.invalidate()
    assert await resolver.find(toolset_id="dispatch_probe", tool_name="probe") is None


@pytest.mark.asyncio
async def test_external_header_cannot_activate_trusted_identity() -> None:
    app, _snapshot = _dispatch_app()
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://external") as client:
        response = await client.post(
            "/v1/probes/one",
            headers={
                "primer-route-tool-dispatch": "alice",
                "x-primer-internal": "true",
                "x-primer-principal": "alice",
                "authorization": "Bearer route-tool",
            },
            json={"value": 1},
        )
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["type"] == "/errors/authentication-failed"


@pytest.mark.asyncio
async def test_trusted_dispatch_revalidates_user_role_disabled_and_system() -> None:
    app, snapshot = _dispatch_app()
    provider = snapshot.providers["dispatch_probe"]
    users = app.state.storage_provider.get_storage(User)

    missing = await _call(
        provider, "probe", {"item_id": "one", "body": {"value": 1}}, Person("missing", "user")
    )
    assert missing.is_error and json.loads(missing.output)["status"] == 401

    alice = await users.get("alice")
    assert alice is not None
    alice.disabled = True
    disabled = await _call(
        provider, "probe", {"item_id": "one", "body": {"value": 1}}, Person("alice", "user")
    )
    assert disabled.is_error and json.loads(disabled.output)["status"] == 401

    alice.disabled = False
    alice.role = "admin"
    changed_role = await _call(
        provider, "probe", {"item_id": "one", "body": {"value": 1}}, Person("alice", "user")
    )
    assert changed_role.is_error and json.loads(changed_role.output)["status"] == 401

    unattributed = await _call(
        provider, "probe", {"item_id": "one", "body": {"value": 1}}, Person(None, "user")
    )
    assert unattributed.is_error and json.loads(unattributed.output)["status"] == 401

    system = await _call(
        provider, "probe", {"item_id": "one", "body": {"value": 1}}, Person(None, "system")
    )
    assert not system.is_error and json.loads(system.output)["actor"] is None


@pytest.mark.asyncio
async def test_recursion_and_timeout_are_bounded(monkeypatch) -> None:
    _app, snapshot = _dispatch_app(include_slow=True)
    provider = snapshot.providers["dispatch_probe"]

    from primer.api import route_tools

    depth = route_tools._DISPATCH_DEPTH.set(1)  # noqa: SLF001 - focused safety seam
    try:
        recursive = await _call(
            provider,
            "probe",
            {"item_id": "one", "body": {"value": 1}},
            Person("alice", "user"),
        )
    finally:
        route_tools._DISPATCH_DEPTH.reset(depth)  # noqa: SLF001
    assert recursive.is_error and json.loads(recursive.output)["type"] == "route-tool-recursion"

    with monkeypatch.context() as timeout_patch:
        timeout_patch.setattr(route_tools, "_DISPATCH_TIMEOUT_S", 0.001)
        timed_out = await _call(
            snapshot.providers["slow_probe"],
            "slow",
            {},
            Person("alice", "user"),
        )
        assert (
            timed_out.is_error
            and json.loads(timed_out.output)["type"] == "route-tool-timeout"
        )

    # The 1 ms deadline proves only the timeout path. Restore the production
    # deadline before independently proving that an oversized response trips
    # the byte bound; otherwise scheduler load can legitimately make the
    # timeout win this race first.
    assert route_tools._DISPATCH_TIMEOUT_S == 30.0  # noqa: SLF001

    too_large = await _call(snapshot.providers["slow_probe"], "large", {}, Person("alice", "user"))
    assert too_large.is_error
    assert json.loads(too_large.output) == {
        "type": "route-tool-response-too-large",
        "message": "generated REST response exceeds the tool limit",
        "limit_bytes": 1_048_576,
    }


@pytest.mark.asyncio
async def test_streaming_response_limit_stops_producer_at_crossing_chunk() -> None:
    app, snapshot = _dispatch_app(include_slow=True)
    provider = snapshot.providers["slow_probe"]

    exact = await _call(provider, "stream_exact", {}, Person("alice", "user"))
    assert not exact.is_error
    assert len(base64.b64decode(json.loads(exact.output)["body_base64"])) == 1_048_576
    assert app.state.exact_stream_chunks == 4

    oversize = await _call(
        provider, "stream_oversize", {}, Person("alice", "user")
    )
    assert json.loads(oversize.output) == {
        "type": "route-tool-response-too-large",
        "message": "generated REST response exceeds the tool limit",
        "limit_bytes": 1_048_576,
    }
    assert app.state.oversize_stream_chunks == 4
    assert app.state.oversize_stream_cleaned is True


@pytest.mark.asyncio
async def test_production_gzip_is_bypassed_and_caller_cannot_override_identity() -> None:
    app = FastAPI()
    app.add_middleware(GZipMiddleware, minimum_size=1)
    app.state.produced_chunks = 0
    app.state.seen_accept_encoding = None

    async def compressible_chunks():
        for _ in range(8):
            app.state.produced_chunks += 1
            yield b"z" * 300_000

    @app.get("/v1/compressible")
    @route_tool(scoped_id="encoding_probe__compressible")
    async def compressible(
        request: Request,
        accept_encoding: str | None = Header(default=None),
    ) -> StreamingResponse:
        del accept_encoding
        app.state.seen_accept_encoding = request.headers.get("accept-encoding")
        return StreamingResponse(
            compressible_chunks(), media_type="application/octet-stream"
        )

    register_error_handlers(app)
    snapshot = build_generated_route_toolsets(app)
    operation = snapshot.operations[0]
    encoding_argument = next(
        binding.argument
        for binding in operation.bindings
        if binding.location == "header"
        and binding.wire_name.lower() == "accept-encoding"
    )
    result = await _call(
        snapshot.providers["encoding_probe"],
        "compressible",
        {encoding_argument: "gzip"},
        Person("alice", "user"),
    )

    assert json.loads(result.output) == {
        "type": "route-tool-response-too-large",
        "message": "generated REST response exceeds the tool limit",
        "limit_bytes": 1_048_576,
    }
    assert app.state.seen_accept_encoding == "identity"
    assert app.state.produced_chunks == 4


@pytest.mark.asyncio
async def test_explicit_gzip_response_is_rejected_before_body_materialization(
    caplog,
) -> None:
    app = FastAPI()
    app.state.produced_chunks = 0
    app.state.request_cleaned = False

    async def response_lifetime():
        try:
            yield
        finally:
            app.state.request_cleaned = True

    async def encoded_chunks():
        app.state.produced_chunks += 1
        yield b"this body must never be requested"

    @app.get("/v1/encoded")
    @route_tool(scoped_id="encoding_probe__encoded")
    async def encoded(_lifetime=Depends(response_lifetime)) -> StreamingResponse:
        del _lifetime
        return StreamingResponse(
            encoded_chunks(),
            media_type="application/octet-stream",
            headers={"Content-Encoding": "gzip"},
        )

    register_error_handlers(app)
    snapshot = build_generated_route_toolsets(app)
    with caplog.at_level(logging.INFO, logger="primer.api.route_tool.audit"):
        result = await _call(
            snapshot.providers["encoding_probe"],
            "encoded",
            {},
            Person("alice", "user"),
        )

    assert json.loads(result.output) == {
        "type": "route-tool-response-encoding-rejected",
        "message": "generated REST response uses an unsupported content encoding",
        "content_encoding": "gzip",
    }
    assert app.state.produced_chunks == 0
    assert app.state.request_cleaned is True
    assert any(
        getattr(record, "outcome", None) == "response_encoding_rejected"
        for record in caplog.records
        if record.name == "primer.api.route_tool.audit"
    )


@pytest.mark.asyncio
async def test_dispatch_cancellation_propagates() -> None:
    _app, snapshot = _dispatch_app(include_slow=True)
    task = asyncio.create_task(
        _call(snapshot.providers["slow_probe"], "slow", {}, Person("alice", "user"))
    )
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

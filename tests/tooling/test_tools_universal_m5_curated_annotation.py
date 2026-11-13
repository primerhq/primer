"""M5 curated-route annotation contract.

Every one of the 135 reviewed curated operations (76 CRUD verbs via the
explicit ``curated_tool_ids`` verb maps + 59 standalone routes) must carry
route metadata whose ``curated_tool_ids`` match the reviewed manifest, and a
route that declares an unknown curated id must fail the build loudly instead
of drifting into a silent row.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import MappingProxyType

import pytest
from fastapi import FastAPI, Request
from pydantic import BaseModel

from primer.api.app import create_app
from primer.api.config import AppConfig
from primer.api.route_tools import (
    CrudToolDeclaration,
    GeneratedRouteToolSnapshot,
    build_generated_route_toolsets,
    coverage_projection,
    route_tool,
    route_tool_metadata,
    validate_curated_route_declarations,
    _HTTP_METHODS,
    _metadata_for,
    _openapi_path,
    _walk_api_routes,
)
from primer.api.middleware.auth import AuthMiddleware
from primer.model.chat import Tool
from primer.model.except_ import ConfigError

MANIFEST_PATH = Path("scripts/data/tools_universal_m0_manifest.json")


@pytest.fixture(scope="module")
def manifest_rows() -> list[dict]:
    return json.loads(MANIFEST_PATH.read_text())["rows"]


@pytest.fixture(scope="module")
def built_app():
    """One full production app build for the whole module (serial)."""
    return create_app(AppConfig())


def _metadata_by_method_path(app) -> dict[tuple[str, str], object]:
    """The projection's route walk: (method, openapi path) -> metadata."""
    metadata_by: dict[tuple[str, str], object] = {}
    for full_path, route, _deps in _walk_api_routes(app.routes):
        for method in sorted((route.methods or set()) & _HTTP_METHODS):
            metadata_by.setdefault(
                (method, _openapi_path(full_path)), _metadata_for(route, method)
            )
    return metadata_by


def test_every_manifest_curated_operation_is_annotated(built_app, manifest_rows) -> None:
    """All 135 curated rows resolve to curated metadata with the reviewed ids."""
    metadata_by = _metadata_by_method_path(built_app)
    curated = [row for row in manifest_rows if row["state"] == "curated"]
    assert len(curated) == 135
    for row in curated:
        metadata = metadata_by.get((row["method"], row["path"]))
        assert metadata is not None, (
            f"curated operation {row['operation_id']} has no route metadata"
        )
        assert metadata.mapping_state == "curated", (
            f"{row['operation_id']}: mapping_state {metadata.mapping_state!r} != 'curated'"
        )
        assert metadata.curated_tool_ids is not None
        assert tuple(sorted(metadata.curated_tool_ids)) == tuple(sorted(row["tool_ids"])), (
            f"{row['operation_id']}: {metadata.curated_tool_ids} != {row['tool_ids']}"
        )


def test_the_nine_one_to_many_rows_are_pinned_exactly(built_app, manifest_rows) -> None:
    """The nine one-to-many operations declare exactly two ids, in declared order."""
    two_id = {row["operation_id"]: row["tool_ids"] for row in manifest_rows if len(row["tool_ids"]) == 2 and row["state"] == "curated"}
    assert len(two_id) == 9
    metadata_by = _metadata_by_method_path(built_app)
    pinned = {
        "_create_v1_agents_post": ("crud__create_agent", "system__create_agent"),
        "_create_v1_graphs_post": ("crud__create_graph", "system__create_graph"),
        "_update_v1_agents__entity_id__put": ("crud__update_agent", "system__update_agent"),
        "_update_v1_graphs__entity_id__put": ("crud__update_graph", "system__update_graph"),
        "create_trigger_endpoint_v1_triggers_post": ("crud__create_trigger", "trigger__create"),
        "update_trigger_endpoint_v1_triggers__trigger_id__put": ("crud__update_trigger", "trigger__update"),
        "get_or_list_collection_documents_v1_collections__collection_id__documents_get": (
            "system__get_document_content", "system__list_documents",
        ),
        "get_or_list_docs_v1_collections__collection_id__docs_get": (
            "collections__collection_tree", "collections__read_document",
        ),
        "search_collection_v1_collections__collection_id__search_post": (
            "collections__search", "system__search_collection",
        ),
    }
    assert set(two_id) == set(pinned)
    for operation_id, declared in pinned.items():
        row = next(r for r in manifest_rows if r["operation_id"] == operation_id)
        assert tuple(row["tool_ids"]) == tuple(declared)
        metadata = metadata_by[(row["method"], row["path"])]
        assert tuple(metadata.curated_tool_ids) == tuple(declared)


class _Body(BaseModel):
    value: str


class _StubProvider:
    def __init__(self, tools: list[Tool]) -> None:
        self._tools = tools

    async def list_tools(self, *, principal: str | None = None):
        del principal
        for tool in self._tools:
            yield tool


class _StubRegistry:
    def __init__(self, tools: list[Tool]) -> None:
        self._provider = _StubProvider(tools)

    def builtin_toolset_providers(self) -> dict:
        return {"m5_probe": self._provider}


def _probe_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    app.state.config = AppConfig(auth={"enabled": True})
    return app


def test_a_curated_id_naming_a_generated_scoped_id_fails_the_build() -> None:
    """Disjointness guard: the reviewed contract keeps the two sets apart."""
    app = _probe_app()

    @app.post("/v1/m5-probes/generated")
    @route_tool(scoped_id="m5_probe__generated")
    async def gen_probe(request: Request, body: _Body) -> dict[str, object]:
        return {"value": body.value}

    @app.post("/v1/m5-probes/curated")
    @route_tool(mapping_state="curated", curated_tool_ids=("m5_probe__generated",))
    async def curated_probe(request: Request, body: _Body) -> dict[str, object]:
        return {"value": body.value}

    with pytest.raises(ConfigError, match="curated route declarations name generated scoped ids"):
        build_generated_route_toolsets(app)


async def test_a_route_declaring_an_unknown_curated_id_fails_loudly() -> None:
    """Liveness guard: an id that names no live tool is a boot failure, not a gap."""
    app = _probe_app()

    @app.post("/v1/m5-probes/unknown")
    @route_tool(mapping_state="curated", curated_tool_ids=("m5_probe__does_not_exist",))
    async def unknown_probe(request: Request, body: _Body) -> dict[str, object]:
        return {"value": body.value}

    live = [Tool(id="present", toolset_id="m5_probe", description="Live probe tool.", args_schema={"type": "object", "properties": {}})]
    app.state.provider_registry = _StubRegistry(live)
    with pytest.raises(ConfigError, match="unknown curated tools"):
        await validate_curated_route_declarations(app)

    # The positive control: the same guard passes once the declared id is live.
    app2 = _probe_app()

    @app2.post("/v1/m5-probes/live")
    @route_tool(mapping_state="curated", curated_tool_ids=("m5_probe__present",))
    async def live_probe(request: Request, body: _Body) -> dict[str, object]:
        return {"value": body.value}

    app2.state.provider_registry = _StubRegistry(live)
    await validate_curated_route_declarations(app2)


def test_an_undeclared_operation_surfaces_as_a_gap_not_a_generated_row() -> None:
    """A route with no declaration is a gap row; it is never generated silently."""
    app = _probe_app()

    @app.get("/v1/m5-probes/unannotated")
    async def unannotated(request: Request) -> dict[str, object]:
        return {}

    app.state.generated_route_toolsets = GeneratedRouteToolSnapshot(
        operations=(), providers=MappingProxyType({})
    )
    row, = coverage_projection(app)
    assert (row.method, row.path) == ("GET", "/v1/m5-probes/unannotated")
    assert row.state == "gap"
    assert row.tool_ids == ()


def test_curated_metadata_rules_reject_malformed_declarations() -> None:
    """The field contract itself: curated needs ids; other states forbid them."""
    from primer.api.route_tools import RouteToolMetadata

    with pytest.raises(ValueError, match="requires curated_tool_ids"):
        RouteToolMetadata(mapping_state="curated")
    with pytest.raises(ValueError, match="requires curated_tool_ids"):
        RouteToolMetadata(mapping_state="curated", curated_tool_ids=())
    with pytest.raises(ValueError, match="valid only for curated routes"):
        RouteToolMetadata(mapping_state="generated", scoped_id="gen__tool", curated_tool_ids=("a__b",))
    with pytest.raises(ValueError, match="must be '<toolset>__<tool>'"):
        RouteToolMetadata(mapping_state="curated", curated_tool_ids=("no_separator",))
    with pytest.raises(ValueError, match="must be '<toolset>__<tool>'"):
        RouteToolMetadata(mapping_state="curated", curated_tool_ids=("a__b__c",))
    ok = RouteToolMetadata(mapping_state="curated", curated_tool_ids=("a__b", "c__d"))
    assert tuple(ok.curated_tool_ids) == ("a__b", "c__d")
    assert route_tool_metadata(lambda: None) == ()


def test_curated_declarations_freeze_the_input_map_and_plural_order() -> None:
    generated = {"get", "update", "delete", "list", "find"}
    ids = ["z__create", "a__create"]
    source = {"create": ids}
    declaration = CrudToolDeclaration(
        resource_id="m5_probe", generated_operations=generated,
        curated_tool_ids=source,
    )
    source.clear()
    ids.reverse()
    generated.clear()
    assert declaration.curated_operations == {"create"}
    metadata = declaration.metadata_for("create")
    assert metadata.curated_tool_ids == ("z__create", "a__create")
    assert declaration.metadata_for("get").mapping_state == "generated"
    with pytest.raises(TypeError):
        declaration.curated_tool_ids["create"] = ("changed__tool",)

    app = _probe_app()

    @app.get("/v1/m5-probes/plural")
    @route_tool(mapping_state="curated", curated_tool_ids=metadata.curated_tool_ids)
    async def plural() -> dict:
        return {}

    app.state.generated_route_toolsets = GeneratedRouteToolSnapshot(
        operations=(), providers=MappingProxyType({}),
    )
    row, = coverage_projection(app)
    assert row.tool_ids == ("a__create", "z__create")
    assert route_tool_metadata(plural)[0].curated_tool_ids == ("z__create", "a__create")


@pytest.mark.parametrize(
    "generated,curated,reason",
    [
        ({"find"}, None, "cover the CRUD verbs exactly once"),
        ({"find"}, {"find": ("a__b",)}, "overlaps generated"),
        ({"get", "update", "delete", "list", "find"}, {"create": ()}, "requires curated_tool_ids"),
        ({"get", "update", "delete", "list", "find"}, {"create": ("a__b", "a__b")}, "duplicate ids"),
        ({"get", "update", "delete", "list", "find"}, {"create": ("invalid",)}, "must be '<toolset>__<tool>'"),
    ],
)
def test_crud_declarations_reject_gaps_overlap_and_invalid_ids(generated, curated, reason) -> None:
    with pytest.raises(ValueError, match=reason):
        CrudToolDeclaration(
            resource_id="m5_probe", generated_operations=generated,
            curated_tool_ids=curated,
        )


async def test_curated_liveness_requires_an_initialized_registry() -> None:
    with pytest.raises(ConfigError, match="provider_registry is not set"):
        await validate_curated_route_declarations(_probe_app())

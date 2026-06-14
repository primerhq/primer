"""E2E: OpenAPI surface and Swagger/ReDoc gating.

Covers backlog items T0049 (OpenAPI JSON contains documented routes)
and T0050 (Swagger /docs hidden when log_level != debug).
"""

from __future__ import annotations

import httpx
import pytest


@pytest.mark.asyncio
async def test_t0049_openapi_json_lists_documented_routes(
    client: httpx.AsyncClient,
) -> None:
    """T0049 — `GET /openapi.json` returns 200, parses as JSON, and
    contains the major documented route prefixes."""
    resp = await client.get("/v1/openapi.json")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    paths = body.get("paths") or {}
    assert isinstance(paths, dict) and paths, body
    # Spot-check the major routers documented in 01-app-spec.md.
    expected_prefixes = (
        "/v1/health",
        "/v1/agents",
        "/v1/llm_providers",
        "/v1/toolsets",
        "/v1/workspaces",
        "/v1/workspace_providers",
        "/v1/workspace_templates",
        "/v1/collections",
        "/v1/documents",
        "/v1/internal_collections/config",
        "/v1/workers",
    )
    declared = list(paths.keys())
    for needle in expected_prefixes:
        assert any(p.startswith(needle) for p in declared), (
            f"expected at least one path starting with {needle!r} in "
            f"openapi paths; saw {declared!r}"
        )


@pytest.mark.asyncio
async def test_t0050_swagger_docs_hidden_when_not_debug(
    client: httpx.AsyncClient,
) -> None:
    """T0050 — `/docs` returns 404 under the standard bringup config,
    which sets log_level=info. Spec §1: Swagger/ReDoc are mounted only
    when `log_level=debug`.

    The bringup script renders ``log_level: info``, so the route must
    not exist. ReDoc is gated on the same condition.
    """
    docs = await client.get("/docs")
    assert docs.status_code == 404, (
        f"/docs should be 404 under non-debug config, got "
        f"{docs.status_code}: {docs.text[:200]}"
    )
    redoc = await client.get("/redoc")
    assert redoc.status_code == 404, (
        f"/redoc should be 404 under non-debug config, got "
        f"{redoc.status_code}: {redoc.text[:200]}"
    )


# ============================================================================
# T0231 — OpenAPI paths cover every documented /v1 router
# ============================================================================


@pytest.mark.asyncio
async def test_t0231_openapi_paths_cover_all_documented_routers(
    client: httpx.AsyncClient,
) -> None:
    """T0231 — Extends T0049 from spot-check to exhaustive: every router
    documented in 01-app-spec.md must show up under /openapi.json paths.

    Catches a regression where someone unwires a router or forgets to
    mount a new one — without exhaustive coverage, the existing T0049
    spot-check could pass with a partial surface.
    """
    resp = await client.get("/v1/openapi.json")
    assert resp.status_code == 200, resp.text
    paths = (resp.json().get("paths") or {})
    declared = list(paths.keys())

    # Full list of router prefixes per spec
    full_set = [
        "/v1/health",
        "/v1/workers",
        # Generator CRUD entities (spec §5)
        "/v1/llm_providers",
        "/v1/embedding_providers",
        "/v1/cross_encoder_providers",
        "/v1/toolsets",
        "/v1/agents",
        "/v1/graphs",
        "/v1/collections",
        "/v1/documents",
        "/v1/workspace_templates",
        # Bespoke entities (spec §12)
        "/v1/workspaces",
        "/v1/workspace_providers",
        # Sessions (spec §12 / §13)
        "/v1/sessions",
        # Internal collections subsystem (spec §11)
        "/v1/internal_collections/config",
        "/v1/internal_collections/bootstrap",
    ]
    for prefix in full_set:
        assert any(p.startswith(prefix) for p in declared), (
            f"router prefix {prefix!r} missing from /openapi.json paths; "
            f"declared paths: {sorted(declared)!r}"
        )


# ============================================================================
# T0232 — OpenAPI Problem schema is defined and referenced from error responses
# ============================================================================


@pytest.mark.asyncio
async def test_t0232_openapi_problem_schema_referenced_from_errors(
    client: httpx.AsyncClient,
) -> None:
    """T0232 — Spec §3 defines a single RFC 7807 envelope for all
    structured errors. The OpenAPI doc must reflect this: there must
    be a component schema describing the Problem shape (with at
    minimum type/title/status/detail properties) AND it must be
    referenced from at least one 404 and one 422 response across the
    CRUD entities.

    This is a schema-level pin: if a renamed/missing Problem schema
    breaks the documented contract, the test catches it before clients
    rely on it.
    """
    resp = await client.get("/v1/openapi.json")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    schemas = (body.get("components") or {}).get("schemas") or {}
    paths = body.get("paths") or {}

    # Find the Problem schema (FastAPI's default may name it
    # 'ProblemDetail', 'Problem', or 'HTTPValidationError'-but-the-
    # custom-handler overrides that). Pin: there exists at least one
    # schema with the 4 documented RFC 7807 field names.
    problem_schemas: list[str] = []
    rfc7807_required = {"type", "title", "status", "detail"}
    for name, schema in schemas.items():
        props = (schema.get("properties") or {})
        if rfc7807_required.issubset(props.keys()):
            problem_schemas.append(name)

    assert problem_schemas, (
        f"no component schema with all of {sorted(rfc7807_required)!r} "
        f"properties found in /openapi.json. Available schemas: "
        f"{sorted(schemas.keys())!r}"
    )

    # Check that at least one 404 and one 422 response references one
    # of those schemas via $ref. Walk paths→methods→responses.
    has_404_ref = False
    has_422_ref = False
    for _path, methods in paths.items():
        if not isinstance(methods, dict):
            continue
        for _method, op in methods.items():
            if not isinstance(op, dict):
                continue
            responses = op.get("responses") or {}
            for code, response_def in responses.items():
                if not isinstance(response_def, dict):
                    continue
                ref_text = str(response_def)
                if any(s in ref_text for s in problem_schemas):
                    if str(code) == "404":
                        has_404_ref = True
                    if str(code) == "422":
                        has_422_ref = True

    assert has_404_ref, (
        f"no 404 response in OpenAPI references any of the Problem "
        f"schemas {problem_schemas!r}"
    )
    assert has_422_ref, (
        f"no 422 response in OpenAPI references any of the Problem "
        f"schemas {problem_schemas!r}"
    )


# ============================================================================
# T0255 — OpenAPI components include pagination envelope schemas
# ============================================================================


@pytest.mark.asyncio
async def test_t0255_openapi_includes_pagination_envelope_schemas(
    client: httpx.AsyncClient,
) -> None:
    """T0255 — Spec §4 declares two pagination envelope shapes:
      - OffsetPageResponse with {items, offset, length, total}
      - CursorPageResponse with {items, next_cursor, length}

    Both must show up as component schemas in /openapi.json so client
    SDK generators can describe the paginated list contracts. Pin
    "at least one schema matches each shape" rather than a specific
    schema name — FastAPI/Pydantic may emit different class names.
    """
    resp = await client.get("/v1/openapi.json")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    schemas = (body.get("components") or {}).get("schemas") or {}

    # Pydantic generics (`OffsetPageResponse[T]`) emit a base generic
    # schema PLUS one concretization per usage (e.g. `OffsetPage`,
    # `OffsetPageResponse_Any_`). The pin: at least one schema whose
    # NAME signals each envelope, AND whose properties cover the
    # required envelope fields.
    #
    # NB: spec §4 originally listed CursorPageResponse as carrying
    # `length`, but the actual schema is {kind, next_cursor, items}
    # — `length` is only on the REQUEST (CursorPage), not the
    # response. Spec was corrected in the iteration that landed
    # this test.
    offset_response_fields = {"offset", "length", "total"}  # excl items
    cursor_response_fields = {"next_cursor"}                # excl items

    offset_matches: list[str] = []
    cursor_matches: list[str] = []
    for name, schema in schemas.items():
        props = set((schema.get("properties") or {}).keys())
        name_l = name.lower()
        if "offsetpageresponse" in name_l:
            if offset_response_fields.issubset(props):
                offset_matches.append(name)
        if "cursorpageresponse" in name_l:
            if cursor_response_fields.issubset(props):
                cursor_matches.append(name)

    assert offset_matches, (
        f"no OpenAPI OffsetPageResponse-style schema with properties "
        f"{sorted(offset_response_fields)!r} found. Available: "
        f"{sorted(schemas.keys())!r}"
    )
    assert cursor_matches, (
        f"no OpenAPI CursorPageResponse-style schema with properties "
        f"{sorted(cursor_response_fields)!r} found. Available: "
        f"{sorted(schemas.keys())!r}"
    )


# ============================================================================
# T0339 — Every CRUD-generator entity has its 6 ops in /openapi.json
# ============================================================================


@pytest.mark.asyncio
async def test_t0339_openapi_every_crud_entity_has_six_ops(
    client: httpx.AsyncClient,
) -> None:
    """T0339 — Spec §5 says every CRUD-generator entity has 6 routes:
    POST /entity, GET /entity/{id}, PUT /entity/{id}, DELETE
    /entity/{id}, GET /entity, POST /entity/find. Pin all 6 are
    present in /openapi.json for every entity that uses the generator.

    NB: WorkspaceProvider has no PUT (per spec §12), so it's
    excluded; Workspace is bespoke (no generator) so also excluded.
    """
    resp = await client.get("/v1/openapi.json")
    assert resp.status_code == 200, resp.text
    paths = (resp.json().get("paths") or {})

    entities = (
        "/v1/llm_providers",
        "/v1/embedding_providers",
        "/v1/cross_encoder_providers",
        "/v1/toolsets",
        "/v1/agents",
        "/v1/graphs",
        "/v1/collections",
        "/v1/documents",
        "/v1/workspace_templates",
    )
    for prefix in entities:
        # Collection-level paths (/{prefix} and /{prefix}/find)
        coll_path = prefix
        find_path = f"{prefix}/find"
        # Instance path (/{prefix}/{id})
        # Look for any path matching /<prefix>/{<param>}
        # FastAPI emits as e.g. "/v1/llm_providers/{provider_id}"
        instance_paths = [
            p for p in paths
            if p.startswith(f"{prefix}/")
            and not p.endswith("/find")
            and "{" in p
            # exclude bespoke sub-resources like /tools, /models
            and p.count("/") == prefix.count("/") + 1
        ]

        # Verify collection path exists with POST + GET
        assert coll_path in paths, (
            f"OpenAPI missing collection path {coll_path!r}"
        )
        coll_methods = paths[coll_path]
        assert "post" in coll_methods, (
            f"{coll_path} missing POST: {list(coll_methods.keys())!r}"
        )
        assert "get" in coll_methods, (
            f"{coll_path} missing GET: {list(coll_methods.keys())!r}"
        )

        # Verify /find path exists with POST
        assert find_path in paths, (
            f"OpenAPI missing /find path {find_path!r}"
        )
        assert "post" in paths[find_path], (
            f"{find_path} missing POST"
        )

        # Verify instance path exists with GET, PUT, DELETE.
        # Some entities register multiple overlapping parameterised paths
        # (e.g. toolsets registers both /{entity_id} and /{toolset_id}
        # for different sub-sets of verbs). Use the path that exposes the
        # most verbs so the union covers all 3 required ops.
        assert instance_paths, (
            f"OpenAPI missing instance path for {prefix!r}; "
            f"available paths: {[p for p in paths if p.startswith(prefix)]!r}"
        )
        # Pick the instance path with the largest verb set.
        instance_path = max(
            instance_paths, key=lambda p: len(paths[p]),
        )
        instance_methods = paths[instance_path]
        for verb in ("get", "put", "delete"):
            assert verb in instance_methods, (
                f"{instance_path} missing {verb.upper()}: "
                f"{list(instance_methods.keys())!r}"
            )


# ============================================================================
# T0340 — No schema in components.schemas is unreferenced
# ============================================================================


@pytest.mark.asyncio
async def test_t0340_openapi_no_unreferenced_schemas(
    client: httpx.AsyncClient,
) -> None:
    """T0340 — Walk components.schemas; every schema name must appear
    as a $ref somewhere in the OpenAPI document (in paths or in
    other schemas). An orphaned schema indicates a route was removed
    but its body type wasn't.

    Tolerates: discriminated-union variants (e.g. _AgentNodeRef) that
    are referenced indirectly through their parent type's anyOf list.
    """
    import json
    resp = await client.get("/v1/openapi.json")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    schemas = (body.get("components") or {}).get("schemas") or {}

    # Serialize the entire doc to a string and grep for each schema
    # name's $ref. Cheap but reliable.
    doc_str = json.dumps(body)

    orphans: list[str] = []
    for name in schemas.keys():
        ref_token = f'#/components/schemas/{name}"'
        if ref_token not in doc_str:
            orphans.append(name)

    assert not orphans, (
        f"unreferenced schema(s) in /openapi.json (no $ref found): "
        f"{sorted(orphans)!r}"
    )

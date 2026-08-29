"""CRUD router for SemanticSearchProvider (/v1/ssp).

Follows the same pattern as :mod:`primer.api.routers.providers` —
wraps :func:`make_crud_router` with per-entity hooks for invalidation
and cascade-block-on-delete.

Deleting an SSP is allowed even when collections point at it: S2 moved
the vector-store reference inside ``Collection.search``, and a broken
reference surfaces through that block's error state rather than by
blocking the delete.

Platform wave P3 adds two more routes:

* ``GET /ssp/_types`` -- form metadata for the register dropdown,
  mirroring :func:`primer.api.routers.providers.list_llm_provider_types`.
* ``POST /ssp/_test`` (draft) / ``GET /ssp/{id}/_test`` (saved) --
  reachability probes, mirroring the LLM family's
  ``_discover_models`` / ``{id}/discovered_models`` two-endpoint shape.
  Deliberately NOT a call to ``VectorStoreProvider.initialize()``: that
  method runs ``CREATE SCHEMA IF NOT EXISTS`` / ``CREATE EXTENSION``
  for the pgvector-family backends, so reusing it for a "test connection"
  button would silently mutate a database the operator is only
  evaluating. The probes here open a connection (or, for lance, check
  filesystem access) and nothing else.
"""

from __future__ import annotations

import os
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Request
from pydantic import BaseModel, ValidationError

from primer.api.deps import get_semantic_search_registry, get_semantic_search_storage
from primer.api.errors import common_responses
from primer.api.registries.provider_registry import RESERVED_SSP_IDS
from primer.api.routers._crud import make_crud_router
from primer.api.routers.providers import _form_field
from primer.model.except_ import NotFoundError
from primer.model.provider import (
    LanceConfig,
    PgVectorConfig,
    PgVectorScaleConfig,
    SemanticSearchProvider,
    SemanticSearchProviderType,
)


# ---------------------------------------------------------------------------
# Reserved-id protection hooks
# ---------------------------------------------------------------------------


async def _reject_reserved_ssp_create(entity, request: Request) -> None:
    """Reject POST /v1/ssp with a reserved SSP id (409)."""
    if entity.id in RESERVED_SSP_IDS:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "reserved_id",
                "kind": "ssp",
                "reserved": sorted(RESERVED_SSP_IDS),
                "message": (
                    f"id {entity.id!r} is reserved and cannot be "
                    "created via the API"
                ),
            },
        )


async def _reject_reserved_ssp_delete(entity_id: str, request: Request) -> None:
    """Reject DELETE /v1/ssp/<reserved-id> (403)."""
    if entity_id in RESERVED_SSP_IDS:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "reserved_id_protected",
                "kind": "ssp",
                "message": (
                    f"id {entity_id!r} is a reserved SSP and cannot be deleted"
                ),
            },
        )


# ---------------------------------------------------------------------------
# CRUD hook: on_create (no-op — no adapter to warm)
# ---------------------------------------------------------------------------


async def _on_create(entity_id: str, request: Request) -> None:
    """No-op: SemanticSearchRegistry lazy-constructs on first use."""


# ---------------------------------------------------------------------------
# CRUD hook: on_update — invalidate cached adapter
# ---------------------------------------------------------------------------


async def _on_update(entity_id: str, request: Request) -> None:
    """Invalidate the cached VectorStoreProvider instance for this SSP row.

    Called after PUT /v1/ssp/{id}; the next call to
    SemanticSearchRegistry.get_provider(id) will re-resolve the row
    from storage and reconstruct the live backend.
    """
    registry = getattr(request.app.state, "semantic_search_registry", None)
    if registry is not None:
        await registry.invalidate(entity_id)


# ---------------------------------------------------------------------------
# _types + _test (draft) helpers -- MUST be mounted before the CRUD
# router in _app_routes.py, or GET /ssp/{entity_id} swallows "_types"
# and "_test" as path params (same constraint as the LLM/embedding/
# cross-encoder _types routers -- providers.py:232-233).
# ---------------------------------------------------------------------------


def _postgres_family_fields() -> list[dict[str, Any]]:
    """Core connection fields shared by pgvector and pgvectorscale
    (primer.model.providers.storage._PostgresBaseConfig). Advanced
    tuning knobs (HNSW/DiskANN params, db_schema, pool) all have sane
    defaults and are left off this minimal form, same as how the LLM
    family's Limits block is flagged rather than enumerated field-by-
    field (providers.py._with_limits)."""
    return [
        _form_field("hostname", "Host", "text", required=True),
        _form_field("port", "Port", "number"),
        _form_field("username", "Username", "text", required=True),
        _form_field("password", "Password", "password", required=True),
        _form_field("database", "Database", "text", required=True),
    ]


semantic_search_provider_helpers_router = APIRouter(tags=["semantic-search-providers"])


@semantic_search_provider_helpers_router.get(
    "/ssp/_types",
    summary="Provider-type metadata for the SSP register form",
)
async def list_semantic_search_provider_types() -> dict[str, dict[str, Any]]:
    return {
        SemanticSearchProviderType.PGVECTOR.value: {
            "label": "pgvector",
            "config_fields": _postgres_family_fields(),
            "row_fields": [],
            # No live "list models" analogue for a vector store (see
            # platform wave P2 addendum A for what this flag means on
            # the LLM family); every SSP kind is non-discoverable.
            "discoverable": False,
        },
        SemanticSearchProviderType.PGVECTORSCALE.value: {
            "label": "pgvectorscale",
            "config_fields": _postgres_family_fields(),
            "row_fields": [],
            "discoverable": False,
        },
        SemanticSearchProviderType.LANCE.value: {
            "label": "LanceDB (embedded)",
            "config_fields": [
                _form_field(
                    "path", "Storage directory", "text", required=True,
                    placeholder="/var/lib/primer/lance",
                ),
            ],
            "row_fields": [],
            "discoverable": False,
        },
    }


class _SspTestDraft(BaseModel):
    """Body for ``POST /ssp/_test``: a draft config, never persisted."""

    provider: str
    config: dict[str, Any]


def _validate_ssp_draft(provider: str, config: dict[str, Any]) -> SemanticSearchProvider:
    """Construct a transient SemanticSearchProvider for a reachability probe.

    Validates via the canonical model so a shape error surfaces as a
    clean {ok: false, error} rather than an obscure crash inside the
    probe itself. The id is synthesized -- never persisted, never used
    beyond satisfying Identifiable.
    """
    try:
        return SemanticSearchProvider.model_validate({
            "id": f"_probe_{uuid4().hex[:8]}",
            "provider": provider,
            "config": config,
        })
    except ValidationError as exc:
        raise ValueError(f"draft SSP config failed validation: {exc}") from exc


async def _run_ssp_probe(row: SemanticSearchProvider) -> dict[str, Any]:
    """Dispatch to the backend-appropriate reachability check.

    Zero schema/filesystem mutation by design (see module docstring):
    a Postgres-family probe opens a bare connection and runs a no-op
    query; a lance probe only checks read/write access, it never
    creates the directory VectorStoreProvider.initialize() would.
    """
    cfg = row.config
    try:
        if isinstance(cfg, (PgVectorConfig, PgVectorScaleConfig)):
            await _probe_postgres_reachable(cfg)
        elif isinstance(cfg, LanceConfig):
            _probe_lance_path(cfg)
        else:  # pragma: no cover - defensive, the discriminator guards this
            return {"ok": False, "error": f"unknown SSP config type: {type(cfg)}"}
    except Exception as exc:  # noqa: BLE001 - diagnostic-only path
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True}


async def _probe_postgres_reachable(config: PgVectorConfig | PgVectorScaleConfig) -> None:
    """Open a bare asyncpg connection and run a trivial query.

    No CREATE SCHEMA, no CREATE EXTENSION -- those are
    VectorStoreProvider.initialize()'s job when the operator actually
    saves and uses the provider, not this probe's.
    """
    import asyncpg

    conn = await asyncpg.connect(
        host=config.hostname,
        port=config.port,
        user=config.username,
        password=config.password.get_secret_value(),
        database=config.database,
        timeout=10.0,
    )
    try:
        await conn.fetchval("SELECT 1")
    finally:
        await conn.close()


def _probe_lance_path(config: LanceConfig) -> None:
    """Filesystem reachability check for LanceDB.

    Read/write access only -- does NOT create the directory (that is
    VectorStoreProvider.initialize()'s job, "created on first use" per
    LanceConfig's own docstring). A path that does not exist yet is
    fine as long as its parent is writable (the directory will be
    created at that point, not by this probe).
    """
    target = config.path if config.path.exists() else config.path.parent
    if not target.exists():
        raise ValueError(f"neither {config.path} nor its parent directory exists")
    if not os.access(target, os.W_OK):
        raise ValueError(f"{target} is not writable by the primer process")


@semantic_search_provider_helpers_router.post(
    "/ssp/_test",
    responses=common_responses(500),
    summary=(
        "Test a draft SSP config's reachability. No schema/filesystem "
        "mutation. Returns {ok: true} or {ok: false, error}."
    ),
)
async def test_semantic_search_provider_draft(
    body: _SspTestDraft = Body(...),
) -> dict[str, Any]:
    try:
        draft = _validate_ssp_draft(body.provider, body.config)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return await _run_ssp_probe(draft)


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


semantic_search_router = make_crud_router(
    model_cls=SemanticSearchProvider,
    storage_dep=get_semantic_search_storage,
    plural="ssp",
    tag="semantic-search-providers",
    on_create=_on_create,
    on_update=_on_update,
    on_delete=_on_update,
    on_pre_create=_reject_reserved_ssp_create,
    on_pre_delete_id=_reject_reserved_ssp_delete,
)


@semantic_search_router.post(
    "/ssp/{entity_id}/invalidate",
    status_code=204,
    summary="Invalidate cached SemanticSearch adapter",
    responses=common_responses(500),
)
async def invalidate_semantic_search_provider(
    entity_id: str = Path(..., description="SemanticSearchProvider id"),
    registry=Depends(get_semantic_search_registry),
) -> None:
    await registry.invalidate(entity_id)


@semantic_search_router.get(
    "/ssp/{entity_id}/_test",
    responses=common_responses(404, 500),
    summary=(
        "Test a saved SSP's reachability using its stored (unredacted) "
        "config. No schema/filesystem mutation. Returns {ok: true} or "
        "{ok: false, error}."
    ),
)
async def test_semantic_search_provider_saved(
    entity_id: str = Path(..., description="SemanticSearchProvider id"),
    storage=Depends(get_semantic_search_storage),
) -> dict[str, Any]:
    """Two-endpoint shape mirroring the LLM family
    (discover_llm_models / discover_saved_llm_models,
    primer/api/routers/providers.py): the draft variant above cannot
    serve a saved provider's detail page, since a GET the console holds
    has its password redacted (SecretStr's default json-mode masking).
    Reading the stored row server-side keeps the real password where it
    belongs -- this route never receives it over the wire at all, in
    either direction.
    """
    row = await storage.get(entity_id)
    if row is None:
        raise NotFoundError(f"SemanticSearchProvider {entity_id!r} does not exist")
    return await _run_ssp_probe(row)


__all__ = ["semantic_search_provider_helpers_router", "semantic_search_router"]

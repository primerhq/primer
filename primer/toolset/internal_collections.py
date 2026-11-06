"""``internal_collections`` toolset - the internal-collections subsystem's admin surface.

The subsystem's REST surface (PUT/GET/DELETE /v1/internal_collections/config,
POST /v1/internal_collections/bootstrap, GET .../bootstrap/status) is
admin-only and, until this toolset, had no agent-reachable tools: an
operator agent asked to enable the subsystem had to raw-curl the REST
routes it cannot authenticate to. These five tools mirror the five
routes 1:1 (same validation, same frozen-field rule, same inline
bootstrap, same status vocabulary) so the enable-internal-collections
runbook is walkable with the agent's own tools:

  get_config         - read the config row (``{"configured": false}`` when absent)
  set_config         - upsert it (vector-space fields freeze once active)
  delete_config      - deactivate (disable search + drop the reserved collections)
  bootstrap          - enable search on the system collection, inline
  bootstrap_status   - the system collection's search state

Every tool is admin-only (``required_role="admin"``): the subsystem
vectorises the platform's own document corpus, which is platform
administration, and the REST routes it mirrors are admin-routed. The
three writes (``set_config``, ``delete_config``, ``bootstrap``) also ship
the admin-tier default approval gate (``primer.toolset.admin_gates``):
a run at an admin's rank parks them for a person, decided by an admin
only; the two reads are ungated.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from primer.knowledge.lifecycle import disable_search, enable_search, search_status
from primer.knowledge.system_collection import SYSTEM_COLLECTION_ID
from primer.model.chat import ToolCallResult, ToolExample
from primer.model.collection import CollectionEmbedder, CollectionSearchConfig
from primer.model.except_ import ConflictError, NotFoundError, PrimerError
from primer.model.internal import (
    INTERNAL_COLLECTION_IDS,
    INTERNAL_COLLECTIONS_CONFIG_ID,
    InternalCollectionsConfig,
)
from primer.model.provider import SemanticSearchProvider
from primer.model.search import CollectionCrossEncoder
from primer.toolset._describe import make_tool
from primer.toolset._helpers import err as _err, ok_json as _ok
from primer.toolset.admin_gates import apply_admin_gate_previews
from primer.toolset.internal import InternalToolsetProvider

logger = logging.getLogger(__name__)

INTERNAL_COLLECTIONS_TOOLSET_ID = "internal_collections"


class _SetConfigArgs(BaseModel):
    """The enable-internal-collections flow's config write (D1 step 2)."""

    embedding_provider_id: str = Field(
        ..., min_length=1,
        description=(
            "Id of the configured EmbeddingProvider to use for every "
            "internal collection. Must reference an existing provider row."
        ),
    )
    embedding_model: str = Field(
        ..., min_length=1,
        description=(
            "Provider-side embedding model name. Must be one of the models "
            "permitted on the referenced provider (checked at bootstrap)."
        ),
    )
    search_provider_id: str = Field(
        ..., min_length=1,
        description=(
            "Id of the SemanticSearchProvider that backs the internal "
            "collections. Must reference an existing SSP row."
        ),
    )
    cross_encoder: CollectionCrossEncoder | None = Field(
        default=None,
        description=(
            "Optional cross-encoder reranker (provider_id + model). "
            "Omit for none; the vector search runs without a reranker. "
            "It stays editable while the subsystem is active."
        ),
    )


def _parse(model: type[BaseModel], arguments: dict[str, Any]):
    """Parse the tool arguments, or return the validation error result."""
    try:
        return model(**arguments), None
    except ValidationError as exc:
        return None, _err(
            "argument validation failed: "
            + str(exc).replace("\n", " "),
            error_type="validation-error",
        )


def _map_error(exc: PrimerError) -> ToolCallResult:
    if isinstance(exc, NotFoundError):
        return _err(str(exc), error_type="not-found")
    if isinstance(exc, ConflictError):
        return _err(str(exc), error_type="conflict")
    return _err(str(exc), error_type="tool-error")


def build_internal_collections_toolset(
    *,
    storage_provider,
    provider_registry=None,
    semantic_search_registry=None,
) -> InternalToolsetProvider:
    """Construct the always-on ``internal_collections`` toolset."""
    storage = storage_provider
    ssr = semantic_search_registry

    async def _get_config(arguments: dict[str, Any]) -> ToolCallResult:
        row = await storage.get_storage(InternalCollectionsConfig).get(
            INTERNAL_COLLECTIONS_CONFIG_ID,
        )
        if row is None:
            return _ok({"configured": False})
        return _ok(row.model_dump(mode="json"))

    async def _set_config(arguments: dict[str, Any]) -> ToolCallResult:
        body, error = _parse(_SetConfigArgs, arguments)
        if error is not None:
            return error
        # The route's first validation: the SSP must exist.
        ssp_row = await storage.get_storage(SemanticSearchProvider).get(
            body.search_provider_id,
        )
        if ssp_row is None:
            return _map_error(NotFoundError(
                f"search_provider_id {body.search_provider_id!r} does not "
                "refer to a known SemanticSearchProvider.",
            ))
        existing = await storage.get_storage(InternalCollectionsConfig).get(
            INTERNAL_COLLECTIONS_CONFIG_ID,
        )
        # Vector-space-defining fields freeze once the subsystem is
        # active: mixing embedding spaces corrupts search. Only
        # cross_encoder (and a no-op identical write) stays mutable.
        if existing is not None and existing.activated_at is not None:
            frozen_diffs = []
            if body.embedding_provider_id != existing.embedding_provider_id:
                frozen_diffs.append("embedding_provider_id")
            if body.embedding_model != existing.embedding_model:
                frozen_diffs.append("embedding_model")
            if body.search_provider_id != existing.search_provider_id:
                frozen_diffs.append("search_provider_id")
            if frozen_diffs:
                return _map_error(ConflictError(
                    f"Cannot change {', '.join(frozen_diffs)} while the "
                    "subsystem is active - these fields define the vector "
                    "space and mixing them would corrupt search results. "
                    "Deactivate the subsystem first (delete_config), then "
                    "re-configure and re-bootstrap.",
                ))
        cfg = InternalCollectionsConfig(
            id=INTERNAL_COLLECTIONS_CONFIG_ID,
            embedding_provider_id=body.embedding_provider_id,
            embedding_model=body.embedding_model,
            search_provider_id=body.search_provider_id,
            cross_encoder=body.cross_encoder,
            activated_at=None,
        )
        if existing is None:
            await storage.get_storage(InternalCollectionsConfig).create(cfg)
        else:
            # Preserve the prior activated_at so an update doesn't appear
            # to deactivate the subsystem.
            cfg = cfg.model_copy(
                update={"activated_at": existing.activated_at},
            )
            await storage.get_storage(InternalCollectionsConfig).update(cfg)
        return _ok(cfg.model_dump(mode="json"))

    async def _delete_config(arguments: dict[str, Any]) -> ToolCallResult:
        row = await storage.get_storage(InternalCollectionsConfig).get(
            INTERNAL_COLLECTIONS_CONFIG_ID,
        )
        if row is None:
            return _map_error(NotFoundError(
                "internal collections subsystem is not configured; nothing "
                "to delete.",
            ))
        # Deactivate: disable semantic search on the system collection
        # and drop the reserved collections from the SSP's backing store
        # (so a re-configuration with a different embedding model cannot
        # surface stale dimensions). Custom collections in the same SSP
        # are untouched.
        dropped: list[str] = []
        try:
            await disable_search(
                storage,
                ssr,
                collection_id=SYSTEM_COLLECTION_ID,
            )
        except NotFoundError:
            pass  # nothing to disable
        if ssr is not None:
            try:
                store = await ssr.get_store(row.search_provider_id)
            except Exception as exc:  # noqa: BLE001 - tolerate registry errors
                logger.warning(
                    "ic deactivate (tool): cannot resolve store for ssp %r: "
                    "%s; skipping collection drops",
                    row.search_provider_id, exc,
                )
                store = None
            if store is not None:
                for coll_id in INTERNAL_COLLECTION_IDS.values():
                    try:
                        await store.drop_collection(coll_id)
                        dropped.append(coll_id)
                    except Exception as exc:  # noqa: BLE001 - best-effort
                        logger.warning(
                            "ic deactivate (tool): drop_collection(%r) on "
                            "ssp %r failed: %s",
                            coll_id, row.search_provider_id, exc,
                        )
        # The config row goes last: a partial failure leaves it in place
        # so the operator can retry (the drops are idempotent).
        await storage.get_storage(InternalCollectionsConfig).delete(
            INTERNAL_COLLECTIONS_CONFIG_ID,
        )
        return _ok({"deleted": True, "collections_dropped": dropped})

    async def _bootstrap(arguments: dict[str, Any]) -> ToolCallResult:
        """Enable search on the system collection, inline (the route's S2 shape)."""
        from datetime import datetime, timezone

        config_storage = storage.get_storage(InternalCollectionsConfig)
        cfg = await config_storage.get(INTERNAL_COLLECTIONS_CONFIG_ID)
        if cfg is None:
            return _map_error(NotFoundError(
                "internal collections subsystem is not configured; "
                "set_config first.",
            ))
        if cfg.activated_at is None:
            cfg = cfg.model_copy(
                update={"activated_at": datetime.now(timezone.utc)},
            )
            await config_storage.update(cfg)
        search_cfg = CollectionSearchConfig(
            embedder=CollectionEmbedder(
                provider_id=cfg.embedding_provider_id,
                model=cfg.embedding_model,
            ),
            vector_store_provider_id=cfg.search_provider_id,
            cross_encoder=cfg.cross_encoder,
        )
        try:
            updated = await enable_search(
                storage,
                provider_registry,
                ssr,
                collection_id=SYSTEM_COLLECTION_ID,
                cfg=search_cfg,
            )
        except PrimerError as exc:
            return _map_error(exc)
        state = updated.search.state if updated.search else "disabled"
        return _ok({
            "status": "succeeded" if state == "ready" else "failed",
            "state": state,
            "collection_id": SYSTEM_COLLECTION_ID,
            "error": updated.search.error if updated.search else None,
            "search": (
                updated.search.model_dump(mode="json")
                if updated.search else None
            ),
        })

    async def _bootstrap_status(arguments: dict[str, Any]) -> ToolCallResult:
        try:
            status = await search_status(
                storage,
                ssr,
                collection_id=SYSTEM_COLLECTION_ID,
            )
        except PrimerError as exc:
            return _map_error(exc)
        body = status.model_dump(mode="json")
        # ``status`` mirrors the lifecycle state in the terminal
        # vocabulary the console polls on; ``state`` is authoritative.
        body["status"] = {
            "ready": "succeeded", "error": "failed",
            "indexing": "running", "disabled": "idle",
        }[status.state]
        return _ok(body)

    empty_schema: dict[str, Any] = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    registry: dict[str, tuple] = {}

    registry["get_config"] = (
        make_tool(
            id="get_config",
            toolset_id=INTERNAL_COLLECTIONS_TOOLSET_ID,
            purpose="Read the internal-collections subsystem config row.",
            when=(
                "Use to check whether the subsystem is configured and with "
                "which embedding provider/model and search provider before "
                "walking the enable-internal-collections flow "
                "(runbooks/configure-internal-collections). Answers "
                "{\"configured\": false} when there is no config row."
            ),
            args_schema=empty_schema,
            examples=[ToolExample(
                args={},
                returns='{"configured": false}',
                note="no config row yet; the subsystem is off",
            )],
            required_role="admin",
        ),
        _get_config,
    )

    registry["set_config"] = (
        make_tool(
            id="set_config",
            toolset_id=INTERNAL_COLLECTIONS_TOOLSET_ID,
            purpose="Activate / re-configure the internal-collections subsystem.",
            when=(
                "Use for the flow's config write (runbooks/configure-"
                "internal-collections, step 2): the embedding provider + "
                "model and the search provider that back the internal "
                "collections, plus an optional cross-encoder reranker. "
                "Once the subsystem is active, the vector-space fields "
                "(embedding provider, model, search provider) are frozen: "
                "change one and the tool answers a conflict naming the "
                "frozen fields - deactivate (delete_config) first, then "
                "re-configure and re-bootstrap."
            ),
            args_schema=_SetConfigArgs.model_json_schema(),
            examples=[ToolExample(
                args={
                    "embedding_provider_id": "huggingface",
                    "embedding_model": "BAAI/bge-small-en-v1.5",
                    "search_provider_id": "lance",
                },
                returns='{"id": "_internal_collections_config", "embedding_provider_id": "huggingface", "embedding_model": "BAAI/bge-small-en-v1.5", "search_provider_id": "lance", "cross_encoder": null, "activated_at": null}',
                note="a clean install's seeded rows: the reserved local embedder + the reserved lance SSP",
            )],
            required_role="admin",
        ),
        _set_config,
    )

    registry["delete_config"] = (
        make_tool(
            id="delete_config",
            toolset_id=INTERNAL_COLLECTIONS_TOOLSET_ID,
            purpose="Deactivate the internal-collections subsystem.",
            when=(
                "Use to switch the subsystem off: it disables semantic "
                "search on the system collection, drops the reserved "
                "internal collections from the backing search provider "
                "(so a re-configuration with a different embedding model "
                "rebuilds cleanly) and removes the config row. Custom "
                "collections in the same search provider are not touched."
            ),
            args_schema=empty_schema,
            examples=[ToolExample(
                args={},
                returns='{"deleted": true, "collections_dropped": ["_internal_agents", "_internal_graphs", "_internal_collections", "_internal_tools"]}',
            )],
            required_role="admin",
            interruptible=False,
        ),
        _delete_config,
    )

    registry["bootstrap"] = (
        make_tool(
            id="bootstrap",
            toolset_id=INTERNAL_COLLECTIONS_TOOLSET_ID,
            purpose=(
                "Enable semantic search on the system collection and "
                "index it (inline; returns the terminal outcome)."
            ),
            when=(
                "Use after set_config to activate the subsystem "
                "(runbooks/configure-internal-collections, step 3). It "
                "runs the embedding pass over the system collection's "
                "documents before it returns, so it can take from tens of "
                "seconds to minutes depending on the model and the corpus. "
                "Idempotent: re-running re-indexes. The embedding model "
                "must be one the configured provider permits; a mismatch "
                "surfaces here, not at set_config."
            ),
            args_schema=empty_schema,
            examples=[ToolExample(
                args={},
                returns='{"status": "succeeded", "state": "ready", "collection_id": "system", "error": null, "search": {"state": "ready", "documents_total": 416, "documents_indexed": 416, ...}}',
            )],
            required_role="admin",
            interruptible=False,
        ),
        _bootstrap,
    )

    registry["bootstrap_status"] = (
        make_tool(
            id="bootstrap_status",
            toolset_id=INTERNAL_COLLECTIONS_TOOLSET_ID,
            purpose="Read the internal-collections bootstrap / lifecycle state.",
            when=(
                "Use as the flow's verify step (runbooks/configure-"
                "internal-collections, step 4): the system collection's "
                "search state - ready, indexing, error or disabled - plus "
                "the document counts. Always answers (a synthetic idle row "
                "when nothing has ever run)."
            ),
            args_schema=empty_schema,
            examples=[ToolExample(
                args={},
                returns='{"state": "ready", "documents_total": 416, "documents_indexed": 416, "status": "succeeded", ...}',
                note="ready with the counts matching is the verified end state",
            )],
            required_role="admin",
        ),
        _bootstrap_status,
    )

    # The three writes ship the admin-tier default gate (the lead's ruling,
    # wiki-first M2 step 0): the approval card PREVIEW_ARGS declares on
    # set_config is checked against its schema here.
    apply_admin_gate_previews(INTERNAL_COLLECTIONS_TOOLSET_ID, registry)

    return InternalToolsetProvider(
        toolset_id=INTERNAL_COLLECTIONS_TOOLSET_ID,
        registry=registry,
    )


__all__ = [
    "INTERNAL_COLLECTIONS_TOOLSET_ID",
    "build_internal_collections_toolset",
]

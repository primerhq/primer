"""Persisted models for the internal collections subsystem.

Two models live here:

* :class:`InternalCollectionsConfig` — single-row activation config
  (id reserved as :data:`INTERNAL_COLLECTIONS_CONFIG_ID`). Carries the
  embedding provider / model and optional rerank / MMR config that
  drives the four reserved internal collections (one per Describeable
  entity type).
* :class:`IngestFailure` — append-only audit table the CDC worker
  writes to whenever a document upsert / delete fails. The future
  global retry scheduler will read these rows; the worker itself just
  logs and moves on.

Both ride on the existing :class:`Storage[T]` interface; they have no
special storage semantics.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from primer.model.common import Identifiable
from primer.model.search import CollectionCrossEncoder, MmrConfig


# Reserved row id for the singleton config row. Kept stable so the
# activation API can consistently locate / upsert the row without the
# caller having to know the convention.
INTERNAL_COLLECTIONS_CONFIG_ID = "_internal_collections_config"

# Reserved row id for the singleton bootstrap-progress row.
# A separate row from the config so progress writes during a long
# ingest don't churn the config's updated_at / cause version conflicts
# with config edits from the UI.
INTERNAL_COLLECTIONS_BOOTSTRAP_STATUS_ID = "_internal_collections_bootstrap_status"

# Conventional internal collection ids (one per Describeable entity).
# The bootstrap orchestrator materialises these as :class:`Collection`
# rows with ``system=True``.
INTERNAL_COLLECTION_IDS: dict[str, str] = {
    "agent": "_internal_agents",
    "graph": "_internal_graphs",
    "collection": "_internal_collections",
    "tool": "_internal_tools",
}

# Reserved id for the agent-facing platform docs collection. Distinct
# from the four entity-keyed collections above: it does NOT track
# storage rows. Its records are produced by walking
# :mod:`primer.ai_docs` markdown files at bootstrap, chunking via the
# Docling-backed :class:`primer.ingest.DocumentIngester`, and embedding
# each chunk. Search-only from the agent side; no CDC events are ever
# enqueued for this collection.
AI_DOCS_COLLECTION_ID = "_internal_ai_docs"


class InternalCollectionsConfig(Identifiable):
    """Activation config for the internal collections subsystem.

    Persisted as a single row at id :data:`INTERNAL_COLLECTIONS_CONFIG_ID`.
    The presence of the row at startup tells the lifespan handler to
    activate the subsystem (build collections lazily on the first
    explicit bootstrap call, but start the CDC worker immediately so
    no live mutation is missed).
    """

    embedding_provider_id: str = Field(
        ...,
        min_length=1,
        description=(
            "Id of the configured "
            ":class:`primer.model.provider.EmbeddingProvider` used to "
            "vectorise every internal collection. Must reference an "
            "existing provider row at activation time."
        ),
    )
    embedding_model: str = Field(
        ...,
        min_length=1,
        description=(
            "Provider-side embedding model name. Must be one of the "
            "models permitted on the referenced provider."
        ),
    )
    cross_encoder: CollectionCrossEncoder | None = Field(
        default=None,
        description=(
            "Optional cross-encoder reranker applied during search "
            "across every internal collection. ``None`` disables "
            "reranking; vector-store score is preserved."
        ),
    )
    search_provider_id: str = Field(
        ...,
        min_length=1,
        description=(
            "Id of the SemanticSearchProvider that backs the four reserved "
            "internal collections (_internal_agents, _internal_graphs, "
            "_internal_collections, _internal_tools)."
        ),
    )
    mmr: MmrConfig | None = Field(
        default=None,
        description=(
            "Optional Maximal Marginal Relevance diversification "
            "applied during search. ``None`` disables MMR; default "
            "vector ranking is preserved."
        ),
    )
    activated_at: datetime | None = Field(
        default=None,
        description=(
            "UTC instant of the most recent successful bootstrap. "
            "``None`` means the subsystem is configured but has not "
            "completed its first bootstrap yet — search APIs and the "
            "``_search`` toolset return 503 until the bootstrap "
            "completes."
        ),
    )


BootstrapPhase = Literal[
    "drain_queue",
    "materialise_collections",
    "ingest_agents",
    "ingest_graphs",
    "ingest_collections",
    "ingest_tools",
    "ingest_ai_docs",
    "finalize",
]


class InternalCollectionsBootstrapStatus(Identifiable):
    """Singleton progress + lifecycle row for the bootstrap pipeline.

    Holds the latest bootstrap attempt's state so the UI can render a
    durable progress card that survives navigation, browser refreshes,
    and (via recovery in the lifespan handler) API process restarts.

    Persisted as id :data:`INTERNAL_COLLECTIONS_BOOTSTRAP_STATUS_ID`.
    The router refuses to start a new bootstrap while ``status ==
    'running'`` — combined with the lifespan handler clearing stale
    'running' rows at boot, this gives us "exactly one active bootstrap
    at a time" without an in-memory mutex.
    """

    status: Literal["idle", "running", "succeeded", "failed"] = Field(
        default="idle",
        description=(
            "High-level lifecycle. ``idle`` means no bootstrap has run "
            "yet (or the row was reset). ``running`` means a background "
            "task is actively materialising collections / ingesting "
            "entities. ``succeeded`` / ``failed`` are terminal."
        ),
    )
    phase: BootstrapPhase | None = Field(
        default=None,
        description=(
            "Current step inside a running bootstrap. ``None`` for "
            "idle/succeeded/failed unless a phase was in flight at "
            "failure time."
        ),
    )
    phase_done: int = Field(
        default=0,
        ge=0,
        description=(
            "Items completed in the current phase. Resets at every "
            "phase transition. Used together with ``phase_total`` to "
            "render a progress bar."
        ),
    )
    phase_total: int | None = Field(
        default=None,
        description=(
            "Best-known item count for the current phase, when one "
            "exists. ``None`` for phases where the total is unknown up "
            "front (e.g. tools enumerated lazily from providers)."
        ),
    )
    counts: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Running totals carried through every phase: "
            "``{'agents': N, 'graphs': N, 'collections': N, 'tools': N}``. "
            "Filled progressively so the UI can show a partial summary "
            "even mid-flight."
        ),
    )
    started_at: datetime | None = Field(
        default=None,
        description="UTC instant the current attempt began.",
    )
    finished_at: datetime | None = Field(
        default=None,
        description=(
            "UTC instant the latest attempt resolved (succeeded or "
            "failed). ``None`` while ``status == 'running'``."
        ),
    )
    error: str | None = Field(
        default=None,
        description=(
            "Single-line error message captured when ``status == "
            "'failed'``. Stored as a plain string so the UI can render "
            "it directly; the full traceback goes to the server log."
        ),
    )
    attempt_id: str | None = Field(
        default=None,
        description=(
            "Per-run UUID. Lets a slow background task detect that its "
            "row was overwritten by a newer attempt (rare; would only "
            "happen if a stale 'running' row got cleared and a new "
            "POST raced in) and abort its writes."
        ),
    )


class IngestFailure(Identifiable):
    """One CDC ingest attempt that failed.

    Append-only — the worker writes a row on each failure, the future
    global scheduler reads + retries them. Successful upserts produce
    no row; the storage of last-success is implicit in the vector
    store itself.
    """

    entity_type: Literal["agent", "graph", "collection", "tool"] = Field(
        ...,
        description="Which internal collection the failed event targeted.",
    )
    entity_id: str = Field(
        ...,
        min_length=1,
        description=(
            "Id of the entity whose ingest attempt failed. For tool "
            "events this is the composite ``<toolset_id>::<tool_id>`` "
            "document id."
        ),
    )
    op: Literal["upsert", "delete"] = Field(
        ...,
        description="Which CDC operation was attempted.",
    )
    error: str = Field(
        ...,
        description=(
            "Human-readable error message captured from the embedder "
            "or vector store at the moment of failure."
        ),
    )
    failed_at: datetime = Field(
        ...,
        description="UTC instant of the failure.",
    )
    retry_count: int = Field(
        default=0,
        ge=0,
        description=(
            "Number of times the global retry scheduler has replayed "
            "this event. Bumped on each retry; the row is not deleted "
            "on success — the scheduler updates a separate ``resolved`` "
            "flag (added in a follow-up sub-project)."
        ),
    )


__all__ = [
    "AI_DOCS_COLLECTION_ID",
    "BootstrapPhase",
    "INTERNAL_COLLECTION_IDS",
    "INTERNAL_COLLECTIONS_BOOTSTRAP_STATUS_ID",
    "INTERNAL_COLLECTIONS_CONFIG_ID",
    "IngestFailure",
    "InternalCollectionsBootstrapStatus",
    "InternalCollectionsConfig",
]

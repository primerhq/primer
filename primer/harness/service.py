"""Service layer for the harness feature.

Handles:
- Building the (kind, template_name) → resolved_id map
- Rewriting cross-references in rendered entity payloads
- Pydantic-validating rendered payloads before any storage writes
- apply_install / apply_sync / apply_uninstall orchestrators

See docs/superpowers/specs/2026-05-27-harness-design.md §9 for the full
operations spec.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError

import logging

from primer.harness.diff import diff_renderings
from primer.harness.hashes import hash_rendered_payload, hash_template_source
from primer.harness.template import RenderedFile
from primer.model.except_ import ConflictError
from primer.model.harness import Harness, HarnessRendering, RenderedEntry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public ID formatter
# ---------------------------------------------------------------------------


def resolved_id(slug: str, template_name: str) -> str:
    """Compute the resolved entity id for a harness-managed entity."""
    return f"{slug}__{template_name}"


# ---------------------------------------------------------------------------
# Build errors container
# ---------------------------------------------------------------------------


@dataclass
class BuildErrors:
    """Per-template validation errors collected during the build step."""

    errors: list[dict[str, Any]] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.errors)


# ---------------------------------------------------------------------------
# Cross-reference rewriting
# ---------------------------------------------------------------------------


def _build_rewrite_map(
    rendered: list[RenderedFile],
    *,
    default_slug: str,
) -> dict[tuple[str, str, str], str]:
    """Build (kind, template_name, source_slug) → resolved_id for the bundle.

    With multi-harness rendering (Spec A §8) each RenderedFile may belong
    to its own slug (its ``source_slug``). The rewrite map is keyed by
    (kind, template_name, source_slug) so two subs declaring the same
    bare template_name get distinct resolved_ids.

    When a file's ``source_slug`` is unset we fall back to ``default_slug``
    (the parent harness's slug), preserving single-harness behaviour.
    """
    out: dict[tuple[str, str, str], str] = {}
    for f in rendered:
        slug = f.source_slug or default_slug
        out[(f.kind, f.template_name, slug)] = resolved_id(slug, f.template_name)
    return out


def _lookup_resolved(
    rewrite_map: dict[tuple[str, str, str], str],
    *,
    kind: str,
    template_name: str,
    file_slug: str,
) -> str | None:
    """Resolve ``template_name`` against the rewrite map for a given file slug.

    Lookup order:
      1. The file's own slug — sub-references-its-own-entity (the common case).
      2. Any other slug — used for parent→sub cross-refs (e.g. parent's agent
         pointing at a sub's toolset). When multiple slugs declare the same
         template_name we pick the lexicographically-first slug deterministically.
    """
    own = rewrite_map.get((kind, template_name, file_slug))
    if own is not None:
        return own
    matches = sorted(
        (slug for (k, n, slug) in rewrite_map
         if k == kind and n == template_name),
    )
    if not matches:
        return None
    return rewrite_map[(kind, template_name, matches[0])]


def _rewrite_agent_payload(
    payload: dict[str, Any],
    rewrite_map: dict[tuple[str, str, str], str],
    *,
    file_slug: str,
) -> dict[str, Any]:
    """Rewrite Agent.tools scoped ids whose toolset_id is a harness toolset."""
    tools = payload.get("tools")
    if not isinstance(tools, list):
        return payload

    new_tools: list[str] = []
    for tool in tools:
        if not isinstance(tool, str):
            new_tools.append(tool)
            continue
        # Tool format: <toolset_id>__<tool_name>
        parts = tool.split("__", 1)
        if len(parts) == 2:
            toolset_resolved = _lookup_resolved(
                rewrite_map, kind="toolset", template_name=parts[0],
                file_slug=file_slug,
            )
            if toolset_resolved is not None:
                new_tools.append(f"{toolset_resolved}__{parts[1]}")
                continue
        new_tools.append(tool)

    return {**payload, "tools": new_tools}


def _rewrite_graph_payload(
    payload: dict[str, Any],
    rewrite_map: dict[tuple[str, str, str], str],
    *,
    file_slug: str,
) -> dict[str, Any]:
    """Rewrite Graph.nodes agent_id / graph_id fields when they reference harness entities."""
    nodes = payload.get("nodes")
    if not isinstance(nodes, list):
        return payload

    new_nodes: list[Any] = []
    for node in nodes:
        if not isinstance(node, dict):
            new_nodes.append(node)
            continue

        node = dict(node)  # shallow copy before mutating
        node_kind = node.get("kind")

        if node_kind == "agent":
            agent_id = node.get("agent_id")
            if isinstance(agent_id, str):
                resolved = _lookup_resolved(
                    rewrite_map, kind="agent", template_name=agent_id,
                    file_slug=file_slug,
                )
                if resolved is not None:
                    node["agent_id"] = resolved

        elif node_kind == "graph":
            graph_id = node.get("graph_id")
            if isinstance(graph_id, str):
                resolved = _lookup_resolved(
                    rewrite_map, kind="graph", template_name=graph_id,
                    file_slug=file_slug,
                )
                if resolved is not None:
                    node["graph_id"] = resolved

        elif node_kind == "tool_call":
            # tool_id is an unresolved bundle ref ``<toolset_template>__<bare_name>``,
            # like agent.tools entries. Rewrite the toolset segment to its resolved id.
            tool_id = node.get("tool_id")
            if isinstance(tool_id, str):
                parts = tool_id.split("__", 1)
                if len(parts) == 2:
                    toolset_resolved = _lookup_resolved(
                        rewrite_map, kind="toolset", template_name=parts[0],
                        file_slug=file_slug,
                    )
                    if toolset_resolved is not None:
                        node["tool_id"] = f"{toolset_resolved}__{parts[1]}"

        new_nodes.append(node)

    return {**payload, "nodes": new_nodes}


def _rewrite_document_payload(
    payload: dict[str, Any],
    rewrite_map: dict[tuple[str, str, str], str],
    *,
    file_slug: str,
) -> dict[str, Any]:
    """Rewrite Document.collection_id when it matches a harness collection template_name."""
    collection_id = payload.get("collection_id")
    if not isinstance(collection_id, str):
        return payload

    resolved = _lookup_resolved(
        rewrite_map, kind="collection", template_name=collection_id,
        file_slug=file_slug,
    )
    if resolved is not None:
        return {**payload, "collection_id": resolved}
    return payload


def _rewrite_payload(
    kind: str,
    payload: dict[str, Any],
    rewrite_map: dict[tuple[str, str, str], str],
    *,
    file_slug: str,
) -> dict[str, Any]:
    """Dispatch to the kind-specific rewriter."""
    if kind == "agent":
        return _rewrite_agent_payload(payload, rewrite_map, file_slug=file_slug)
    if kind == "graph":
        return _rewrite_graph_payload(payload, rewrite_map, file_slug=file_slug)
    if kind == "document":
        return _rewrite_document_payload(payload, rewrite_map, file_slug=file_slug)
    # Collection and Toolset: no harness cross-refs
    return payload


# ---------------------------------------------------------------------------
# Pydantic validation per kind
# ---------------------------------------------------------------------------

def _harness_kind_models() -> dict[str, type]:
    """Return a mapping of harness-managed kind names to Pydantic model classes.

    Uses the CDC kinds registry as the single source of truth.  Populates
    the registry on first call (and after any test-reset) by doing the same
    lazy imports that the old _kind_models() function used — so this function
    is safe to call even before router modules have been imported, and is
    idempotent when the registry is already fully populated.

    The lazy imports here avoid circular-import issues:
    graph.py → session.py → graph.py.  Importing workspace_session before
    graph ensures the _rebuild_models() call completes first.
    """
    from primer.api.routers._cdc_hooks import (  # noqa: PLC0415
        known_cdc_kinds,
        register_cdc_kind,
    )
    kinds = known_cdc_kinds()
    _required = frozenset({"agent", "graph", "collection", "document", "toolset"})
    if not _required.issubset(kinds.keys()):
        # Registry incomplete (first call, or cleared by a test reset).
        # Import models and re-register to rebuild the mapping.
        from primer.model.agent import Agent  # noqa: PLC0415
        from primer.model.collection import Collection, Document  # noqa: PLC0415
        import primer.model.workspace_session  # noqa: PLC0415, F401 — must precede graph
        from primer.model.graph import Graph  # noqa: PLC0415
        from primer.model.provider import Toolset  # noqa: PLC0415
        for _kind, _cls in (
            ("agent", Agent),
            ("graph", Graph),
            ("collection", Collection),
            ("document", Document),
            ("toolset", Toolset),
        ):
            register_cdc_kind(_kind, _cls)
        kinds = known_cdc_kinds()
    return kinds


def _validate_payload(
    kind: str,
    resolved_entity_id: str,
    payload: dict[str, Any],
) -> tuple[Any, list[dict[str, Any]]]:
    """Pydantic-validate a rendered payload against its entity model.

    Returns (validated_entity, []) on success, or (None, [error_dict]) on failure.
    """
    model_cls = _harness_kind_models()[kind]
    data = {"id": resolved_entity_id, **payload}
    try:
        entity = model_cls.model_validate(data)
        return entity, []
    except ValidationError as exc:
        return None, [
            {
                "loc": list(e["loc"]),
                "msg": e["msg"],
                "type": e["type"],
            }
            for e in exc.errors()
        ]


# ---------------------------------------------------------------------------
# Build rendered entries (validate + rewrite in one pass)
# ---------------------------------------------------------------------------


def build_rendered_entries(
    rendered: list[RenderedFile],
    *,
    slug: str,
) -> tuple[list[RenderedEntry], BuildErrors]:
    """Rewrite cross-refs and Pydantic-validate each RenderedFile.

    ``slug`` is the default/fallback slug used when a file does not set
    ``source_slug`` (i.e. when called from the legacy single-harness path).
    Multi-harness callers (Spec A §8) tag each RenderedFile with
    ``source_slug`` and ``source_dependency``; this function honours both
    when present and threads them through into the resulting
    ``RenderedEntry`` rows.

    Returns (entries, errors). When errors is truthy, entries is empty
    (build-before-apply contract: validate the whole bundle before writing
    anything).
    """
    rewrite_map = _build_rewrite_map(rendered, default_slug=slug)
    errors = BuildErrors()
    entries: list[RenderedEntry] = []

    for f in rendered:
        file_slug = f.source_slug or slug
        spec = dict(f.rendered.get("spec", {}))
        rewritten_payload = _rewrite_payload(
            f.kind, spec, rewrite_map, file_slug=file_slug,
        )

        rid = resolved_id(file_slug, f.template_name)
        entity, validation_errors = _validate_payload(f.kind, rid, rewritten_payload)

        if validation_errors:
            errors.errors.append(
                {
                    "template_name": f.template_name,
                    "kind": f.kind,
                    "code": "pydantic_validation_error",
                    "message": json.dumps(validation_errors),
                }
            )
        else:
            entries.append(
                RenderedEntry(
                    kind=f.kind,
                    template_name=f.template_name,
                    resolved_id=rid,
                    template_source_hash=hash_template_source(f.source_bytes),
                    rendered_hash=hash_rendered_payload(rewritten_payload),
                    rendered_payload=rewritten_payload,
                    source_dependency=f.source_dependency,
                )
            )

    if errors:
        return [], errors

    return entries, errors


# ---------------------------------------------------------------------------
# Storage helper — build entity from rendered entry
# ---------------------------------------------------------------------------


def _entity_from_entry(
    entry: RenderedEntry,
    *,
    harness_id: str,
) -> Any:
    """Reconstruct the entity model from a RenderedEntry with harness_id stamped."""
    model_cls = _harness_kind_models()[entry.kind]
    # harness_id MUST be last so a template accidentally (or maliciously)
    # carrying a harness_id field in the rendered payload can never override
    # the dispatch's own value.
    data = {
        "id": entry.resolved_id,
        **entry.rendered_payload,
        "harness_id": harness_id,
    }
    return model_cls.model_validate(data)


def _storage_for_kind(storage_provider: Any, kind: str) -> Any:
    """Return the storage bucket for a given entity kind string."""
    model_cls = _harness_kind_models()[kind]
    return storage_provider.get_storage(model_cls)


# ---------------------------------------------------------------------------
# Apply order constants
# ---------------------------------------------------------------------------

_INSTALL_ORDER: list[str] = ["toolset", "collection", "document", "agent", "graph"]
_UNINSTALL_ORDER: list[str] = list(reversed(_INSTALL_ORDER))


# ---------------------------------------------------------------------------
# Document indexing (best-effort)
# ---------------------------------------------------------------------------


async def _index_installed_document(
    *,
    storage_provider: Any,
    document_entity: Any,
    provider_registry: Any | None,
    semantic_search_registry: Any | None,
) -> None:
    """Route a harness-installed Document through the same chunk/embed/index
    pipeline the REST ``create_document`` flow uses (its on_create hook).

    Best-effort: when the registries are not wired (pure-storage tests) or
    the embedder / vector store is unavailable, the failure is logged and
    swallowed so the install never fails on indexing. The Document row is
    already persisted; it simply will not be searchable until a later sync
    re-indexes it.
    """
    if provider_registry is None or semantic_search_registry is None:
        return
    try:
        from primer.knowledge.indexing import index_document  # noqa: PLC0415
        from primer.model.collection import Collection  # noqa: PLC0415

        collection = await storage_provider.get_storage(Collection).get(
            document_entity.collection_id
        )
        if collection is None:
            return
        await index_document(
            document=document_entity,
            collection=collection,
            provider_registry=provider_registry,
            semantic_search_registry=semantic_search_registry,
        )
    except Exception:  # noqa: BLE001 - best-effort indexing, never fail install
        logger.exception(
            "harness document %s: indexing failed; row persisted but not "
            "searchable",
            getattr(document_entity, "id", "?"),
        )


# ---------------------------------------------------------------------------
# apply_install
# ---------------------------------------------------------------------------


async def apply_install(
    *,
    storage_provider: Any,
    harness: Harness,
    entries: list[RenderedEntry],
    rendered_files_by_name: dict[str, RenderedFile],
    bundle_hash: str,
    overrides_hash: str,
    schema_hash: str | None,
    provider_registry: Any | None = None,
    semantic_search_registry: Any | None = None,
) -> str | None:
    """Create every entity in storage with harness_id=harness.id.

    Order: toolset → collection → document → agent → graph.
    Writes the HarnessRendering snapshot (id = harness.id).
    Returns None on success or a JSON-encoded error string on failure.

    Document content (content_inline / content_path) is stored in
    Document.meta["content"] when present in the corresponding RenderedFile,
    and each installed Document is routed through the same chunk/embed/index
    pipeline as the REST create_document flow (best-effort: indexing failures
    are logged, never aborting the install). When the registries are omitted
    (pure-storage callers) indexing is skipped.
    """
    # Group entries by kind for ordered application
    by_kind: dict[str, list[RenderedEntry]] = {k: [] for k in _INSTALL_ORDER}
    for entry in entries:
        by_kind.setdefault(entry.kind, []).append(entry)

    created: list[tuple[str, str]] = []  # (kind, resolved_id) for rollback
    created_documents: list[Any] = []  # entities to index after a clean apply

    try:
        for kind in _INSTALL_ORDER:
            for entry in by_kind.get(kind, []):
                payload = dict(entry.rendered_payload)

                # Document content: store in meta["content"] if available
                if kind == "document" and entry.template_name in rendered_files_by_name:
                    rf = rendered_files_by_name[entry.template_name]
                    if rf.content is not None:
                        meta = dict(payload.get("meta") or {})
                        meta["content"] = rf.content
                        payload = {**payload, "meta": meta}

                entity = _entity_from_entry(
                    RenderedEntry(
                        kind=entry.kind,
                        template_name=entry.template_name,
                        resolved_id=entry.resolved_id,
                        template_source_hash=entry.template_source_hash,
                        rendered_hash=entry.rendered_hash,
                        rendered_payload=payload,
                    ),
                    harness_id=harness.id,
                )
                storage = _storage_for_kind(storage_provider, kind)
                try:
                    await storage.create(entity)
                except ConflictError as conflict:
                    # Inspect the colliding row: if it's owned by another
                    # harness, surface a cross-harness collision so the
                    # parent can roll back and report apply_id_conflict.
                    existing = await storage.get(entry.resolved_id)
                    existing_harness_id = getattr(existing, "harness_id", None)
                    if (
                        existing_harness_id is not None
                        and existing_harness_id != harness.id
                    ):
                        # Roll back any rows already written in this attempt.
                        for rb_kind, rb_id in reversed(created):
                            try:
                                await _storage_for_kind(
                                    storage_provider, rb_kind,
                                ).delete(rb_id)
                            except Exception:
                                pass
                        return json.dumps({
                            "code": "apply_id_conflict",
                            "message": (
                                f"resolved id {entry.resolved_id!r} already "
                                f"belongs to harness {existing_harness_id!r}"
                            ),
                            "conflicting_id": entry.resolved_id,
                            "existing_harness_id": existing_harness_id,
                        })
                    # Otherwise it's a same-harness or untagged collision —
                    # let the generic apply_failed path handle it.
                    raise conflict
                created.append((kind, entry.resolved_id))
                if kind == "document":
                    created_documents.append(entity)

    except Exception as exc:
        # Best-effort rollback of already-created entities
        for rollback_kind, rollback_id in reversed(created):
            try:
                await _storage_for_kind(storage_provider, rollback_kind).delete(rollback_id)
            except Exception:
                pass
        return json.dumps({"code": "apply_failed", "message": str(exc)})

    # Write the HarnessRendering snapshot
    rendering = HarnessRendering(
        id=harness.id,
        harness_id=harness.id,
        bundle_hash=bundle_hash,
        overrides_hash=overrides_hash,
        schema_hash=schema_hash,
        entries=entries,
        rendered_at=datetime.now(timezone.utc),
    )
    await storage_provider.get_storage(HarnessRendering).create(rendering)

    # Best-effort: index installed documents through the normal pipeline.
    for doc_entity in created_documents:
        await _index_installed_document(
            storage_provider=storage_provider,
            document_entity=doc_entity,
            provider_registry=provider_registry,
            semantic_search_registry=semantic_search_registry,
        )

    return None


# ---------------------------------------------------------------------------
# apply_sync
# ---------------------------------------------------------------------------


async def apply_sync(
    *,
    storage_provider: Any,
    harness: Harness,
    new_entries: list[RenderedEntry],
    rendered_files_by_name: dict[str, RenderedFile],
    bundle_hash: str,
    overrides_hash: str,
    schema_hash: str | None,
    provider_registry: Any | None = None,
    semantic_search_registry: Any | None = None,
) -> str | None:
    """Diff against the stored HarnessRendering, apply, replace snapshot.

    Fast path: if bundle_hash and overrides_hash both match the stored
    rendering, skip all mutations (no-op).

    Per-entity failures are collected; we continue applying the rest. On any
    per-entity failure the rendering snapshot is left untouched (see BUG1
    note below) so the next sync re-runs the diff against the real baseline.

    Created / updated documents are routed through the same chunk/embed/index
    pipeline as the REST flow (best-effort; skipped when registries omitted).
    Returns None on success or a JSON-encoded error string on failure.
    """
    rendering_storage = storage_provider.get_storage(HarnessRendering)
    old_rendering = await rendering_storage.get(harness.id)

    # Fast path: if nothing changed, skip all mutations
    if (
        old_rendering is not None
        and old_rendering.bundle_hash == bundle_hash
        and old_rendering.overrides_hash == overrides_hash
    ):
        return None

    old_entries: list[RenderedEntry] = old_rendering.entries if old_rendering else []
    diff = diff_renderings(old_entries, new_entries)

    apply_errors: list[dict[str, Any]] = []
    indexed_documents: list[Any] = []  # entities to index after a clean apply

    # Process deletes first (reverse uninstall order: graph → ... → toolset)
    for entry in _sorted_by_kind(diff.deletes, _UNINSTALL_ORDER):
        storage = _storage_for_kind(storage_provider, entry.kind)
        try:
            await storage.delete(entry.resolved_id)
        except Exception as exc:
            apply_errors.append(
                {"kind": entry.kind, "id": entry.resolved_id, "error": str(exc)}
            )

    # Process creates (install order: toolset → ... → graph)
    for entry in _sorted_by_kind(diff.creates, _INSTALL_ORDER):
        payload = dict(entry.rendered_payload)
        if entry.kind == "document" and entry.template_name in rendered_files_by_name:
            rf = rendered_files_by_name[entry.template_name]
            if rf.content is not None:
                meta = dict(payload.get("meta") or {})
                meta["content"] = rf.content
                payload = {**payload, "meta": meta}

        entity = _entity_from_entry(
            RenderedEntry(
                kind=entry.kind,
                template_name=entry.template_name,
                resolved_id=entry.resolved_id,
                template_source_hash=entry.template_source_hash,
                rendered_hash=entry.rendered_hash,
                rendered_payload=payload,
            ),
            harness_id=harness.id,
        )
        storage = _storage_for_kind(storage_provider, entry.kind)
        try:
            await storage.create(entity)
        except Exception as exc:
            apply_errors.append(
                {"kind": entry.kind, "id": entry.resolved_id, "error": str(exc)}
            )
        else:
            if entry.kind == "document":
                indexed_documents.append(entity)

    # Process updates
    for _old_entry, new_entry in diff.updates:
        payload = dict(new_entry.rendered_payload)
        if new_entry.kind == "document" and new_entry.template_name in rendered_files_by_name:
            rf = rendered_files_by_name[new_entry.template_name]
            if rf.content is not None:
                meta = dict(payload.get("meta") or {})
                meta["content"] = rf.content
                payload = {**payload, "meta": meta}

        entity = _entity_from_entry(
            RenderedEntry(
                kind=new_entry.kind,
                template_name=new_entry.template_name,
                resolved_id=new_entry.resolved_id,
                template_source_hash=new_entry.template_source_hash,
                rendered_hash=new_entry.rendered_hash,
                rendered_payload=payload,
            ),
            harness_id=harness.id,
        )
        storage = _storage_for_kind(storage_provider, new_entry.kind)
        try:
            await storage.update(entity)
        except Exception as exc:
            apply_errors.append(
                {"kind": new_entry.kind, "id": new_entry.resolved_id, "error": str(exc)}
            )
        else:
            if new_entry.kind == "document":
                indexed_documents.append(entity)

    # On partial apply failure we MUST NOT advance the rendering snapshot.
    # The snapshot is the baseline the next sync diffs against (and the
    # fast-path keys off its bundle_hash); advancing it to the new bundle
    # would make the next sync believe the failed entity was applied and
    # silently skip it forever (permanent drift). Leave the prior snapshot
    # untouched and surface the failure so the harness status reflects ERROR.
    if apply_errors:
        return json.dumps({"code": "partial_apply_failure", "errors": apply_errors})

    # All entities applied: replace the rendering snapshot.
    new_rendering = HarnessRendering(
        id=harness.id,
        harness_id=harness.id,
        bundle_hash=bundle_hash,
        overrides_hash=overrides_hash,
        schema_hash=schema_hash,
        entries=new_entries,
        rendered_at=datetime.now(timezone.utc),
    )
    if old_rendering is not None:
        await rendering_storage.update(new_rendering)
    else:
        await rendering_storage.create(new_rendering)

    # Best-effort: index created/updated documents through the normal pipeline.
    for doc_entity in indexed_documents:
        await _index_installed_document(
            storage_provider=storage_provider,
            document_entity=doc_entity,
            provider_registry=provider_registry,
            semantic_search_registry=semantic_search_registry,
        )

    return None


def _sorted_by_kind(
    entries: list[RenderedEntry],
    order: list[str],
) -> list[RenderedEntry]:
    """Sort entries by the given kind order list."""
    order_index = {k: i for i, k in enumerate(order)}
    return sorted(entries, key=lambda e: order_index.get(e.kind, 999))


# ---------------------------------------------------------------------------
# apply_uninstall
# ---------------------------------------------------------------------------


async def apply_uninstall(
    *,
    storage_provider: Any,
    harness: Harness,
) -> None:
    """Delete every managed entity, then the rendering, then the harness row.

    Order: graph → agent → document → collection → toolset.
    Tolerates "not found" for already-removed entities.
    """
    rendering_storage = storage_provider.get_storage(HarnessRendering)
    rendering = await rendering_storage.get(harness.id)

    if rendering is not None:
        # Sort entries in reverse-DAG order for deletion
        entries_by_kind: dict[str, list[RenderedEntry]] = {}
        for entry in rendering.entries:
            entries_by_kind.setdefault(entry.kind, []).append(entry)

        for kind in _UNINSTALL_ORDER:
            for entry in entries_by_kind.get(kind, []):
                storage = _storage_for_kind(storage_provider, kind)
                try:
                    await storage.delete(entry.resolved_id)
                except Exception:
                    pass  # tolerate "not found"

        # Delete the rendering row
        try:
            await rendering_storage.delete(harness.id)
        except Exception:
            pass

    # Delete the harness row
    harness_storage = storage_provider.get_storage(Harness)
    try:
        await harness_storage.delete(harness.id)
    except Exception:
        pass


__all__ = [
    "BuildErrors",
    "apply_install",
    "apply_sync",
    "apply_uninstall",
    "build_rendered_entries",
    "resolved_id",
]

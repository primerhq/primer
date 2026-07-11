"""Worker-side harness-operation dispatch.

One ``run_one_harness_operation`` invocation per claimed HarnessLease.
The worker pool's harness claim loop creates these as background tasks;
each task reads the Harness row, runs the pending operation, releases
the claim, and publishes a ``harness:{id}:done`` event.

On lease loss the pool cancels this task; the engine fences the lease
release so no stale writes land.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import jsonschema
import jsonschema.exceptions

from primer.harness.dependencies import (
    CanonicalKey,
    DependencyCycleError,
    DependencyVersionConflictError,
    canonical_key,
    walk_dependencies,
)
from primer.harness.git import (
    HarnessGitError,
    _redact,
    clone_at_ref,
    fetch_harness_metadata,
    ls_remote,
    push_bundle,
)
from primer.harness.hashes import (
    hash_bundle,
    hash_overrides,
    hash_rendered_payload,
    hash_schema,
    hash_template_source,
)
from primer.harness.outbound import (
    BuildResult,
    OutboundBuildError,
    build_outbound,
)
from primer.harness.service import (
    BuildErrors,
    apply_install,
    apply_sync,
    apply_uninstall,
    build_rendered_entries,
)
from primer.harness.template import (
    HarnessTemplateError,
    RenderedFile,
    compose_overrides_schema,
    render_bundle,
    slice_overrides_for_dep,
)
from primer.int.event_bus import EventBus
from primer.int.storage_provider import StorageProvider
from primer.model.harness import (
    Harness,
    HarnessDirection,
    HarnessOperation,
    HarnessRendering,
    HarnessStatus,
    RenderedEntry,
    ResolvedDependency,
)
from primer.model.storage import OffsetPage


logger = logging.getLogger(__name__)


def _collect_bundle_files(base: Path) -> list[tuple[str, bytes]]:
    """Walk ``base`` and return (relative_path, bytes) for every non-.git file."""
    result: list[tuple[str, bytes]] = []
    for p in sorted(base.rglob("*")):
        # Skip the .git directory and everything inside it
        parts = p.relative_to(base).parts
        if parts and parts[0] == ".git":
            continue
        if p.is_file():
            rel = str(p.relative_to(base))
            result.append((rel, p.read_bytes()))
    return result

HARNESS_HEARTBEAT_INTERVAL_SECONDS = 10.0

# harness.yaml validation constants
_EXPECTED_API_VERSION = "primer/v1"
_EXPECTED_KIND = "Harness"


def _safe_error_message(exc: Exception, token: str | None) -> str:
    """Defence in depth: any third-party exception text routed into
    ``last_operation_error`` is passed through the token redactor."""
    return _redact(str(exc), token)


_SLUG_NON_ALNUM_RE = re.compile(r"[^a-z0-9-]+")
_SLUG_DASH_COLLAPSE_RE = re.compile(r"-+")


def _slugify(name: str) -> str:
    """Lower-case, replace non-[a-z0-9-] with '-', collapse repeats, strip edges.

    Used to derive a sub-harness slug from its ``metadata.name`` when the
    sub does not provide an explicit ``metadata.slug``. Must satisfy the
    Harness slug regex ``[a-z][a-z0-9-]{1,63}``; callers must validate.
    """
    if not isinstance(name, str):
        return ""
    s = name.strip().lower()
    s = _SLUG_NON_ALNUM_RE.sub("-", s)
    s = _SLUG_DASH_COLLAPSE_RE.sub("-", s)
    return s.strip("-")


@dataclass
class HarnessDispatchDeps:
    """Bundle of runtime dependencies the worker injects per task."""

    storage_provider: StorageProvider
    event_bus: EventBus
    provider_registry: Any | None = None  # may be None in pure-storage tests
    semantic_search_registry: Any | None = None  # may be None in pure-storage tests


# ---------------------------------------------------------------------------
# Public entrypoints
# ---------------------------------------------------------------------------


async def run_one_harness_operation(
    deps: HarnessDispatchDeps,
    *,
    harness_id: str,
    worker_id: str,
) -> None:
    """Dispatch entrypoint. Branches on harness.pending_operation.

    The harness row MUST already have ``pending_operation`` set and the
    engine lease must be held by the worker pool — the pool's claim loop
    has already done that atomically via the ClaimEngine.

    Publishes ``harness:{id}:done`` on completion (success or error).
    """
    harness_storage = deps.storage_provider.get_storage(Harness)

    harness = await harness_storage.get(harness_id)
    if harness is None:
        logger.warning("harness %s vanished before dispatch", harness_id)
        return

    operation = harness.pending_operation
    if operation is None:
        logger.warning(
            "harness %s has no pending_operation — releasing", harness_id
        )
        await _release_harness(
            harness_storage, harness_id, worker_id,
            next_status=harness.status,
        )
        return

    # Direction guard: outbound rows run BUILD/PUSH; inbound rows run
    # FETCH/INSTALL/SYNC. UNINSTALL (delete the harness) is allowed on BOTH
    # directions — a harness can always be deleted, and for an outbound
    # harness a non-cascade uninstall simply removes the harness itself.
    # Mismatches release with a clear error so the user sees what went wrong
    # rather than a cryptic stack from a downstream helper.
    _outbound_ops = {HarnessOperation.BUILD, HarnessOperation.PUSH}
    _inbound_ops = {
        HarnessOperation.FETCH,
        HarnessOperation.INSTALL,
        HarnessOperation.SYNC,
    }
    if (
        harness.direction == HarnessDirection.INBOUND
        and operation in _outbound_ops
    ) or (
        harness.direction == HarnessDirection.OUTBOUND
        and operation in _inbound_ops
    ):
        logger.warning(
            "harness %s direction=%s incompatible with operation %s — releasing as ERROR",
            harness_id, harness.direction.value, operation.value,
        )
        error_json = json.dumps({
            "code": "direction_mismatch",
            "message": (
                f"operation {operation.value!r} not allowed on "
                f"{harness.direction.value} harness"
            ),
            "operation": operation.value,
        })
        await _release_harness(
            harness_storage, harness_id, worker_id,
            next_status=HarnessStatus.ERROR,
            last_operation_error=error_json,
        )
        await deps.event_bus.publish(
            f"harness:{harness_id}:done", {"harness_id": harness_id},
        )
        return

    try:
        if operation == HarnessOperation.FETCH:
            next_status, error_json = await _do_fetch(deps, harness)
        elif operation == HarnessOperation.INSTALL:
            next_status, error_json = await _do_install(deps, harness)
        elif operation == HarnessOperation.SYNC:
            next_status, error_json = await _do_sync(deps, harness)
        elif operation == HarnessOperation.UNINSTALL:
            await _do_uninstall(deps, harness)
            # Row is gone, skip release, just publish done.
            await deps.event_bus.publish(
                f"harness:{harness_id}:done", {"harness_id": harness_id},
            )
            return
        elif operation == HarnessOperation.BUILD:
            next_status, error_json = await _do_build(deps, harness)
        elif operation == HarnessOperation.PUSH:
            next_status, error_json = await _do_push(deps, harness)
        else:
            logger.error(
                "harness %s unknown operation %r, releasing as error",
                harness_id, operation,
            )
            next_status = HarnessStatus.ERROR
            error_json = json.dumps(
                {"code": "unknown_operation", "message": str(operation)}
            )
    except Exception as exc:
        # Defence in depth: each `_do_*` is supposed to catch its own
        # exceptions and return (ERROR, error_json). If one slips
        # through, we'd leak the claim to the sweeper's 90s window.
        # Catch here so release_harness always runs.
        logger.exception(
            "harness %s operation %r raised uncaught, releasing as ERROR",
            harness_id, operation,
        )
        next_status = HarnessStatus.ERROR
        error_json = json.dumps(
            {"code": "dispatch_unhandled", "message": str(exc)}
        )

    await _release_harness(
        harness_storage,
        harness_id,
        worker_id,
        next_status=next_status,
        last_operation_error=error_json,
    )

    await deps.event_bus.publish(
        f"harness:{harness_id}:done", {"harness_id": harness_id},
    )


async def sweep_harnesses(
    deps: HarnessDispatchDeps,
    *,
    heartbeat_stale_after: timedelta = timedelta(seconds=90),
) -> int:
    """Legacy sweeper — now a no-op.

    Lease-based heartbeating is handled by the ClaimEngine; the pool's
    heartbeat loop detects lost leases and signals the dispatch task
    directly. This function is retained for API compatibility but no
    longer inspects or mutates harness rows.
    """
    return 0


# ---------------------------------------------------------------------------
# Claim release / heartbeat helpers (direct storage operations)
# ---------------------------------------------------------------------------


async def _release_harness(
    harness_storage,
    harness_id: str,
    worker_id: str,
    *,
    next_status: HarnessStatus,
    last_operation_error: str | None = None,
) -> None:
    """Release the harness operation by updating storage directly.

    Sets ``status=next_status``, clears ``pending_operation``.
    Silently no-ops if the harness row is missing.
    """
    harness = await harness_storage.get(harness_id)
    if harness is None:
        return
    updated = harness.model_copy(update={
        "status": next_status,
        "pending_operation": None,
        "last_operation_error": last_operation_error,
        "last_operation_at": datetime.now(timezone.utc),
    })
    await harness_storage.update(updated)


# ---------------------------------------------------------------------------
# _do_fetch
# ---------------------------------------------------------------------------


async def _do_fetch(
    deps: HarnessDispatchDeps,
    harness: Harness,
) -> tuple[HarnessStatus, str | None]:
    """Resolve ref → commit, clone, read harness.yaml + schema, compute hashes.

    Returns (next_status, error_json | None).
    """
    harness_storage = deps.storage_provider.get_storage(Harness)

    token = harness.git_token.get_secret_value() if harness.git_token else None

    try:
        available_commit = await ls_remote(harness.git_url, token=token, ref=harness.ref)
    except HarnessGitError as exc:
        return HarnessStatus.ERROR, json.dumps(
            {"code": exc.code, "message": exc.message}
        )
    except Exception as exc:
        return HarnessStatus.ERROR, json.dumps(
            {"code": "git_clone_failed", "message": _safe_error_message(exc, token)}
        )

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            dest = str(Path(tmpdir) / "checkout")
            try:
                await clone_at_ref(
                    harness.git_url,
                    token=token,
                    ref=available_commit,
                    dest=dest,
                )
            except HarnessGitError as exc:
                return HarnessStatus.ERROR, json.dumps(
                    {"code": exc.code, "message": exc.message}
                )
            except Exception as exc:
                return HarnessStatus.ERROR, json.dumps(
                    {"code": "git_clone_failed", "message": _safe_error_message(exc, token)}
                )

            # Resolve base path
            base = Path(dest)
            if harness.subpath:
                base = base / harness.subpath

            # Read harness.yaml
            harness_yaml_path = base / "harness.yaml"
            if not harness_yaml_path.is_file():
                return HarnessStatus.ERROR, json.dumps(
                    {"code": "harness_yaml_missing",
                     "message": "harness.yaml not found at subpath"}
                )
            try:
                import yaml
                _yaml_text = await asyncio.to_thread(harness_yaml_path.read_text)
                harness_meta = await asyncio.to_thread(yaml.safe_load, _yaml_text)
            except Exception as exc:
                return HarnessStatus.ERROR, json.dumps(
                    {"code": "harness_yaml_invalid", "message": _safe_error_message(exc, token)}
                )
            if not isinstance(harness_meta, dict):
                return HarnessStatus.ERROR, json.dumps(
                    {"code": "harness_yaml_invalid",
                     "message": "harness.yaml must be a YAML mapping"}
                )
            api_version = harness_meta.get("apiVersion")
            kind = harness_meta.get("kind")
            if api_version != _EXPECTED_API_VERSION or kind != _EXPECTED_KIND:
                return HarnessStatus.ERROR, json.dumps(
                    {"code": "harness_yaml_invalid",
                     "message": (
                         f"harness.yaml must have apiVersion={_EXPECTED_API_VERSION!r} "
                         f"and kind={_EXPECTED_KIND!r}; "
                         f"got apiVersion={api_version!r} kind={kind!r}"
                     )}
                )

            # Read overrides.schema.json
            schema_path = base / "overrides.schema.json"
            parent_overrides_schema: dict[str, Any] | None = None
            if schema_path.is_file():
                try:
                    import json as _json
                    _schema_text = await asyncio.to_thread(schema_path.read_text)
                    parent_overrides_schema = await asyncio.to_thread(
                        _json.loads, _schema_text,
                    )
                except Exception as exc:
                    return HarnessStatus.ERROR, json.dumps(
                        {"code": "harness_yaml_invalid",
                         "message": f"overrides.schema.json: {exc}"}
                    )

            # Compute parent's own bundle_hash over the subpath (excluding .git)
            bundle_files = await asyncio.to_thread(_collect_bundle_files, base)
            parent_bundle_hash = hash_bundle(bundle_files)

            # ---- Dependency walk (optional) -----------------------------
            parent_deps = harness_meta.get("dependencies") or []
            if not isinstance(parent_deps, list):
                return HarnessStatus.ERROR, json.dumps(
                    {"code": "harness_yaml_invalid",
                     "message": "dependencies must be a list"}
                )

            resolved_deps: list[ResolvedDependency] = []
            # Side-channel populated by the fetcher closure so we can recover
            # each sub's overrides.schema.json after walk_dependencies returns.
            schemas_by_key: dict[CanonicalKey, dict[str, Any]] = {}
            # Track the most recent (url, ref, subpath, name) being fetched so
            # we can surface meaningful error details when a sub-fetch fails.
            current_fetch_target: dict[str, Any] = {}

            async def _fetch_meta(
                url: str, ref: str, subpath: str | None, tok: str | None,
            ) -> tuple[str, list[dict], str, str]:
                current_fetch_target.update(
                    {"git_url": url, "ref": ref, "subpath": subpath},
                )
                hy, sch, sub_bundle_hash, sub_commit = await fetch_harness_metadata(
                    git_url=url, ref=ref, subpath=subpath, token=tok,
                )
                meta = hy.get("metadata") if isinstance(hy.get("metadata"), dict) else {}
                slug_raw = meta.get("slug") if isinstance(meta, dict) else None
                if not slug_raw:
                    slug_raw = _slugify(meta.get("name", "") if isinstance(meta, dict) else "")
                if not slug_raw:
                    # Fall back to slugifying any top-level name field too.
                    slug_raw = _slugify(hy.get("name", "") if isinstance(hy.get("name"), str) else "")
                if not slug_raw or not re.match(r"^[a-z][a-z0-9-]{1,63}$", slug_raw):
                    raise HarnessGitError(
                        "dependency_yaml_invalid",
                        "sub harness.yaml must declare metadata.name or metadata.slug",
                    )
                sub_deps = hy.get("dependencies") or []
                if not isinstance(sub_deps, list):
                    raise HarnessGitError(
                        "dependency_yaml_invalid",
                        "sub harness.yaml: dependencies must be a list",
                    )
                key = canonical_key(url, ref, subpath)
                if isinstance(sch, dict):
                    schemas_by_key[key] = sch
                return slug_raw, sub_deps, sub_bundle_hash, sub_commit

            if parent_deps:
                try:
                    resolved_deps, _visited_by_key = await walk_dependencies(
                        parent_deps=parent_deps,
                        fetcher=_fetch_meta,
                    )
                except DependencyCycleError as exc:
                    return HarnessStatus.ERROR, json.dumps(
                        {"code": "dependency_cycle",
                         "message": str(exc),
                         "path": exc.path}
                    )
                except DependencyVersionConflictError as exc:
                    return HarnessStatus.ERROR, json.dumps(
                        {"code": "dependency_version_conflict",
                         "message": str(exc),
                         "slug": exc.slug,
                         "ref_a": exc.ref_a,
                         "ref_b": exc.ref_b,
                         "path_a": exc.path_a,
                         "path_b": exc.path_b}
                    )
                except HarnessGitError as exc:
                    if exc.code == "dependency_yaml_invalid":
                        return HarnessStatus.ERROR, json.dumps(
                            {"code": "dependency_yaml_invalid",
                             "message": exc.message,
                             "git_url": current_fetch_target.get("git_url"),
                             "ref": current_fetch_target.get("ref")}
                        )
                    return HarnessStatus.ERROR, json.dumps(
                        {"code": "dependency_fetch_failed",
                         "message": exc.message,
                         "git_url": current_fetch_target.get("git_url"),
                         "ref": current_fetch_target.get("ref"),
                         "inner_code": exc.code}
                    )

            # ---- Compose overrides schema --------------------------------
            # Build (dep.name, sub_schema) pairs for DIRECT deps only; the
            # walker preserves declaration order for direct entries via the
            # post-order list with depth==0 trailing each subtree.
            direct_sub_schemas: list[tuple[str, dict[str, Any]]] = []
            for dep_decl in parent_deps:
                name = dep_decl.get("name")
                url = dep_decl.get("git_url")
                ref_ = dep_decl.get("ref", "main")
                subpath_ = dep_decl.get("subpath")
                if not isinstance(name, str) or not isinstance(url, str):
                    continue
                key = canonical_key(url, ref_, subpath_)
                sub_schema = schemas_by_key.get(key, {"type": "object", "properties": {}})
                direct_sub_schemas.append((name, sub_schema))

            base_schema_for_compose = parent_overrides_schema or {"type": "object", "properties": {}}
            if direct_sub_schemas:
                overrides_schema: dict[str, Any] | None = compose_overrides_schema(
                    parent_schema=base_schema_for_compose,
                    sub_schemas=direct_sub_schemas,
                )
            else:
                overrides_schema = parent_overrides_schema
            new_schema_hash = hash_schema(overrides_schema) if overrides_schema is not None else None

            # ---- Composite available_bundle_hash -------------------------
            # parent's own bundle hash bytes + each dep's bundle_hash + \0
            # in canonical-key order (deterministic across runs).
            if resolved_deps:
                h = hashlib.sha256()
                h.update(parent_bundle_hash.encode("utf-8"))
                ordered = sorted(
                    resolved_deps,
                    key=lambda d: (
                        canonical_key(d.git_url, d.ref, d.subpath).url,
                        canonical_key(d.git_url, d.ref, d.subpath).ref,
                        canonical_key(d.git_url, d.ref, d.subpath).subpath,
                    ),
                )
                for d in ordered:
                    h.update(d.bundle_hash.encode("utf-8"))
                    h.update(b"\x00")
                new_available_bundle_hash = h.hexdigest()
            else:
                new_available_bundle_hash = parent_bundle_hash

            # Re-validate current overrides against composite schema
            schema_missing_input = False
            if overrides_schema is not None:
                try:
                    jsonschema.validate(
                        instance=harness.overrides,
                        schema=overrides_schema,
                    )
                except jsonschema.exceptions.ValidationError:
                    # Check if it's due to a required field
                    schema_missing_input = True
                except Exception:
                    pass

            # Compute outdated flags
            commits_ahead = (
                harness.resolved_commit is None
                or available_commit != harness.resolved_commit
                or new_available_bundle_hash != harness.bundle_hash
            )
            new_overrides_hash = hash_overrides(harness.overrides)
            overrides_dirty = new_overrides_hash != (harness.overrides_hash or "")

            # Determine next status
            if harness.status == HarnessStatus.DRAFT:
                next_status = HarnessStatus.READY
            elif commits_ahead or overrides_dirty or schema_missing_input:
                next_status = HarnessStatus.OUTDATED
            else:
                next_status = HarnessStatus.INSTALLED

            # Update harness row (do it directly in storage, not through release)
            refreshed = await harness_storage.get(harness.id)
            if refreshed is None:
                return HarnessStatus.ERROR, json.dumps(
                    {"code": "harness_not_found",
                     "message": "harness row disappeared"}
                )
            updated = refreshed.model_copy(update={
                "available_commit": available_commit,
                "available_bundle_hash": new_available_bundle_hash,
                "overrides_schema": overrides_schema,
                "schema_hash": new_schema_hash,
                "overrides_hash": new_overrides_hash,
                "commits_ahead": commits_ahead,
                "overrides_dirty": overrides_dirty,
                "schema_missing_input": schema_missing_input,
                "dependencies_resolved": resolved_deps,
            })
            await harness_storage.update(updated)

            return next_status, None

    except Exception as exc:
        logger.exception("_do_fetch unhandled error for harness %s", harness.id)
        return HarnessStatus.ERROR, json.dumps(
            {"code": "fetch_failed", "message": _safe_error_message(exc, token)}
        )


# ---------------------------------------------------------------------------
# Subharness rendering helpers (Spec A §8)
# ---------------------------------------------------------------------------


def _dep_path_for(
    dep: ResolvedDependency,
    all_deps: list[ResolvedDependency],
) -> str:
    """Walk parent_name links back to the root and return a path of dep-names.

    e.g. for a chain root → "docs" → "embeddings" the deepest dep gets
    "docs/embeddings"; a direct (depth-0) dep gets just "docs".
    """
    by_name: dict[str, ResolvedDependency] = {d.name: d for d in all_deps}
    chain: list[str] = []
    cursor: ResolvedDependency | None = dep
    visited: set[str] = set()
    while cursor is not None:
        if cursor.name in visited:
            break
        visited.add(cursor.name)
        chain.append(cursor.name)
        if cursor.parent_name is None:
            break
        cursor = by_name.get(cursor.parent_name)
    return "/".join(reversed(chain))


def _slice_overrides_along_path(
    parent_overrides: dict[str, Any],
    dep: ResolvedDependency,
    all_deps: list[ResolvedDependency],
) -> dict[str, Any]:
    """Chain ``slice_overrides_for_dep`` down the dep-name path.

    For a direct dep ("docs") this just returns
    ``slice_overrides_for_dep(parent_overrides, "docs")``.

    For a nested dep ("docs/embeddings") this walks
    parent_overrides["dependencies"]["docs"]["dependencies"]["embeddings"].
    """
    path = _dep_path_for(dep, all_deps).split("/")
    cursor: dict[str, Any] = parent_overrides
    for segment in path:
        cursor = slice_overrides_for_dep(cursor, segment)
        if not cursor:
            return {}
    return cursor


async def _render_sub_bundle(
    *,
    dep: ResolvedDependency,
    token: str | None,
    overrides: dict[str, Any],
    dep_path: str,
) -> list[RenderedFile]:
    """Clone a sub at its resolved_commit + render its templates/.

    Each returned file is tagged with ``source_slug=dep.slug`` and
    ``source_dependency=dep_path``.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        dest = str(Path(tmpdir) / "sub")
        await clone_at_ref(
            dep.git_url,
            token=token,
            ref=dep.resolved_commit,
            dest=dest,
        )
        sub_ctx = {
            "slug": dep.slug,
            "name": dep.name,
            "description": "",
        }
        sub_files = await render_bundle(
            checkout_dir=dest,
            subpath=dep.subpath,
            overrides=overrides,
            harness_ctx=sub_ctx,
        )
        for f in sub_files:
            f.source_slug = dep.slug
            f.source_dependency = dep_path
        return sub_files


# ---------------------------------------------------------------------------
# _do_install
# ---------------------------------------------------------------------------


async def _do_install(
    deps: HarnessDispatchDeps,
    harness: Harness,
) -> tuple[HarnessStatus, str | None]:
    """Clone at available_commit, render + validate, apply_install.

    Returns (next_status, error_json | None).
    """
    token = harness.git_token.get_secret_value() if harness.git_token else None

    # Pre-flight: validate overrides against schema
    if harness.overrides_schema is not None:
        try:
            jsonschema.validate(
                instance=harness.overrides,
                schema=harness.overrides_schema,
            )
        except jsonschema.exceptions.ValidationError as exc:
            return HarnessStatus.ERROR, json.dumps(
                {"code": "overrides_invalid",
                 "message": exc.message,
                 "path": list(exc.absolute_path)}
            )

    if harness.available_commit is None:
        return HarnessStatus.ERROR, json.dumps(
            {"code": "fetch_required",
             "message": "fetch must be run before install"}
        )

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            dest = str(Path(tmpdir) / "checkout")
            try:
                await clone_at_ref(
                    harness.git_url,
                    token=token,
                    ref=harness.available_commit,
                    dest=dest,
                )
            except HarnessGitError as exc:
                return HarnessStatus.ERROR, json.dumps(
                    {"code": exc.code, "message": exc.message}
                )

            # Verify bundle hash (excluding .git). When dependencies are
            # present, the parent's available_bundle_hash is the COMPOSITE
            # hash computed in _do_fetch; we recompute it here from
            # parent_bundle + each dep's stored bundle_hash (deterministic
            # canonical-key order) so we can detect tampering.
            base = Path(dest)
            if harness.subpath:
                base = base / harness.subpath
            parent_bundle_hash = hash_bundle(
                await asyncio.to_thread(_collect_bundle_files, base),
            )
            if harness.dependencies_resolved:
                h = hashlib.sha256()
                h.update(parent_bundle_hash.encode("utf-8"))
                ordered = sorted(
                    harness.dependencies_resolved,
                    key=lambda d: (
                        canonical_key(d.git_url, d.ref, d.subpath).url,
                        canonical_key(d.git_url, d.ref, d.subpath).ref,
                        canonical_key(d.git_url, d.ref, d.subpath).subpath,
                    ),
                )
                for d in ordered:
                    h.update(d.bundle_hash.encode("utf-8"))
                    h.update(b"\x00")
                current_bundle_hash = h.hexdigest()
            else:
                current_bundle_hash = parent_bundle_hash
            if (
                harness.available_bundle_hash is not None
                and current_bundle_hash != harness.available_bundle_hash
            ):
                return HarnessStatus.ERROR, json.dumps(
                    {"code": "bundle_hash_mismatch",
                     "message": "bundle changed since fetch; re-run fetch"}
                )

            # Render the parent's bundle. Each parent file gets tagged with
            # the parent's slug + source_dependency=None so the multi-slug
            # rewrite map in service.build_rendered_entries can scope cross-
            # refs correctly even when subs are present.
            harness_ctx = {"slug": harness.slug, "name": harness.name,
                           "description": harness.description}
            try:
                parent_rendered = await render_bundle(
                    checkout_dir=dest,
                    subpath=harness.subpath,
                    overrides=harness.overrides,
                    harness_ctx=harness_ctx,
                )
            except HarnessTemplateError as exc:
                return HarnessStatus.ERROR, json.dumps(
                    {"code": exc.code, "message": exc.message}
                )
            for f in parent_rendered:
                f.source_slug = harness.slug
                f.source_dependency = None

            # ---- Render subharness bundles ------------------------------
            # ``dependencies_resolved`` is post-order (deepest first); we
            # render in that same order and concatenate so apply_install
            # writes deeper subs before parents. Each sub gets its own
            # tmp clone at its resolved_commit.
            sub_rendered_concat: list[RenderedFile] = []
            try:
                for dep in harness.dependencies_resolved:
                    dep_overrides = _slice_overrides_along_path(
                        harness.overrides, dep, harness.dependencies_resolved,
                    )
                    dep_path = _dep_path_for(dep, harness.dependencies_resolved)
                    sub_files = await _render_sub_bundle(
                        dep=dep,
                        token=token,
                        overrides=dep_overrides,
                        dep_path=dep_path,
                    )
                    sub_rendered_concat.extend(sub_files)
            except HarnessTemplateError as exc:
                return HarnessStatus.ERROR, json.dumps(
                    {"code": exc.code, "message": exc.message,
                     "source_dependency": getattr(exc, "template", None)}
                )
            except HarnessGitError as exc:
                return HarnessStatus.ERROR, json.dumps(
                    {"code": "dependency_fetch_failed", "message": exc.message,
                     "inner_code": exc.code}
                )

            # Combine — subs first (deepest-first by post-order), then parent.
            rendered = sub_rendered_concat + parent_rendered

            # Build + validate entries. The parent's slug is the fallback
            # for any file that lacks ``source_slug`` (none here, but the
            # contract is honoured for safety).
            entries, build_errors = build_rendered_entries(rendered, slug=harness.slug)
            if build_errors:
                return HarnessStatus.ERROR, json.dumps(
                    {"code": "build_errors", "errors": build_errors.errors}
                )

            # Build rendered_files_by_name (kept template_name-keyed for
            # document-content lookups). When multiple subs declare the
            # same template_name a later entry would overwrite an earlier
            # one in this dict, but document content lookup matches by
            # template_name anyway — keep the parent's last to mirror the
            # entries order (deepest-first then parent).
            rendered_files_by_name: dict[str, RenderedFile] = {
                f.template_name: f for f in rendered
            }

            overrides_hash = hash_overrides(harness.overrides)

            error = await apply_install(
                storage_provider=deps.storage_provider,
                harness=harness,
                entries=entries,
                rendered_files_by_name=rendered_files_by_name,
                bundle_hash=current_bundle_hash,
                overrides_hash=overrides_hash,
                schema_hash=harness.schema_hash,
                provider_registry=deps.provider_registry,
                semantic_search_registry=deps.semantic_search_registry,
            )
            if error is not None:
                return HarnessStatus.ERROR, error

            # Update harness row fields (release_harness sets status)
            harness_storage = deps.storage_provider.get_storage(Harness)
            refreshed = await harness_storage.get(harness.id)
            if refreshed is not None:
                updated = refreshed.model_copy(update={
                    "bundle_hash": current_bundle_hash,
                    "resolved_commit": harness.available_commit,
                    "commits_ahead": False,
                    "overrides_dirty": False,
                    "schema_missing_input": False,
                    "overrides_hash": overrides_hash,
                })
                await harness_storage.update(updated)

            return HarnessStatus.INSTALLED, None

    except Exception as exc:
        logger.exception("_do_install unhandled error for harness %s", harness.id)
        return HarnessStatus.ERROR, json.dumps(
            {"code": "install_failed", "message": _safe_error_message(exc, token)}
        )


# ---------------------------------------------------------------------------
# _do_sync
# ---------------------------------------------------------------------------


async def _do_sync(
    deps: HarnessDispatchDeps,
    harness: Harness,
) -> tuple[HarnessStatus, str | None]:
    """Diff old rendering vs new bundle, apply changes.

    Fast path: if available_bundle_hash == bundle_hash AND overrides unchanged,
    just refresh resolved_commit.

    Returns (next_status, error_json | None).
    """
    token = harness.git_token.get_secret_value() if harness.git_token else None

    overrides_hash = hash_overrides(harness.overrides)

    # Check rendering storage for the current hash
    rendering_storage = deps.storage_provider.get_storage(HarnessRendering)
    old_rendering = await rendering_storage.get(harness.id)
    stored_overrides_hash = old_rendering.overrides_hash if old_rendering else None

    # Fast path: nothing changed
    if (
        harness.available_bundle_hash is not None
        and harness.available_bundle_hash == harness.bundle_hash
        and overrides_hash == stored_overrides_hash
    ):
        # Just update resolved_commit
        harness_storage = deps.storage_provider.get_storage(Harness)
        refreshed = await harness_storage.get(harness.id)
        if refreshed is not None and harness.available_commit is not None:
            updated = refreshed.model_copy(update={
                "resolved_commit": harness.available_commit,
                "commits_ahead": False,
                "overrides_dirty": False,
                "schema_missing_input": False,
            })
            await harness_storage.update(updated)
        return HarnessStatus.INSTALLED, None

    if harness.available_commit is None:
        return HarnessStatus.ERROR, json.dumps(
            {"code": "fetch_required",
             "message": "fetch must be run before sync"}
        )

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            dest = str(Path(tmpdir) / "checkout")
            try:
                await clone_at_ref(
                    harness.git_url,
                    token=token,
                    ref=harness.available_commit,
                    dest=dest,
                )
            except HarnessGitError as exc:
                return HarnessStatus.ERROR, json.dumps(
                    {"code": exc.code, "message": exc.message}
                )

            # Compute the parent's own bundle hash, then fold in each
            # dep's bundle_hash for the composite (matches _do_install
            # and the available_bundle_hash computed during fetch).
            base = Path(dest)
            if harness.subpath:
                base = base / harness.subpath
            parent_bundle_hash = hash_bundle(
                await asyncio.to_thread(_collect_bundle_files, base),
            )
            if harness.dependencies_resolved:
                h = hashlib.sha256()
                h.update(parent_bundle_hash.encode("utf-8"))
                ordered = sorted(
                    harness.dependencies_resolved,
                    key=lambda d: (
                        canonical_key(d.git_url, d.ref, d.subpath).url,
                        canonical_key(d.git_url, d.ref, d.subpath).ref,
                        canonical_key(d.git_url, d.ref, d.subpath).subpath,
                    ),
                )
                for d in ordered:
                    h.update(d.bundle_hash.encode("utf-8"))
                    h.update(b"\x00")
                current_bundle_hash = h.hexdigest()
            else:
                current_bundle_hash = parent_bundle_hash

            # Render the parent's bundle. Tag every parent file with the
            # parent slug so the multi-slug rewrite map can scope cross-
            # refs correctly even when subs are present.
            harness_ctx = {"slug": harness.slug, "name": harness.name,
                           "description": harness.description}
            try:
                parent_rendered = await render_bundle(
                    checkout_dir=dest,
                    subpath=harness.subpath,
                    overrides=harness.overrides,
                    harness_ctx=harness_ctx,
                )
            except HarnessTemplateError as exc:
                return HarnessStatus.ERROR, json.dumps(
                    {"code": exc.code, "message": exc.message}
                )
            for f in parent_rendered:
                f.source_slug = harness.slug
                f.source_dependency = None

            # Render every subharness bundle in post-order (deepest first).
            # Mirrors _do_install so the 3-way diff sees the full set of
            # entries; otherwise sub entries from the previous install
            # would be diff-deleted and the sub's entities removed.
            sub_rendered_concat: list[RenderedFile] = []
            try:
                for dep in harness.dependencies_resolved:
                    dep_overrides = _slice_overrides_along_path(
                        harness.overrides, dep, harness.dependencies_resolved,
                    )
                    dep_path = _dep_path_for(dep, harness.dependencies_resolved)
                    sub_files = await _render_sub_bundle(
                        dep=dep,
                        token=token,
                        overrides=dep_overrides,
                        dep_path=dep_path,
                    )
                    sub_rendered_concat.extend(sub_files)
            except HarnessTemplateError as exc:
                return HarnessStatus.ERROR, json.dumps(
                    {"code": exc.code, "message": exc.message,
                     "source_dependency": getattr(exc, "template", None)}
                )
            except HarnessGitError as exc:
                return HarnessStatus.ERROR, json.dumps(
                    {"code": "dependency_fetch_failed", "message": exc.message,
                     "inner_code": exc.code}
                )

            rendered = sub_rendered_concat + parent_rendered

            entries, build_errors = build_rendered_entries(rendered, slug=harness.slug)
            if build_errors:
                return HarnessStatus.ERROR, json.dumps(
                    {"code": "build_errors", "errors": build_errors.errors}
                )

            rendered_files_by_name: dict[str, RenderedFile] = {
                f.template_name: f for f in rendered
            }

            error = await apply_sync(
                storage_provider=deps.storage_provider,
                harness=harness,
                new_entries=entries,
                rendered_files_by_name=rendered_files_by_name,
                bundle_hash=current_bundle_hash,
                overrides_hash=overrides_hash,
                schema_hash=harness.schema_hash,
                provider_registry=deps.provider_registry,
                semantic_search_registry=deps.semantic_search_registry,
            )

            # Only stamp bundle_hash / resolved_commit when the apply
            # was clean. On partial failure we keep the old bundle_hash
            # so the next sync's fast-path does NOT skip — it'll re-run
            # the diff against the actual deployed state.
            harness_storage = deps.storage_provider.get_storage(Harness)
            refreshed = await harness_storage.get(harness.id)
            if refreshed is not None:
                update_fields: dict[str, Any] = {
                    "overrides_hash": overrides_hash,
                }
                if error is None:
                    update_fields.update({
                        "bundle_hash": current_bundle_hash,
                        "resolved_commit": harness.available_commit,
                        "commits_ahead": False,
                        "overrides_dirty": False,
                        "schema_missing_input": False,
                    })
                updated = refreshed.model_copy(update=update_fields)
                await harness_storage.update(updated)

            if error is not None:
                return HarnessStatus.ERROR, error

            return HarnessStatus.INSTALLED, None

    except Exception as exc:
        logger.exception("_do_sync unhandled error for harness %s", harness.id)
        return HarnessStatus.ERROR, json.dumps(
            {"code": "sync_failed", "message": _safe_error_message(exc, token)}
        )


# ---------------------------------------------------------------------------
# _do_uninstall
# ---------------------------------------------------------------------------


async def _do_uninstall(
    deps: HarnessDispatchDeps,
    harness: Harness,
) -> None:
    """Delete all managed entities, rendering, and harness row.

    Caller must NOT call release_harness after this (row is gone).
    """
    try:
        await apply_uninstall(
            storage_provider=deps.storage_provider,
            harness=harness,
            cascade=harness.uninstall_cascade,
        )
    except Exception:
        logger.exception("_do_uninstall error for harness %s", harness.id)


# ---------------------------------------------------------------------------
# _do_build (outbound)
# ---------------------------------------------------------------------------


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


async def _do_build(
    deps: HarnessDispatchDeps,
    harness: Harness,
) -> tuple[HarnessStatus, str | None]:
    """Render tracked entities → templates, persist bundle hash + rendering.

    Decides next status based on whether the harness has been pushed
    before and whether the freshly-built ``bundle_hash`` matches the
    last-pushed one (drift detection).
    """
    try:
        result: BuildResult = await build_outbound(
            harness, storage_provider=deps.storage_provider,
        )
    except OutboundBuildError as exc:
        err: dict[str, Any] = {"code": exc.code, "message": exc.message}
        if exc.template_name:
            err["template_name"] = exc.template_name
        return HarnessStatus.ERROR, json.dumps(err)
    except Exception as exc:
        logger.exception("_do_build unhandled error for harness %s", harness.id)
        return HarnessStatus.ERROR, json.dumps(
            {"code": "build_failed", "message": str(exc)}
        )

    harness_storage = deps.storage_provider.get_storage(Harness)
    rendering_storage = deps.storage_provider.get_storage(HarnessRendering)

    # Decide next status: DRAFT until first push, INSTALLED when local matches
    # last-pushed bundle, OUTDATED when local has drifted.
    if harness.last_pushed_commit is None:
        next_status = HarnessStatus.DRAFT
    elif result.bundle_hash == harness.last_pushed_bundle_hash:
        next_status = HarnessStatus.INSTALLED
    else:
        next_status = HarnessStatus.OUTDATED

    new_schema_hash = hash_schema(result.overrides_schema)

    # Persist the freshly computed hashes and schema on the harness row.
    refreshed = await harness_storage.get(harness.id)
    if refreshed is not None:
        updated = refreshed.model_copy(update={
            "bundle_hash": result.bundle_hash,
            "overrides_schema": result.overrides_schema,
            "schema_hash": new_schema_hash,
            "last_operation_at": _now_utc(),
        })
        await harness_storage.update(updated)

    # Per-entity HarnessRendering snapshot so the UI can show drift per row.
    # Only the per-template files are interesting; harness.yaml and the
    # overrides schema are bundle-level concerns.
    import yaml as _yaml
    entries: list[RenderedEntry] = []
    for te in harness.tracked_entities:
        tf = next(
            (
                f for f in result.files
                if f.template_path == f"templates/{te.template_name}.yaml"
            ),
            None,
        )
        if tf is None:
            continue
        rendered_doc = _yaml.safe_load(tf.rendered_text) or {}
        rendered_payload = rendered_doc.get("spec") or {}
        entries.append(
            RenderedEntry(
                kind=te.kind,
                template_name=te.template_name,
                resolved_id=f"{harness.slug}__{te.template_name}",
                template_source_hash=hash_template_source(tf.source_bytes),
                rendered_hash=hash_rendered_payload(rendered_payload),
                rendered_payload=rendered_payload,
                source_dependency=None,
                source_entity_id=te.source_id,
            ),
        )

    rendering = HarnessRendering(
        id=harness.id,
        harness_id=harness.id,
        bundle_hash=result.bundle_hash,
        overrides_hash=hash_overrides(harness.overrides),
        schema_hash=new_schema_hash,
        entries=entries,
        rendered_at=_now_utc(),
    )
    existing = await rendering_storage.get(harness.id)
    if existing is None:
        await rendering_storage.create(rendering)
    else:
        await rendering_storage.update(rendering)

    return next_status, None


# ---------------------------------------------------------------------------
# _do_push (outbound)
# ---------------------------------------------------------------------------


async def _do_push(
    deps: HarnessDispatchDeps,
    harness: Harness,
) -> tuple[HarnessStatus, str | None]:
    """Re-render then push the bundle to the remote git repo."""
    if not harness.tracked_entities:
        return HarnessStatus.ERROR, json.dumps({
            "code": "outbound_no_entities",
            "message": "no tracked entities; cannot push",
        })

    if not harness.git_url:
        # Defence in depth — the push route already rejects this (422). A
        # git-less outbound harness is consumed via build/download, not push.
        return HarnessStatus.ERROR, json.dumps({
            "code": "git_remote_not_configured",
            "message": "no git_url configured; cannot push (build/download instead)",
        })

    try:
        result = await build_outbound(
            harness, storage_provider=deps.storage_provider,
        )
    except OutboundBuildError as exc:
        err: dict[str, Any] = {"code": exc.code, "message": exc.message}
        if exc.template_name:
            err["template_name"] = exc.template_name
        return HarnessStatus.ERROR, json.dumps(err)
    except Exception as exc:
        logger.exception("_do_push build error for harness %s", harness.id)
        return HarnessStatus.ERROR, json.dumps(
            {"code": "build_failed", "message": str(exc)}
        )

    files = [(f.template_path, f.source_bytes) for f in result.files]
    token = harness.git_token.get_secret_value() if harness.git_token else None
    commit_message = (
        f"primer outbound: {harness.slug} @ {_now_utc().isoformat()}"
    )

    try:
        new_sha = await push_bundle(
            url=harness.git_url,
            token=token,
            ref=harness.ref,
            files=files,
            subpath=harness.subpath,
            commit_message=commit_message,
            expected_remote_sha=harness.last_pushed_commit,
        )
    except HarnessGitError as exc:
        return HarnessStatus.ERROR, json.dumps(
            {"code": exc.code, "message": exc.message}
        )
    except Exception as exc:
        logger.exception("_do_push push error for harness %s", harness.id)
        return HarnessStatus.ERROR, json.dumps(
            {"code": "git_push_failed",
             "message": _safe_error_message(exc, token)}
        )

    harness_storage = deps.storage_provider.get_storage(Harness)
    refreshed = await harness_storage.get(harness.id)
    if refreshed is not None:
        updated = refreshed.model_copy(update={
            "last_pushed_commit": new_sha,
            "last_pushed_bundle_hash": result.bundle_hash,
            "last_pushed_at": _now_utc(),
            "bundle_hash": result.bundle_hash,
            "last_operation_at": _now_utc(),
        })
        await harness_storage.update(updated)

    return HarnessStatus.INSTALLED, None


__all__ = [
    "HarnessDispatchDeps",
    "HARNESS_HEARTBEAT_INTERVAL_SECONDS",
    "run_one_harness_operation",
    "sweep_harnesses",
]

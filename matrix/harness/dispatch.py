"""Worker-side harness-operation dispatch.

One ``run_one_harness_operation`` invocation per claimed HarnessLease.
The worker pool's harness claim loop creates these as background tasks;
each task reads the Harness row, runs the pending operation, releases
the claim, and publishes a ``harness:{id}:done`` event.

Structure mirrors ``matrix.chat.dispatch`` (heartbeat task, lease_lost
event, branch on operation, finally cleanup).
"""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import jsonschema
import jsonschema.exceptions

from matrix.harness.git import HarnessGitError, _redact, clone_at_ref, ls_remote
from matrix.harness.hashes import hash_bundle, hash_overrides, hash_schema
from matrix.harness.service import (
    BuildErrors,
    apply_install,
    apply_sync,
    apply_uninstall,
    build_rendered_entries,
)
from matrix.harness.template import HarnessTemplateError, RenderedFile, render_bundle
from matrix.int.event_bus import EventBus
from matrix.int.storage_provider import StorageProvider
from matrix.model.harness import Harness, HarnessOperation, HarnessRendering, HarnessStatus
from matrix.model.storage import OffsetPage


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
_EXPECTED_API_VERSION = "matrix/v1"
_EXPECTED_KIND = "Harness"


def _safe_error_message(exc: Exception, token: str | None) -> str:
    """Defence in depth: any third-party exception text routed into
    ``last_operation_error`` is passed through the token redactor."""
    return _redact(str(exc), token)


@dataclass
class HarnessDispatchDeps:
    """Bundle of runtime dependencies the worker injects per task."""

    storage_provider: StorageProvider
    event_bus: EventBus
    provider_registry: Any | None = None  # may be None in pure-storage tests


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

    lease_lost = asyncio.Event()

    heartbeat_task = asyncio.create_task(
        _heartbeat_loop(deps, harness_id, worker_id, lease_lost),
        name=f"harness-hb-{harness_id}",
    )

    try:
        if lease_lost.is_set():
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
                # Row is gone — skip release, just publish done.
                await deps.event_bus.publish(
                    f"harness:{harness_id}:done", {"harness_id": harness_id},
                )
                return
            else:
                logger.error(
                    "harness %s unknown operation %r — releasing as error",
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
                "harness %s operation %r raised uncaught — releasing as ERROR",
                harness_id, operation,
            )
            next_status = HarnessStatus.ERROR
            error_json = json.dumps(
                {"code": "dispatch_unhandled", "message": str(exc)}
            )

        if not lease_lost.is_set():
            await _release_harness(
                harness_storage,
                harness_id,
                worker_id,
                next_status=next_status,
                last_operation_error=error_json,
            )

    finally:
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except (asyncio.CancelledError, Exception):
            pass

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
# Heartbeat loop
# ---------------------------------------------------------------------------


async def _heartbeat_loop(
    deps: HarnessDispatchDeps,
    harness_id: str,
    worker_id: str,
    lease_lost: asyncio.Event,
) -> None:
    """Heartbeat placeholder — actual lease heartbeating is done by the pool's
    engine heartbeat loop, which sets ``lease_lost`` via the WorkerPool when
    the engine no longer confirms the lease. This coroutine exists so the
    ``run_one_harness_operation`` interface is unchanged; it simply waits to
    be cancelled when the operation completes."""
    try:
        await asyncio.Event().wait()  # wait forever until cancelled
    except asyncio.CancelledError:
        return


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
            overrides_schema: dict[str, Any] | None = None
            new_schema_hash: str | None = None
            if schema_path.is_file():
                try:
                    import json as _json
                    _schema_text = await asyncio.to_thread(schema_path.read_text)
                    overrides_schema = await asyncio.to_thread(_json.loads, _schema_text)
                    new_schema_hash = hash_schema(overrides_schema)
                except Exception as exc:
                    return HarnessStatus.ERROR, json.dumps(
                        {"code": "harness_yaml_invalid",
                         "message": f"overrides.schema.json: {exc}"}
                    )

            # Compute bundle_hash over entire subpath (excluding .git)
            bundle_files = await asyncio.to_thread(_collect_bundle_files, base)
            new_available_bundle_hash = hash_bundle(bundle_files)

            # Re-validate current overrides against new schema
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
            })
            await harness_storage.update(updated)

            return next_status, None

    except Exception as exc:
        logger.exception("_do_fetch unhandled error for harness %s", harness.id)
        return HarnessStatus.ERROR, json.dumps(
            {"code": "fetch_failed", "message": _safe_error_message(exc, token)}
        )


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

            # Verify bundle hash (excluding .git)
            base = Path(dest)
            if harness.subpath:
                base = base / harness.subpath
            current_bundle_hash = hash_bundle(await asyncio.to_thread(_collect_bundle_files, base))
            if (
                harness.available_bundle_hash is not None
                and current_bundle_hash != harness.available_bundle_hash
            ):
                return HarnessStatus.ERROR, json.dumps(
                    {"code": "bundle_hash_mismatch",
                     "message": "bundle changed since fetch; re-run fetch"}
                )

            # Render the bundle
            harness_ctx = {"slug": harness.slug, "name": harness.name,
                           "description": harness.description}
            try:
                rendered = await render_bundle(
                    checkout_dir=dest,
                    subpath=harness.subpath,
                    overrides=harness.overrides,
                    harness_ctx=harness_ctx,
                )
            except HarnessTemplateError as exc:
                return HarnessStatus.ERROR, json.dumps(
                    {"code": exc.code, "message": exc.message}
                )

            # Build + validate entries
            entries, build_errors = build_rendered_entries(rendered, slug=harness.slug)
            if build_errors:
                return HarnessStatus.ERROR, json.dumps(
                    {"code": "build_errors", "errors": build_errors.errors}
                )

            # Build rendered_files_by_name
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

            # Compute bundle hash (excluding .git)
            base = Path(dest)
            if harness.subpath:
                base = base / harness.subpath
            current_bundle_hash = hash_bundle(await asyncio.to_thread(_collect_bundle_files, base))

            # Render the bundle
            harness_ctx = {"slug": harness.slug, "name": harness.name,
                           "description": harness.description}
            try:
                rendered = await render_bundle(
                    checkout_dir=dest,
                    subpath=harness.subpath,
                    overrides=harness.overrides,
                    harness_ctx=harness_ctx,
                )
            except HarnessTemplateError as exc:
                return HarnessStatus.ERROR, json.dumps(
                    {"code": exc.code, "message": exc.message}
                )

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
        )
    except Exception:
        logger.exception("_do_uninstall error for harness %s", harness.id)


__all__ = [
    "HarnessDispatchDeps",
    "HARNESS_HEARTBEAT_INTERVAL_SECONDS",
    "run_one_harness_operation",
    "sweep_harnesses",
]

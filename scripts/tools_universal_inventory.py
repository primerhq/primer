#!/usr/bin/env python3
"""Build and validate the Tools Universal M0 REST/tool parity inventory.

The tracked manifest is the reviewed contract.  ``baseline`` verifies that
the current application and built-in catalogue still match it.  ``strict``
performs the same verification and then refuses every remaining GAP.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import io
import json
import re
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


HTTP_METHODS = frozenset({"delete", "get", "head", "options", "patch", "post", "put"})
EXPECTED_OPERATION_COUNT = 404
EXPECTED_PATH_COUNT = 273
MANIFEST_SCHEMA_VERSION = 1
BASE_COMMIT = "fb18c24b92bec57131bc5e49959344a4d7d97960"
ALLOWED_STATES = frozenset({"curated", "generated", "gap", "skip"})
HUMAN_SIGN_IN_PATHS = frozenset(
    {
        ("POST", "/v1/auth/change-password"),
        ("POST", "/v1/auth/login"),
        ("POST", "/v1/auth/logout"),
        ("POST", "/v1/auth/logout-all"),
        ("POST", "/v1/auth/register"),
        ("GET", "/v1/auth/sso/{provider_id}/callback"),
        ("GET", "/v1/auth/sso/{provider_id}/login"),
        ("POST", "/v1/auth/sso/{provider_id}/link"),
    }
)


class InventoryError(RuntimeError):
    """The current app or catalogue violates the reviewed M0 contract."""


@dataclass(frozen=True)
class ToolRecord:
    scoped_id: str
    toolset_id: str
    bare_id: str
    required_role: str | None
    exposable: bool
    exposure_reason: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "bare_id": self.bare_id,
            "exposable": self.exposable,
            "exposure_reason": self.exposure_reason,
            "required_role": self.required_role,
            "scoped_id": self.scoped_id,
            "toolset_id": self.toolset_id,
        }


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _digest_identities(rows: Iterable[Mapping[str, Any]]) -> str:
    identities = sorted(
        f"{row['operation_id']}\t{row['method']}\t{row['path']}" for row in rows
    )
    return hashlib.sha256(("\n".join(identities) + "\n").encode()).hexdigest()


def _dependency_names(dependencies: Any) -> set[str]:
    names: set[str] = set()
    for dep in dependencies:
        call = getattr(dep, "dependency", None) or getattr(dep, "call", None)
        if call is not None:
            names.add(getattr(call, "__name__", str(call)))
    return names


def _route_dependency_names(route: Any) -> set[str]:
    names: set[str] = set()

    def walk(dependant: Any) -> None:
        for sub in dependant.dependencies:
            if sub.call is not None:
                names.add(getattr(sub.call, "__name__", str(sub.call)))
            walk(sub)

    if hasattr(route, "dependant"):
        walk(route.dependant)
    return names


def _walk_routes(
    routes: Any,
    prefix: str = "",
    inherited: frozenset[str] = frozenset(),
) -> Iterable[tuple[str, str, Any, frozenset[str]]]:
    from fastapi.routing import APIRoute, _IncludedRouter

    for route in routes:
        if isinstance(route, _IncludedRouter):
            context = route.include_context
            yield from _walk_routes(
                route.original_router.routes,
                prefix + context.prefix,
                inherited | _dependency_names(context.dependencies),
            )
        elif isinstance(route, APIRoute):
            dependencies = inherited | _route_dependency_names(route)
            for method in sorted(route.methods or ()):
                yield prefix + route.path, method, route.endpoint, dependencies


def _mount_auth(dependencies: set[str] | frozenset[str]) -> str:
    if any("require_admin" in name for name in dependencies):
        return "admin"
    if "serve_global_settings" in dependencies:
        return "settings-read"
    if any("require_user" in name for name in dependencies):
        return "user"
    if any("require_auth" in name for name in dependencies):
        return "authenticated"
    return "public"


def _rbac_class(
    path: str, mount_auth: str, dependencies: set[str] | frozenset[str]
) -> str:
    if path.startswith("/v1/auth/"):
        return "human-auth" if mount_auth == "public" else "authenticated-account"
    if mount_auth == "admin":
        return "administrator"
    if mount_auth == "settings-read":
        return "global-settings-read"
    if any(
        "access" in name or "visible" in name or "readable" in name
        for name in dependencies
    ):
        return "resource-policy"
    return mount_auth


def _resource(path: str, tags: Sequence[str]) -> str:
    if tags:
        return str(tags[0])
    parts = [part for part in path.split("/") if part]
    return parts[1] if len(parts) > 1 and parts[0] == "v1" else "root"


def _openapi_path(path: str) -> str:
    """Normalize Starlette converters to their OpenAPI path spelling."""
    return re.sub(r"\{([^}:]+):[^}]+\}", r"{\1}", path)


def build_openapi_inventory() -> tuple[list[dict[str, Any]], int]:
    """Enumerate the current app once, including real mount dependencies."""
    from primer.api.app import create_app
    from primer.api.config import AppConfig

    app = create_app(AppConfig())
    schema = app.openapi()
    generated_by_operation = {
        item.operation_id: f"{item.tool.toolset_id}__{item.tool.id}"
        for item in app.state.generated_route_toolsets.operations
    }
    route_meta: dict[tuple[str, str], tuple[str, frozenset[str]]] = {}
    for path, method, endpoint, dependencies in _walk_routes(app.routes):
        route_meta[(method.upper(), _openapi_path(path))] = (
            f"{endpoint.__module__}:{endpoint.__name__}",
            dependencies,
        )

    rows: list[dict[str, Any]] = []
    for path, item in schema["paths"].items():
        for method, operation in item.items():
            if method not in HTTP_METHODS:
                continue
            operation_id = operation.get("operationId")
            if not isinstance(operation_id, str) or not operation_id:
                raise InventoryError(
                    f"operation lacks operationId: {method.upper()} {path}"
                )
            source, dependencies = route_meta.get(
                (method.upper(), path), ("unknown", frozenset())
            )
            mount_auth = _mount_auth(dependencies)
            tags = sorted(str(tag) for tag in (operation.get("tags") or []))
            rows.append(
                {
                    "method": method.upper(),
                    "declared_generated_tool_id": generated_by_operation.get(operation_id),
                    "mount_auth": mount_auth,
                    "operation_id": operation_id,
                    "path": path,
                    "rbac_class": _rbac_class(path, mount_auth, dependencies),
                    "resource": _resource(path, tags),
                    "route_source": source,
                    "tags": tags,
                }
            )
    rows.sort(key=lambda row: (row["operation_id"], row["method"], row["path"]))
    return rows, len(schema["paths"])


async def _enumerate_builtin_catalogue(storage: Any) -> list[ToolRecord]:
    """Enumerate reserved built-ins through the production ProviderRegistry seam."""
    from primer.api.registries import ProviderRegistry, WorkspaceRegistry
    from primer.api.registries.provider_registry import RESERVED_TOOLSET_IDS
    from primer.api.route_tools import generated_route_toolset_providers
    from primer.mcp.safety import is_exposable, tool_scoped_id
    from primer.model.canvas import CANVAS_SETTINGS_ID, CanvasSettings
    from primer.toolset.canvas import build_canvas_toolset
    from primer.toolset.collections import build_collections_toolset
    from primer.toolset.crud import build_crud_toolset
    from primer.toolset.harness import build_harness_toolset_provider
    from primer.toolset.misc import build_misc_toolset
    from primer.toolset.system import build_system_toolset
    from primer.toolset.trigger import build_trigger_toolset_provider
    from primer.toolset.web import build_web_toolset
    from primer.toolset.workspace_ext import build_workspace_ext_toolset
    from primer.toolset.workspaces import build_workspaces_toolset

    registry = ProviderRegistry(
        storage,
        llm_factory=lambda provider: object(),
        embedder_factory=lambda provider: object(),
        cross_encoder_factory=lambda provider: object(),
        toolset_factory=lambda provider: object(),
        generated_api_toolset_providers=generated_route_toolset_providers(),
    )
    workspace_registry = WorkspaceRegistry(storage)
    registry._system_toolset_provider = build_system_toolset(  # noqa: SLF001
        storage_provider=storage,
        provider_registry=registry,
        workspace_registry=workspace_registry,
    )
    registry._misc_toolset_provider = build_misc_toolset()  # noqa: SLF001
    registry._collections_toolset_provider = build_collections_toolset(  # noqa: SLF001
        storage_provider=storage,
        provider_registry=registry,
        semantic_search_registry=None,
    )
    registry._web_toolset_provider = build_web_toolset(  # noqa: SLF001
        web_search_service=object(),
        web_fetch_service=object(),
    )
    registry._workspaces_toolset_provider = build_workspaces_toolset(  # noqa: SLF001
        storage_provider=storage,
        workspace_registry=workspace_registry,
    )
    registry._harness_toolset_provider = build_harness_toolset_provider(  # noqa: SLF001
        storage_provider=storage,
    )
    registry._trigger_toolset_provider = build_trigger_toolset_provider(  # noqa: SLF001
        storage_provider=storage,
    )
    registry._workspace_ext_toolset_provider = build_workspace_ext_toolset(  # noqa: SLF001
        storage_provider=storage,
    )
    registry._crud_toolset_provider = build_crud_toolset(storage_provider=storage)  # noqa: SLF001
    registry._canvas_toolset_provider = build_canvas_toolset(storage_provider=storage)  # noqa: SLF001
    await storage.get_storage(CanvasSettings).create(
        CanvasSettings(id=CANVAS_SETTINGS_ID, agent_tools_enabled=True),
    )

    tools: list[ToolRecord] = []
    for toolset_id in sorted(RESERVED_TOOLSET_IDS):
        provider = await registry.get_toolset(toolset_id)
        async for tool in provider.list_tools():
            exposable, reason = is_exposable(tool, provider=provider)
            tools.append(
                ToolRecord(
                    scoped_id=tool_scoped_id(tool),
                    toolset_id=tool.toolset_id,
                    bare_id=tool.id,
                    required_role=tool.required_role,
                    exposable=exposable,
                    exposure_reason=reason,
                )
            )
    tools.sort(key=lambda tool: tool.scoped_id)
    return tools


async def build_builtin_catalogue() -> list[ToolRecord]:
    """Build the catalogue with exception-safe temporary storage cleanup."""
    import tempfile

    from primer.model.provider import SqliteConfig
    from primer.storage.sqlite import SqliteStorageProvider

    with tempfile.TemporaryDirectory() as temporary_directory:
        storage = SqliteStorageProvider(
            SqliteConfig(path=Path(temporary_directory) / "inventory.sqlite"),
        )
        try:
            await storage.initialize()
            return await _enumerate_builtin_catalogue(storage)
        finally:
            await storage.aclose()


def _new_coverage(operation: Mapping[str, Any]) -> dict[str, Any]:
    """Seed only approved skips; every other unseen operation needs review."""
    if (operation["method"], operation["path"]) in HUMAN_SIGN_IN_PATHS:
        return {
            "aggregation_id": None,
            "reason": "interactive human sign-in or account credential ceremony",
            "skip_code": "human_sign_in",
            "source": "DESIGN.md#approved-decisions-2",
            "state": "skip",
            "tool_ids": [],
        }

    return {
        "aggregation_id": None,
        "reason": "unseen operation requires explicit semantic coverage review",
        "skip_code": None,
        "source": f"reviewed-gap:{operation['resource']}",
        "state": "gap",
        "tool_ids": [],
    }


def bootstrap_manifest(
    operations: Sequence[Mapping[str, Any]],
    path_count: int,
    tools: Sequence[ToolRecord],
    existing: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    reviewed_by_identity = {
        _identity(row): row for row in (existing or {}).get("rows", [])
    }
    coverage_fields = (
        "aggregation_id",
        "reason",
        "skip_code",
        "source",
        "state",
        "tool_ids",
    )
    rows: list[dict[str, Any]] = []
    for operation in operations:
        reviewed = reviewed_by_identity.get(_identity(operation))
        coverage = (
            {field: reviewed[field] for field in coverage_fields}
            if reviewed is not None
            else _new_coverage(operation)
        )
        generated_tool_id = operation.get("declared_generated_tool_id")
        if generated_tool_id is not None:
            if reviewed is not None and reviewed["state"] not in {"gap", "generated"}:
                raise InventoryError(
                    f"generated route declaration conflicts with reviewed {reviewed['state']} row: "
                    f"{_identity(operation)}"
                )
            coverage = {
                "aggregation_id": None,
                "reason": "generated from immutable route metadata and dispatched through the REST operation",
                "skip_code": None,
                "source": "PLAN.md#M1-route-metadata-generator-catalogue-join-and-trusted-ASGI-dispatch",
                "state": "generated",
                "tool_ids": [generated_tool_id],
            }
        rows.append({**operation, **coverage})
    return {
        "base_commit": BASE_COMMIT,
        "expected_operation_count": EXPECTED_OPERATION_COUNT,
        "expected_path_count": EXPECTED_PATH_COUNT,
        "reviewed_gap_sha256": _digest_identities(
            row for row in rows if row["state"] == "gap"
        ),
        "reviewed_skip_sha256": _digest_identities(
            row for row in rows if row["state"] == "skip"
        ),
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "tool_catalogue": [tool.as_dict() for tool in tools],
        "rows": rows,
    }


def _identity(row: Mapping[str, Any]) -> tuple[str, str, str]:
    return str(row["operation_id"]), str(row["method"]), str(row["path"])


def validate_manifest(
    manifest: Mapping[str, Any],
    operations: Sequence[Mapping[str, Any]],
    path_count: int,
    tools: Sequence[ToolRecord],
) -> list[dict[str, Any]]:
    errors: list[str] = []
    rows = manifest.get("rows")
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        errors.append(f"schema_version must be {MANIFEST_SCHEMA_VERSION}")
    if manifest.get("base_commit") != BASE_COMMIT:
        errors.append(f"base_commit must be {BASE_COMMIT}")
    if not isinstance(rows, list) or not rows:
        raise InventoryError("manifest rows are missing or vacuous")
    if len(operations) != EXPECTED_OPERATION_COUNT or path_count != EXPECTED_PATH_COUNT:
        errors.append(
            f"current OpenAPI count drift: operations={len(operations)} paths={path_count}; "
            f"expected {EXPECTED_OPERATION_COUNT}/{EXPECTED_PATH_COUNT}"
        )
    if manifest.get("expected_operation_count") != EXPECTED_OPERATION_COUNT:
        errors.append("manifest expected_operation_count drift")
    if manifest.get("expected_path_count") != EXPECTED_PATH_COUNT:
        errors.append("manifest expected_path_count drift")

    current_by_identity = {_identity(row): row for row in operations}
    if len(current_by_identity) != len(operations):
        errors.append(
            "current OpenAPI contains duplicate operationId+method+path identities"
        )
    manifest_by_identity = {_identity(row): row for row in rows}
    if len(manifest_by_identity) != len(rows):
        errors.append("manifest contains duplicate operationId+method+path identities")
    current_operation_ids = [row["operation_id"] for row in operations]
    duplicates = sorted(
        key for key, count in Counter(current_operation_ids).items() if count > 1
    )
    if duplicates:
        errors.append(f"current OpenAPI contains duplicate operationIds: {duplicates}")
    if set(current_by_identity) != set(manifest_by_identity):
        added = sorted(set(current_by_identity) - set(manifest_by_identity))
        removed = sorted(set(manifest_by_identity) - set(current_by_identity))
        errors.append(f"operation manifest drift: added={added} removed={removed}")

    live_tools = {tool.scoped_id for tool in tools}
    if not live_tools:
        errors.append("built-in catalogue enumeration was vacuous")
    if len(live_tools) != len(tools):
        errors.append("built-in catalogue contains duplicate scoped tool ids")
    manifest_tools = manifest.get("tool_catalogue")
    if not isinstance(manifest_tools, list) or not manifest_tools:
        errors.append("manifest tool catalogue is missing or vacuous")
    elif manifest_tools != [tool.as_dict() for tool in tools]:
        errors.append("built-in tool catalogue drift")

    reverse: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    metadata_fields = (
        "mount_auth",
        "declared_generated_tool_id",
        "rbac_class",
        "resource",
        "route_source",
        "tags",
    )
    for identity, operation in current_by_identity.items():
        row = manifest_by_identity.get(identity)
        if row is None:
            continue
        if any(row.get(field) != operation.get(field) for field in metadata_fields):
            errors.append(f"route metadata drift for {identity}")
        state = row.get("state")
        tool_ids = row.get("tool_ids")
        if state not in ALLOWED_STATES:
            errors.append(f"unknown state for {identity}: {state!r}")
            continue
        if not isinstance(tool_ids, list) or any(
            not isinstance(item, str) for item in tool_ids
        ):
            errors.append(f"tool_ids must be a string list for {identity}")
            continue
        unknown = sorted(set(tool_ids) - live_tools)
        if unknown:
            errors.append(f"unknown tool ids for {identity}: {unknown}")
        if state in {"curated", "generated"} and not tool_ids:
            errors.append(f"{state} row has no tool ids: {identity}")
        if state in {"gap", "skip"} and tool_ids:
            errors.append(f"{state} row unexpectedly maps tools: {identity}")
        declared = operation.get("declared_generated_tool_id")
        if state == "generated" and tool_ids != [declared]:
            errors.append(
                f"generated mapping differs from route declaration for {identity}: "
                f"declared={declared!r}, manifest={tool_ids!r}"
            )
        if state != "generated" and declared is not None:
            errors.append(f"declared generated route is classified {state}: {identity}")
        if state == "skip":
            if (
                row.get("skip_code") != "human_sign_in"
                or (row["method"], row["path"]) not in HUMAN_SIGN_IN_PATHS
            ):
                errors.append(f"unsupported skip classification: {identity}")
        elif row.get("skip_code") is not None:
            errors.append(f"non-skip row carries skip_code: {identity}")
        if not isinstance(row.get("reason"), str) or len(row["reason"]) < 10:
            errors.append(f"missing reviewed reason for {identity}")
        if not isinstance(row.get("source"), str) or not row["source"]:
            errors.append(f"missing reviewed source for {identity}")
        for tool_id in tool_ids:
            reverse[tool_id].append(row)

    for tool_id, mapped_rows in sorted(reverse.items()):
        if len(mapped_rows) < 2:
            continue
        aggregation_ids = {row.get("aggregation_id") for row in mapped_rows}
        if len(aggregation_ids) != 1 or None in aggregation_ids:
            errors.append(
                f"tool {tool_id} maps multiple operations without one explicit aggregation_id"
            )

    gaps = [row for row in rows if row.get("state") == "gap"]
    skips = [row for row in rows if row.get("state") == "skip"]
    if manifest.get("reviewed_gap_sha256") != _digest_identities(gaps):
        errors.append("reviewed GAP manifest digest drift")
    if manifest.get("reviewed_skip_sha256") != _digest_identities(skips):
        errors.append("reviewed SKIP manifest digest drift")
    if errors:
        raise InventoryError("\n".join(errors))
    return [dict(row) for row in rows]


def strict_gap_error(rows: Sequence[Mapping[str, Any]]) -> str | None:
    gaps = [row for row in rows if row["state"] == "gap"]
    if not gaps:
        return None
    by_resource = Counter(str(row["resource"]) for row in gaps)
    summary = ", ".join(f"{key}={value}" for key, value in sorted(by_resource.items()))
    examples = ", ".join(row["operation_id"] for row in gaps[:12])
    internal_collections = ", ".join(
        str(row["operation_id"])
        for row in gaps
        if str(row["path"]).startswith("/v1/internal_collections")
    )
    return (
        f"strict zero-gap check failed: {len(gaps)} GAP operations ({summary}); "
        f"first: {examples}; internal-collections: {internal_collections}"
    )


def _counts(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, int]]:
    return {
        "by_mount_auth": dict(
            sorted(Counter(str(row["mount_auth"]) for row in rows).items())
        ),
        "by_state": dict(sorted(Counter(str(row["state"]) for row in rows).items())),
    }


def write_evidence(
    output_dir: Path,
    rows: Sequence[Mapping[str, Any]],
    tools: Sequence[ToolRecord],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "counts": _counts(rows),
        "rows": rows,
        "tool_catalogue": [tool.as_dict() for tool in tools],
    }
    (output_dir / "inventory.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n"
    )

    csv_buffer = io.StringIO(newline="")
    fieldnames = [
        "operation_id",
        "method",
        "path",
        "declared_generated_tool_id",
        "tags",
        "resource",
        "mount_auth",
        "rbac_class",
        "state",
        "tool_ids",
        "aggregation_id",
        "skip_code",
        "reason",
        "source",
        "route_source",
    ]
    writer = csv.DictWriter(csv_buffer, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(
            {
                key: ";".join(row[key]) if key in {"tags", "tool_ids"} else row.get(key)
                for key in fieldnames
            }
        )
    (output_dir / "inventory.csv").write_text(csv_buffer.getvalue())

    lines = [
        "# Tools Universal M0 inventory",
        "",
        f"Operations: {len(rows)}",
        f"Built-in tools: {len(tools)}",
        "",
        "## Counts",
        "",
        "| Dimension | Value | Count |",
        "| --- | --- | ---: |",
    ]
    for dimension, values in _counts(rows).items():
        lines.extend(
            f"| {dimension} | {value} | {count} |" for value, count in values.items()
        )
    lines.extend(
        [
            "",
            "## Reviewed operations",
            "",
            "| Operation | Method | Path | Resource | Auth | RBAC | State | Tool(s) |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for row in rows:
        lines.append(
            f"| `{row['operation_id']}` | {row['method']} | `{row['path']}` | {row['resource']} | "
            f"{row['mount_auth']} | {row['rbac_class']} | {row['state']} | "
            f"{', '.join(f'`{item}`' for item in row['tool_ids']) or '-'} |"
        )
    (output_dir / "inventory.md").write_text("\n".join(lines) + "\n")


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise InventoryError(f"cannot read manifest {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise InventoryError("manifest root must be an object")
    return value


async def _run(args: argparse.Namespace) -> int:
    operations, path_count = build_openapi_inventory()
    tools = await build_builtin_catalogue()
    if args.bootstrap_manifest:
        existing = _load_manifest(args.manifest) if args.manifest.exists() else None
        manifest = bootstrap_manifest(operations, path_count, tools, existing)
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        args.manifest.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    manifest = _load_manifest(args.manifest)
    rows = validate_manifest(manifest, operations, path_count, tools)
    if args.output_dir:
        write_evidence(args.output_dir, rows, tools)
    if args.mode == "strict":
        failure = strict_gap_error(rows)
        if failure:
            if args.failure_report:
                args.failure_report.parent.mkdir(parents=True, exist_ok=True)
                args.failure_report.write_text(failure + "\n")
            raise InventoryError(failure)
    print(
        _canonical_json(
            {
                "counts": _counts(rows),
                "operations": len(rows),
                "paths": path_count,
                "tools": len(tools),
            }
        )
    )
    return 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("scripts/data/tools_universal_m0_manifest.json"),
    )
    parser.add_argument("--mode", choices=("baseline", "strict"), default="baseline")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--failure-report", type=Path)
    parser.add_argument(
        "--bootstrap-manifest", action="store_true", help=argparse.SUPPRESS
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except InventoryError as exc:
        print(f"tools-universal inventory: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

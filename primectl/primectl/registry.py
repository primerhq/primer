"""Parse an OpenAPI document into a registry of resources + operations.

Pure functions only: no I/O. ``build_registry(spec)`` recognises the generic
CRUD shape the Primer API emits and exposes each resource with its verbs and
(in a later task) custom operations.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field


class UnknownResource(Exception):
    """Raised when a resource name/alias does not resolve."""


@dataclass(frozen=True)
class Operation:
    operation_id: str
    method: str                       # "get" | "post" | "put" | "delete"
    path_template: str                # "/v1/agents/{agent_id}/status"
    path_params: tuple[str, ...] = ()
    query_params: tuple[str, ...] = ()
    request_schema_ref: str | None = None


@dataclass
class Resource:
    name: str                         # singular, e.g. "agent"
    plural: str                       # path segment, e.g. "agents"
    path_prefix: str                  # "/v1/agents"
    id_param: str | None = None       # the {param} in /v1/<plural>/{param}
    list_op: Operation | None = None
    get_op: Operation | None = None
    create_op: Operation | None = None
    update_op: Operation | None = None
    delete_op: Operation | None = None
    find_op: Operation | None = None
    custom_ops: dict[str, Operation] = field(default_factory=dict)
    entity_schema_ref: str | None = None
    aliases: tuple[str, ...] = ()


# Static short aliases (kubectl-like). Extend as needed.
_ALIASES = {
    "ag": "agents",
    "gr": "graphs",
    "ws": "workspaces",
    "col": "collections",
    "doc": "documents",
    "llm": "llm_providers",
    "ts": "toolsets",
}


def _depluralize(plural: str) -> str:
    """Best-effort singular. Conservative: leave irregular/short names alone."""
    if plural.endswith("ies") and len(plural) > 3:
        return plural[:-3] + "y"
    for suffix in ("sses", "shes", "ches", "xes", "zes", "ses"):
        if plural.endswith(suffix):
            return plural[:-2]  # strip the trailing "es"
    if plural.endswith("ss"):
        return plural  # e.g. "ssp"-like names; no strip
    if plural.endswith("s") and len(plural) > 1:
        return plural[:-1]
    return plural


def _request_ref(op_obj: dict) -> str | None:
    body = op_obj.get("requestBody") or {}
    schema = (
        body.get("content", {})
        .get("application/json", {})
        .get("schema", {})
    )
    return schema.get("$ref")


def _query_params(op_obj: dict) -> tuple[str, ...]:
    return tuple(
        p["name"] for p in op_obj.get("parameters", []) if p.get("in") == "query"
    )


def _path_params(op_obj: dict) -> tuple[str, ...]:
    return tuple(
        p["name"] for p in op_obj.get("parameters", []) if p.get("in") == "path"
    )


def _make_op(method: str, path: str, op_obj: dict) -> Operation:
    return Operation(
        operation_id=op_obj.get("operationId", f"{method}_{path}"),
        method=method,
        path_template=path,
        path_params=_path_params(op_obj),
        query_params=_query_params(op_obj),
        request_schema_ref=_request_ref(op_obj),
    )


def _action_name(plural: str, path: str) -> str:
    """Derive an action name from a custom path under /v1/<plural>/.

    Drops the plural prefix and any {param} segments, joins the rest with '-',
    strips leading underscores, and turns '_' into '-'.
    Example: /v1/llm_providers/_discover_models -> "discover-models".
    """
    tail = path[len(f"/v1/{plural}/"):]
    segs = [s for s in tail.split("/") if s and not (s.startswith("{") and s.endswith("}"))]
    raw = "-".join(segs)
    return raw.lstrip("_").replace("_", "-")


# /v1/<plural> exactly
_RE_PLURAL = re.compile(r"^/v1/([^/]+)$")
# /v1/<plural>/{param} exactly (single path param, no further segments)
_RE_ITEM = re.compile(r"^/v1/([^/]+)/\{([^}]+)\}$")
# /v1/<plural>/find
_RE_FIND = re.compile(r"^/v1/([^/]+)/find$")


class ResourceRegistry:
    def __init__(self, resources: list[Resource], spec: dict) -> None:
        self._spec = spec
        self._by_key: dict[str, Resource] = {}
        for r in resources:
            self._by_key[r.name] = r
            self._by_key[r.plural] = r
            for a in r.aliases:
                self._by_key[a] = r
        self._resources = resources

    def all(self) -> list[Resource]:
        return list(self._resources)

    def resolve(self, name_or_alias: str) -> Resource:
        key = name_or_alias.strip()
        if key in self._by_key:
            return self._by_key[key]
        # alias table -> plural -> resource
        if key in _ALIASES and _ALIASES[key] in self._by_key:
            return self._by_key[_ALIASES[key]]
        candidates = sorted({r.name for r in self._resources})
        close = difflib.get_close_matches(key, candidates, n=3)
        hint = f" Did you mean: {', '.join(close)}?" if close else ""
        raise UnknownResource(f"unknown resource {key!r}.{hint}")

    def entity_schema(self, resource: Resource) -> dict | None:
        ref = resource.entity_schema_ref
        if not ref or not ref.startswith("#/components/schemas/"):
            return None
        name = ref.rsplit("/", 1)[-1]
        return self._spec.get("components", {}).get("schemas", {}).get(name)


def build_registry(spec: dict) -> ResourceRegistry:
    paths: dict = spec.get("paths", {})
    resources: dict[str, Resource] = {}

    def ensure(plural: str) -> Resource:
        if plural not in resources:
            aliases = tuple(a for a, p in _ALIASES.items() if p == plural)
            resources[plural] = Resource(
                name=_depluralize(plural),
                plural=plural,
                path_prefix=f"/v1/{plural}",
                aliases=aliases,
            )
        return resources[plural]

    # Pass 1: bare plural (list + create) and find.
    for path, methods in paths.items():
        m = _RE_PLURAL.match(path)
        if m:
            plural = m.group(1)
            r = ensure(plural)
            if "get" in methods:
                r.list_op = _make_op("get", path, methods["get"])
            if "post" in methods:
                r.create_op = _make_op("post", path, methods["post"])
            continue
        mf = _RE_FIND.match(path)
        if mf:
            r = ensure(mf.group(1))
            if "post" in methods:
                r.find_op = _make_op("post", path, methods["post"])

    # Pass 2: item path (get/put/delete) with whatever the id param is named.
    for path, methods in paths.items():
        mi = _RE_ITEM.match(path)
        if not mi:
            continue
        plural, param = mi.group(1), mi.group(2)
        r = ensure(plural)
        r.id_param = param
        if "get" in methods:
            r.get_op = _make_op("get", path, methods["get"])
        if "put" in methods:
            r.update_op = _make_op("put", path, methods["put"])
        if "delete" in methods:
            r.delete_op = _make_op("delete", path, methods["delete"])

    # Pass 3: everything else under /v1/<plural>/... is a custom operation.
    for path, methods in paths.items():
        if _RE_PLURAL.match(path) or _RE_ITEM.match(path) or _RE_FIND.match(path):
            continue
        m = re.match(r"^/v1/([^/]+)/.+$", path)
        if not m:
            continue
        plural = m.group(1)
        if plural not in resources:
            continue
        r = resources[plural]
        action = _action_name(plural, path)
        if not action:
            continue
        for method, op_obj in methods.items():
            if method not in ("get", "post", "put", "delete", "patch"):
                continue
            # First method wins the bare action name; extra methods get suffixed.
            key = action if action not in r.custom_ops else f"{action}-{method}"
            r.custom_ops[key] = _make_op(method, path, op_obj)

    # Entity schema ref: prefer create body, then update body, then get response.
    for r in resources.values():
        for op in (r.create_op, r.update_op):
            if op and op.request_schema_ref:
                r.entity_schema_ref = op.request_schema_ref
                break

    # Keep only real resources: those with an item path (get/put/delete on
    # /v1/<plural>/{id}), a create, or a find. This drops bare-GET utility
    # singletons like /v1/health that have only a list_op.
    kept = [
        r for r in resources.values()
        if r.get_op or r.update_op or r.delete_op or r.create_op or r.find_op
    ]
    kept.sort(key=lambda r: r.name)
    return ResourceRegistry(kept, spec)

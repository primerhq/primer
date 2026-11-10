"""The three fixed MCP meta-tools advertised by Primer."""

from __future__ import annotations

import base64
import hashlib
import json
from contextvars import ContextVar
from typing import Any

import jsonschema

from primer.common.log import redact_credentials
from primer.knowledge.wiki.reference import reference_path
from primer.mcp.dispatch import NotExposed, invoke_exposed
from primer.mcp.exposure import AuthorizedTool, ExposureDeps, authorized_catalogue


LIST_TOOLS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "cursor": {"type": "string", "maxLength": 512},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50},
        "toolset_id": {"type": "string", "minLength": 1, "maxLength": 200},
        "resource": {"type": "string", "minLength": 1, "maxLength": 200},
        "read_only": {"type": "boolean"},
        "required_role": {"enum": ["user", "admin"]},
        "text_prefix": {"type": "string", "maxLength": 200},
        "include_schema": {"type": "boolean", "default": False},
    },
}

FIND_TOOLS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["query"],
    "properties": {
        "query": {"type": "string", "minLength": 1, "maxLength": 1000},
        "cursor": {"type": "string", "maxLength": 512},
        "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
        "include_schema": {"type": "boolean", "default": False},
    },
}

CALL_TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["scoped_id"],
    "properties": {
        "scoped_id": {"type": "string", "minLength": 1, "maxLength": 300},
        "arguments": {"type": "object", "default": {}},
    },
}

META_TOOL_SCHEMAS = {
    "list_tools": LIST_TOOLS_SCHEMA,
    "find_tools": FIND_TOOLS_SCHEMA,
    "call_tool": CALL_TOOL_SCHEMA,
}

META_TOOL_DESCRIPTIONS = {
    "list_tools": "List the Primer tools this caller may use, with deterministic filters and pagination.",
    "find_tools": "Find Primer tools this caller may use by purpose or stable identifier.",
    "call_tool": "Call one authorized Primer tool by its stable scoped identifier.",
}

# The opaque cursor: base64url (padding stripped) of
# {"v": 1, "f": <32-hex sha256 of the canonical filter/query spec>,
# "k": <last scoped id>}. A cursor is only valid for the exact filter
# set or query that produced it; anything else is refused, never
# mis-paged. The house pattern is primer/tap/cursor.py.
_CURSOR_VERSION = 1
_CURSOR_MAX = 512  # matches the cursor maxLength in the LIST/FIND schemas

# Audit-only channel: the stable denial reason of the inner target, read
# by primer/mcp/server.py for the audit record and event payload. It
# never reaches the wire (the wire keeps the fixed envelope).
call_tool_rejection: ContextVar[str | None] = ContextVar(
    "call_tool_rejection", default=None,
)


def _fingerprint(spec: dict) -> str:
    """Truncated sha256 over the canonical active filter/query spec (32 hex chars keeps the token under the cursor limit)."""
    raw = json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()[:32]


def _encode_cursor(key: str, fingerprint: str) -> str:
    raw = json.dumps(
        {"v": _CURSOR_VERSION, "f": fingerprint, "k": key},
        sort_keys=True, separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _decode_cursor(cursor: str, fingerprint: str) -> str | None:
    """Last key when well-formed, v==1 and bound to THIS request's fingerprint; else None."""
    if not cursor or len(cursor) > _CURSOR_MAX:
        return None
    try:
        data = json.loads(
            base64.urlsafe_b64decode(cursor + "=" * ((4 - len(cursor)) % 4)).decode()
        )
    except Exception:  # noqa: BLE001 - opaque client input; never raises
        return None
    if (
        not isinstance(data, dict)
        or data.get("v") != _CURSOR_VERSION
        or data.get("f") != fingerprint
    ):
        return None
    key = data.get("k")
    return key if isinstance(key, str) and key else None


def _err(code: str, message: str) -> dict:
    return {"ok": False, "error": {"code": code, "message": message}}


def _request_context() -> tuple[Any, str | None, list[str] | None]:
    # Lazy import prevents the server/meta-tool modules from forming an
    # import cycle while retaining the request-scoped identity values.
    from primer.mcp.server import (
        current_actor,
        current_api_token_scopes,
        current_principal,
    )

    return (
        current_actor.get(),
        current_principal.get(),
        current_api_token_scopes.get(),
    )


async def _catalogue(deps: ExposureDeps) -> list[AuthorizedTool]:
    actor, principal, scopes = _request_context()
    return await authorized_catalogue(
        deps,
        actor=actor,
        principal=principal,
        api_token_scopes=scopes,
    )


def _purpose(description: str) -> str:
    first = next((line.strip() for line in description.splitlines() if line.strip()), "")
    if first.casefold().startswith("purpose:"):
        first = first.split(":", 1)[1].strip()
    return first[:500]


def _resource(toolset_id: str) -> str:
    return toolset_id[:-4] if toolset_id.endswith("_api") else toolset_id


def _projection(item: AuthorizedTool, *, include_schema: bool) -> dict[str, Any]:
    tool = item.tool
    out: dict[str, Any] = {
        "scoped_id": item.scoped_id,
        "toolset_id": tool.toolset_id,
        "resource": _resource(tool.toolset_id),
        "purpose": _purpose(tool.description or ""),
        "reference_path": reference_path(tool.toolset_id, tool.id),
        "capabilities": {
            "read_only": bool(tool.read_only),
            "required_role": item.required_role,
            "yields": bool(tool.yields),
            "requires_workspace": bool(tool.requires_workspace),
            "interruptible": bool(tool.interruptible),
        },
    }
    if include_schema:
        out["schema"] = tool.args_schema or {"type": "object", "properties": {}}
    return out


def _page(
    items: list[AuthorizedTool],
    *,
    after: str | None,
    fingerprint: str,
    limit: int,
    include_schema: bool,
) -> dict[str, Any]:
    # Keyset pagination over stable scoped ids: inserts before the cursor do
    # not duplicate rows. ``after`` is the decoded key of the request's
    # (filter-bound) cursor; ``fingerprint`` stamps the next one.
    selected = [item for item in items if after is None or item.scoped_id > after]
    page = selected[:limit]
    more = len(selected) > len(page)
    return {
        "items": [_projection(item, include_schema=include_schema) for item in page],
        "next_cursor": _encode_cursor(page[-1].scoped_id, fingerprint) if more and page else None,
    }


async def list_tools(deps: ExposureDeps, arguments: dict[str, Any]) -> dict[str, Any]:
    """List only tools the current caller is authorized to discover."""
    items = await _catalogue(deps)
    toolset_id = arguments.get("toolset_id")
    resource = arguments.get("resource")
    read_only = arguments.get("read_only")
    required_role = arguments.get("required_role")
    # The fingerprint binds the cursor to THIS request's active filter
    # set: a cursor from a different filter set is refused, not
    # mis-paged.
    spec = {
        "toolset_id": toolset_id,
        "resource": resource,
        "read_only": read_only,
        "required_role": required_role,
        "text_prefix": str(arguments.get("text_prefix") or ""),
    }
    fingerprint = _fingerprint(spec)
    cursor = arguments.get("cursor")
    after = _decode_cursor(cursor, fingerprint) if cursor else None
    if cursor is not None and after is None:
        return _err("invalid_cursor", "Cursor is invalid for this request.")
    prefix = str(arguments.get("text_prefix") or "").casefold()
    filtered: list[AuthorizedTool] = []
    for item in items:
        tool = item.tool
        if toolset_id is not None and tool.toolset_id != toolset_id:
            continue
        if resource is not None and _resource(tool.toolset_id) != resource:
            continue
        if read_only is not None and bool(tool.read_only) is not read_only:
            continue
        if required_role is not None and item.required_role != required_role:
            continue
        if prefix:
            fields = (item.scoped_id, tool.toolset_id, tool.id, _purpose(tool.description or ""))
            if not any(field.casefold().startswith(prefix) for field in fields):
                continue
        filtered.append(item)
    return _page(
        filtered,
        after=after,
        fingerprint=fingerprint,
        limit=int(arguments.get("limit", 50)),
        include_schema=bool(arguments.get("include_schema", False)),
    )


def _lexical_match(item: AuthorizedTool, query: str) -> bool:
    words = [word for word in query.casefold().split() if word]
    haystack = " ".join(
        (item.scoped_id, item.tool.toolset_id, item.tool.id, item.tool.description or "")
    ).casefold()
    return all(word in haystack for word in words)


async def find_tools(deps: ExposureDeps, arguments: dict[str, Any]) -> dict[str, Any]:
    """Search the authorized catalogue with lexical fallback always present."""
    query = str(arguments["query"]).strip()
    # A find cursor is bound to the query that issued it.
    fingerprint = _fingerprint({"query": query})
    cursor = arguments.get("cursor")
    after = _decode_cursor(cursor, fingerprint) if cursor else None
    if cursor is not None and after is None:
        return _err("invalid_cursor", "Cursor is invalid for this request.")
    catalogue = await _catalogue(deps)
    by_path = {
        reference_path(item.tool.toolset_id, item.tool.id): item
        for item in catalogue
    }
    found = {item.scoped_id: item for item in catalogue if _lexical_match(item, query)}

    semantic = deps.semantic_search
    if semantic is not None:
        try:
            hits = await semantic(query, max(int(arguments.get("limit", 10)) * 4, 20))
        except Exception:  # noqa: BLE001 - semantic is optional by contract
            hits = []
        for hit in hits:
            path = hit if isinstance(hit, str) else hit.get("path")
            item = by_path.get(path)
            if item is not None:
                found[item.scoped_id] = item

    return _page(
        sorted(found.values(), key=lambda item: item.scoped_id),
        after=after,
        fingerprint=fingerprint,
        limit=int(arguments.get("limit", 10)),
        include_schema=bool(arguments.get("include_schema", False)),
    )


async def call_tool(deps: ExposureDeps, arguments: dict[str, Any]) -> dict[str, Any]:
    """Validate and dispatch one target exclusively through invoke_exposed."""
    call_tool_rejection.set(None)
    scoped_id = str(arguments["scoped_id"])
    target = next((item for item in await _catalogue(deps) if item.scoped_id == scoped_id), None)
    if target is None:
        return _err("tool_unavailable", "Tool is unavailable.")
    inner_arguments = arguments.get("arguments") or {}
    try:
        jsonschema.validate(instance=inner_arguments, schema=target.tool.args_schema)
    except jsonschema.exceptions.ValidationError as exc:
        return {
            "ok": False,
            "error": {
                "code": "invalid_arguments",
                "message": f"Arguments do not match the tool schema: {exc.message}"[:1000],
            },
        }
    except jsonschema.exceptions.SchemaError:
        return _err("tool_unavailable", "Tool is unavailable.")

    actor, principal, scopes = _request_context()
    try:
        result = await invoke_exposed(
            scoped_id=scoped_id,
            arguments=inner_arguments,
            principal=principal,
            actor=actor,
            api_token_scopes=scopes,
            deps=deps,
        )
    except NotExposed as exc:
        # The wire stays indistinguishable; the reason is audit-only.
        call_tool_rejection.set(exc.reason or "not_exposed")
        return _err("tool_unavailable", "Tool is unavailable.")
    if result.is_error:
        # Retained access denials (the in-band scope/role refusals
        # invoke_exposed returns) never reach the wire with their inner
        # wording: the client sees one fixed, bounded message instead.
        if (result.output or "").startswith("access denied:"):
            call_tool_rejection.set("access_denied")
            return _err("access_denied", "Access to this tool is denied.")
        return {
            "ok": False,
            "error": {
                "code": "tool_failed",
                "message": redact_credentials(result.output or "Tool call failed.")[:4096],
            },
        }
    return {"ok": True, "output": result.output or ""}


async def invoke_meta_tool(
    name: str,
    arguments: dict[str, Any],
    deps: ExposureDeps,
) -> dict[str, Any]:
    """Invoke a fixed meta-tool after validating its small outer schema."""
    schema = META_TOOL_SCHEMAS.get(name)
    if schema is None:
        raise KeyError(name)
    jsonschema.validate(instance=arguments, schema=schema)
    if name == "list_tools":
        return await list_tools(deps, arguments)
    if name == "find_tools":
        return await find_tools(deps, arguments)
    return await call_tool(deps, arguments)


def result_text(payload: dict[str, Any]) -> str:
    """Serialize a meta-tool response deterministically for MCP text content."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


__all__ = [
    "CALL_TOOL_SCHEMA",
    "FIND_TOOLS_SCHEMA",
    "LIST_TOOLS_SCHEMA",
    "META_TOOL_DESCRIPTIONS",
    "META_TOOL_SCHEMAS",
    "call_tool",
    "find_tools",
    "invoke_meta_tool",
    "list_tools",
    "result_text",
]

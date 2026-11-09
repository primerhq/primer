"""Declarative REST-to-tool metadata and trusted in-process dispatch.

The REST route remains the behaviour owner.  A generated tool sends an ASGI
request through the application, so FastAPI validation, dependencies, access
checks and RFC 7807 handlers are exactly the same ones the HTTP surface uses.
The only additional trust is an object-identity marker placed in the ASGI scope
by :class:`_TrustedAsgiApp`; no request header or serialized value can select it.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import re
import threading
import time
import weakref
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal, TypeVar, get_args, get_origin
from urllib.parse import quote

import httpx
from fastapi import FastAPI, Request
from fastapi.routing import APIRoute, _IncludedRouter, request_response
from starlette.responses import Response, StreamingResponse
from starlette.types import Send

from primer.access.person import Person, person_for_tool
from primer.model.chat import Tool, ToolCallResult
from primer.model.except_ import ConfigError
from primer.model.yield_ import ToolContext
from primer.toolset._describe import make_tool
from primer.toolset.internal import InternalToolsetProvider


logger = logging.getLogger(__name__)
audit_logger = logging.getLogger("primer.api.route_tool.audit")

MappingState = Literal["curated", "generated", "skip"]
_HTTP_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"})
_METADATA_ATTR = "__primer_route_tool_metadata__"
_TRUST_SCOPE_KEY = "primer.route_tool_dispatch"
_TRUST_PERSON_SCOPE_KEY = "primer.route_tool_person"
_TRUST_OBJECT = object()
_DISPATCH_DEPTH: ContextVar[int] = ContextVar("primer_route_tool_dispatch_depth", default=0)
_DISPATCH_TIMEOUT_S = 30.0
_MAX_RESPONSE_BYTES = 1_048_576
_MAX_LOCAL_REF_DEPTH = 64
_COMPILED_ROUTE_CONTRACT_CACHE_SIZE = 8
_STABLE_ROUTE_PART_CACHE_SIZE = 4_096
_F = TypeVar("_F", bound=Callable[..., Any])


def _freeze_pairs(value: Mapping[str, str] | Sequence[tuple[str, str]] | None) -> tuple[tuple[str, str], ...]:
    if value is None:
        return ()
    items = value.items() if isinstance(value, Mapping) else value
    return tuple(sorted((str(source), str(target)) for source, target in items))


@dataclass(frozen=True)
class RouteToolMetadata:
    """Immutable declaration attached to a FastAPI endpoint.

    ``scoped_id`` is the complete publication declaration. CRUD/access
    factories may instead declare the equally explicit ``resource_id`` and
    ``tool_id`` pair. Incidental operation ids, tags and function names never
    become a published tool id. Collisions fail the build; no suffix is added.
    """

    mapping_state: MappingState = "generated"
    scoped_id: str | None = None
    resource_id: str | None = None
    tool_id: str | None = None
    purpose: str | None = None
    use_when: str | None = None
    argument_overrides: tuple[tuple[str, str], ...] = ()
    read_only: bool | None = None
    yields: bool = False
    requires_workspace: bool = False
    required_role: str | None = None
    interruptible: bool | None = None
    aggregation_id: str | None = None
    skip_code: str | None = None
    methods: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if self.scoped_id is not None and self.scoped_id.count("__") != 1:
            raise ValueError("scoped_id must be '<toolset>__<tool>'")
        if self.mapping_state == "skip" and not self.skip_code:
            raise ValueError("skip route metadata requires skip_code")
        if self.mapping_state != "skip" and self.skip_code is not None:
            raise ValueError("skip_code is valid only for skipped routes")
        if self.methods and any(method not in _HTTP_METHODS for method in self.methods):
            raise ValueError(f"unsupported route-tool method set: {sorted(self.methods)}")


def route_tool(
    *,
    mapping_state: MappingState = "generated",
    scoped_id: str | None = None,
    resource_id: str | None = None,
    tool_id: str | None = None,
    purpose: str | None = None,
    use_when: str | None = None,
    argument_overrides: Mapping[str, str] | Sequence[tuple[str, str]] | None = None,
    read_only: bool | None = None,
    yields: bool = False,
    requires_workspace: bool = False,
    required_role: str | None = None,
    interruptible: bool | None = None,
    aggregation_id: str | None = None,
    skip_code: str | None = None,
    methods: Sequence[str] = (),
) -> Callable[[_F], _F]:
    """Attach one immutable REST-to-tool declaration to an endpoint."""

    metadata = RouteToolMetadata(
        mapping_state=mapping_state,
        scoped_id=scoped_id,
        resource_id=resource_id,
        tool_id=tool_id,
        purpose=purpose,
        use_when=use_when,
        argument_overrides=_freeze_pairs(argument_overrides),
        read_only=read_only,
        yields=yields,
        requires_workspace=requires_workspace,
        required_role=required_role,
        interruptible=interruptible,
        aggregation_id=aggregation_id,
        skip_code=skip_code,
        methods=frozenset(method.upper() for method in methods),
    )

    def decorate(endpoint: _F) -> _F:
        existing = tuple(getattr(endpoint, _METADATA_ATTR, ()))
        setattr(endpoint, _METADATA_ATTR, (*existing, metadata))
        return endpoint

    return decorate


@dataclass(frozen=True)
class CrudToolDeclaration:
    """One CRUD resource's generated-operation and shared-guard declaration."""

    resource_id: str
    generated_operations: frozenset[str]
    guards: Any = None
    operation_ids: tuple[tuple[str, str], ...] = ()

    @property
    def curated_operations(self) -> frozenset[str]:
        """CRUD verbs whose existing hand-written tool wins publication."""

        return frozenset({"create", "get", "update", "delete", "list", "find"}) - self.generated_operations

    def metadata_for(self, operation: str) -> RouteToolMetadata:
        state: MappingState = "generated" if operation in self.generated_operations else "curated"
        overrides = dict(self.operation_ids)
        return RouteToolMetadata(
            mapping_state=state,
            resource_id=self.resource_id,
            tool_id=overrides.get(operation, operation),
            read_only=operation in {"get", "list", "find"},
        )


def attach_route_tool_metadata(endpoint: Callable[..., Any], metadata: RouteToolMetadata) -> None:
    """Attach factory-produced metadata without wrapping the endpoint."""

    existing = tuple(getattr(endpoint, _METADATA_ATTR, ()))
    setattr(endpoint, _METADATA_ATTR, (*existing, metadata))


def route_tool_metadata(endpoint: Callable[..., Any]) -> tuple[RouteToolMetadata, ...]:
    """Return endpoint declarations exactly as FastAPI route inspection sees them."""

    return tuple(getattr(endpoint, _METADATA_ATTR, ()))


def canonical_tool_id(value: str) -> str:
    """Canonical provider/tool spelling; deterministic and never lossy-by-suffix."""

    canonical = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
    if not canonical:
        raise ConfigError(f"route-tool id {value!r} canonicalizes to an empty id")
    return canonical


def _metadata_for(route: APIRoute, method: str) -> RouteToolMetadata | None:
    declarations = route_tool_metadata(route.endpoint)
    matches = [item for item in declarations if not item.methods or method in item.methods]
    if len(matches) > 1:
        raise ConfigError(f"route {method} {route.path} has multiple applicable route-tool declarations")
    return matches[0] if matches else None


def _rewrite_refs(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: (
                item.replace("#/components/schemas/", "#/$defs/")
                if key == "$ref" and isinstance(item, str)
                else _rewrite_refs(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_rewrite_refs(item) for item in value]
    return value


def _tool_schema(value: Any) -> Any:
    """Translate OpenAPI schemas into self-contained Draft 2020-12 nodes."""

    rewritten = _rewrite_refs(value)
    if isinstance(rewritten, dict):
        mapped = {key: _tool_schema(item) for key, item in rewritten.items()}
        is_file = mapped.get("format") == "binary" or isinstance(
            mapped.get("contentMediaType"), str
        )
        if mapped.get("type") == "string" and is_file:
            mapped.setdefault("contentEncoding", "base64")
            mapped.setdefault("contentMediaType", "application/octet-stream")
        return mapped
    if isinstance(rewritten, list):
        return [_tool_schema(item) for item in rewritten]
    return rewritten


def _transitive_definitions(root: Any, definitions: Mapping[str, Any]) -> dict[str, Any]:
    pending: list[str] = []

    def find(node: Any) -> None:
        if isinstance(node, Mapping):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                pending.append(ref.removeprefix("#/$defs/"))
            for item in node.values():
                find(item)
        elif isinstance(node, list):
            for item in node:
                find(item)

    find(root)
    selected: dict[str, Any] = {}
    while pending:
        name = pending.pop()
        if name in selected:
            continue
        if name not in definitions:
            raise ConfigError(f"route-tool schema references missing component {name!r}")
        selected[name] = _tool_schema(definitions[name])
        find(selected[name])
    return {name: selected[name] for name in sorted(selected)}


@dataclass(frozen=True)
class _ArgumentBinding:
    argument: str
    wire_name: str
    location: Literal["path", "query", "header", "body"]
    media_type: str | None = None
    binary_fields: tuple[str, ...] = ()


def _resolve_local_component_schema(
    value: Any,
    definitions: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Resolve a local component-ref chain for request binding inspection.

    The published schema keeps its refs and is made self-contained separately.
    This bounded resolver is only for deciding which multipart fields require
    base64 decoding before the in-process HTTP request is dispatched.
    """

    current = value
    seen: set[str] = set()
    for _depth in range(_MAX_LOCAL_REF_DEPTH):
        if not isinstance(current, Mapping):
            return {}
        ref = current.get("$ref")
        if not isinstance(ref, str) or not ref.startswith("#/components/schemas/"):
            return current
        name = ref.removeprefix("#/components/schemas/")
        if name in seen:
            raise ConfigError(
                f"route-tool schema has a component reference cycle at {name!r}"
            )
        seen.add(name)
        if name not in definitions:
            raise ConfigError(f"route-tool schema references missing component {name!r}")
        current = definitions[name]
    raise ConfigError(
        f"route-tool schema exceeds {_MAX_LOCAL_REF_DEPTH} local component references"
    )


def _is_multipart_binary_field(
    value: Any,
    definitions: Mapping[str, Any],
) -> bool:
    schema = _resolve_local_component_schema(value, definitions)
    return schema.get("type") == "string" and (
        schema.get("format") == "binary"
        or isinstance(schema.get("contentMediaType"), str)
    )


def derive_argument_schema(
    operation: Mapping[str, Any],
    components: Mapping[str, Any],
    metadata: RouteToolMetadata,
) -> tuple[dict[str, Any], tuple[_ArgumentBinding, ...]]:
    """Derive one self-contained tool schema and its request bindings."""

    overrides = dict(metadata.argument_overrides)
    properties: dict[str, Any] = {}
    required: list[str] = []
    bindings: list[_ArgumentBinding] = []
    for parameter in operation.get("parameters") or ():
        location = parameter.get("in")
        if location not in {"path", "query", "header"}:
            continue
        wire_name = str(parameter["name"])
        argument = overrides.get(f"{location}.{wire_name}", overrides.get(wire_name, wire_name))
        if argument in properties:
            raise ConfigError(f"route-tool argument override collides at {argument!r}")
        properties[argument] = _tool_schema(parameter.get("schema") or {})
        if parameter.get("description"):
            properties[argument] = {**properties[argument], "description": parameter["description"]}
        if parameter.get("required"):
            required.append(argument)
        bindings.append(_ArgumentBinding(argument, wire_name, location))

    request_body = operation.get("requestBody") or {}
    content = request_body.get("content") or {}
    definitions = components.get("schemas") or {}
    if content:
        preferred = next(
            (kind for kind in ("application/json", "multipart/form-data", "application/x-www-form-urlencoded", "application/octet-stream") if kind in content),
            sorted(content)[0],
        )
        argument = overrides.get("body", "body")
        if argument in properties:
            raise ConfigError(f"route-tool body argument collides at {argument!r}")
        body_schema = (content[preferred] or {}).get("schema") or {}
        properties[argument] = _tool_schema(body_schema)
        if request_body.get("description"):
            properties[argument] = {**properties[argument], "description": request_body["description"]}
        if request_body.get("required"):
            required.append(argument)
        resolved_body_schema = _resolve_local_component_schema(
            body_schema, definitions
        )
        binary_fields = tuple(
            sorted(
                str(name)
                for name, field_schema in (
                    resolved_body_schema.get("properties") or {}
                ).items()
                if _is_multipart_binary_field(field_schema, definitions)
            )
        )
        bindings.append(_ArgumentBinding(argument, "body", "body", preferred, binary_fields))

    schema: dict[str, Any] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = sorted(set(required))
    selected = _transitive_definitions(schema, definitions)
    if selected:
        schema["$defs"] = selected
    return schema, tuple(bindings)


def _dependency_names(route: APIRoute) -> set[str]:
    names: set[str] = set()

    def walk(dependant: Any) -> None:
        call = getattr(dependant, "call", None)
        if call is not None:
            names.add(getattr(call, "__name__", repr(call)))
        for child in getattr(dependant, "dependencies", ()):
            walk(child)

    walk(route.dependant)
    return names


def _required_role(
    route: APIRoute,
    metadata: RouteToolMetadata,
    inherited_dependencies: Sequence[Any] = (),
) -> str:
    if metadata.required_role is not None:
        return metadata.required_role
    names = _dependency_names(route)
    names.update(
        getattr(getattr(dependency, "dependency", None), "__name__", "")
        for dependency in inherited_dependencies
    )
    return "admin" if "require_admin" in names else "user"


def _purpose(route: APIRoute, metadata: RouteToolMetadata) -> str:
    summary = (route.summary or route.name or "Call REST operation").rstrip(".")
    return metadata.purpose or f"{summary}."


def _use_when(route: APIRoute, metadata: RouteToolMetadata) -> str:
    return metadata.use_when or f"Use when the matching {route.path} REST operation is needed."


def _scoped_id(
    route: APIRoute,
    operation: Mapping[str, Any],
    metadata: RouteToolMetadata,
    full_path: str,
) -> tuple[str, str]:
    if metadata.scoped_id is not None:
        return tuple(metadata.scoped_id.split("__", 1))  # type: ignore[return-value]
    if metadata.resource_id is None or metadata.tool_id is None:
        raise ConfigError(
            f"generated route {full_path!r} lacks an explicit scoped_id or resource_id+tool_id"
        )
    return canonical_tool_id(metadata.resource_id), canonical_tool_id(metadata.tool_id)


def _format_path(path: str, bindings: Sequence[_ArgumentBinding], arguments: Mapping[str, Any]) -> str:
    rendered = path
    for binding in bindings:
        if binding.location == "path":
            rendered = rendered.replace("{" + binding.wire_name + "}", quote(str(arguments[binding.argument]), safe=""))
    return rendered


def _request_parts(
    bindings: Sequence[_ArgumentBinding], arguments: Mapping[str, Any]
) -> tuple[list[tuple[str, str]], dict[str, str], dict[str, Any]]:
    query: list[tuple[str, str]] = []
    headers: dict[str, str] = {}
    body: dict[str, Any] = {}
    for binding in bindings:
        if binding.argument not in arguments:
            continue
        value = arguments[binding.argument]
        if binding.location == "query":
            values = value if isinstance(value, list) else [value]
            query.extend((binding.wire_name, str(item).lower() if isinstance(item, bool) else str(item)) for item in values)
        elif binding.location == "header":
            headers[binding.wire_name] = str(value)
        elif binding.location == "body":
            body = {
                "media_type": binding.media_type,
                "value": value,
                "binary_fields": binding.binary_fields,
            }
    return query, headers, body


def _body_kwargs(body: Mapping[str, Any]) -> dict[str, Any]:
    if not body:
        return {}
    media_type = body["media_type"]
    value = body["value"]
    if media_type == "application/json":
        return {"json": value}
    if media_type == "multipart/form-data":
        if not isinstance(value, Mapping):
            raise ConfigError("multipart/form-data body must be an object")
        binary_fields = set(body.get("binary_fields") or ())
        data: dict[str, str] = {}
        files: dict[str, tuple[str, bytes, str]] = {}
        for key, item in value.items():
            if key in binary_fields:
                if not isinstance(item, str):
                    raise ConfigError(f"multipart binary field {key!r} must be base64 text")
                try:
                    content = base64.b64decode(item, validate=True)
                except (binascii.Error, ValueError) as exc:
                    raise ConfigError(f"multipart binary field {key!r} must be base64 text") from exc
                files[key] = (f"{key}.bin", content, "application/octet-stream")
            else:
                data[key] = item if isinstance(item, str) else json.dumps(item, separators=(",", ":"))
        return {"data": data, "files": files}
    if media_type == "application/x-www-form-urlencoded":
        return {"data": value}
    if media_type == "application/octet-stream":
        if isinstance(value, str):
            try:
                value = base64.b64decode(value, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ConfigError("application/octet-stream body must be base64") from exc
        return {"content": value, "headers": {"content-type": media_type}}
    return {"content": value if isinstance(value, (bytes, str)) else json.dumps(value), "headers": {"content-type": media_type}}


class _TrustedAsgiApp:
    def __init__(self, app: FastAPI, person: Person) -> None:
        self._app = app
        self._person = person

    async def __call__(self, scope, receive, send) -> None:
        trusted_scope = dict(scope)
        trusted_scope[_TRUST_SCOPE_KEY] = _TRUST_OBJECT
        trusted_scope[_TRUST_PERSON_SCOPE_KEY] = self._person
        await self._app(trusted_scope, receive, send)


class _ResponseLimitExceeded(BaseException):
    """Private control flow that bypasses application exception handlers."""

    def __init__(self, status_code: int | None) -> None:
        self.status_code = status_code


class _ResponseEncodingRejected(BaseException):
    """Non-identity response encoding refused before the first body byte."""

    def __init__(self, status_code: int | None, content_encoding: str) -> None:
        self.status_code = status_code
        self.content_encoding = content_encoding


def _contains_response_limit(exc: BaseException) -> bool:
    if isinstance(exc, _ResponseLimitExceeded):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return any(_contains_response_limit(item) for item in exc.exceptions)
    return False


def _find_response_encoding_rejection(
    exc: BaseException,
) -> _ResponseEncodingRejected | None:
    if isinstance(exc, _ResponseEncodingRejected):
        return exc
    if isinstance(exc, BaseExceptionGroup):
        for item in exc.exceptions:
            found = _find_response_encoding_rejection(item)
            if found is not None:
                return found
    return None


def _non_identity_content_encoding(
    headers: Sequence[tuple[bytes, bytes]],
) -> str | None:
    codings = [
        coding.strip()
        for key, value in headers
        if key.lower() == b"content-encoding"
        for coding in value.decode("latin-1").split(",")
        if coding.strip() and coding.strip().lower() != "identity"
    ]
    return ", ".join(codings) or None


class _ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks: Sequence[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk


class _BoundedAsgiTransport(httpx.AsyncBaseTransport):
    """ASGI transport that stops the producer at the response byte limit."""

    def __init__(self, app: _TrustedAsgiApp, *, max_response_bytes: int) -> None:
        self._app = app
        self._max_response_bytes = max_response_bytes

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if not isinstance(request.stream, httpx.AsyncByteStream):
            raise TypeError("bounded ASGI transport requires an async request stream")

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": request.method,
            "headers": [(key.lower(), value) for key, value in request.headers.raw],
            "scheme": request.url.scheme,
            "path": request.url.path,
            "raw_path": request.url.raw_path.split(b"?")[0],
            "query_string": request.url.query,
            "server": (request.url.host, request.url.port),
            "client": ("127.0.0.1", 0),
            "root_path": "",
        }
        request_chunks = request.stream.__aiter__()
        request_complete = False
        response_complete = asyncio.Event()
        response_started = False
        status_code: int | None = None
        response_headers: list[tuple[bytes, bytes]] | None = None
        response_chunks: list[bytes] = []
        response_size = 0
        abort: asyncio.Future[
            _ResponseLimitExceeded | _ResponseEncodingRejected
        ] = asyncio.get_running_loop().create_future()
        abort_hold = asyncio.Event()

        async def receive() -> dict[str, Any]:
            nonlocal request_complete
            if request_complete:
                await response_complete.wait()
                return {"type": "http.disconnect"}
            try:
                chunk = await request_chunks.__anext__()
            except StopAsyncIteration:
                request_complete = True
                return {"type": "http.request", "body": b"", "more_body": False}
            return {"type": "http.request", "body": chunk, "more_body": True}

        async def send(message: Mapping[str, Any]) -> None:
            nonlocal response_headers, response_size, response_started, status_code
            if message["type"] == "http.response.start":
                if response_started:
                    raise RuntimeError("ASGI response started more than once")
                status_code = int(message["status"])
                response_headers = list(message.get("headers", ()))
                response_started = True
                content_encoding = _non_identity_content_encoding(response_headers)
                if content_encoding is not None:
                    abort.set_result(
                        _ResponseEncodingRejected(status_code, content_encoding)
                    )
                    await abort_hold.wait()
                return
            if message["type"] != "http.response.body":
                return
            chunk = message.get("body", b"")
            if chunk and request.method != "HEAD":
                if response_size + len(chunk) > self._max_response_bytes:
                    abort.set_result(_ResponseLimitExceeded(status_code))
                    await abort_hold.wait()
                response_chunks.append(chunk)
                response_size += len(chunk)
            if not message.get("more_body", False):
                response_complete.set()

        app_task = asyncio.create_task(self._app(scope, receive, send))

        async def cancel_app_task() -> None:
            caller = asyncio.current_task()
            cancellation_count = caller.cancelling() if caller is not None else 0
            app_task.cancel()
            try:
                await app_task
            except asyncio.CancelledError:
                if caller is not None and caller.cancelling() > cancellation_count:
                    raise
            except Exception:
                pass

        try:
            done, _pending = await asyncio.wait(
                (app_task, abort),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if abort in done:
                await cancel_app_task()
                raise abort.result() from None

            try:
                await app_task
            except BaseException as exc:
                response_complete.set()
                encoding_rejection = _find_response_encoding_rejection(exc)
                if encoding_rejection is not None:
                    raise _ResponseEncodingRejected(
                        status_code,
                        encoding_rejection.content_encoding,
                    ) from None
                if _contains_response_limit(exc):
                    raise _ResponseLimitExceeded(status_code) from None
                if not isinstance(exc, Exception):
                    raise
                if status_code is None:
                    status_code = 500
                if response_headers is None:
                    response_headers = []
        finally:
            if not app_task.done():
                await cancel_app_task()

        if not response_complete.is_set():
            raise RuntimeError("ASGI application returned without completing a response")
        if status_code is None or response_headers is None:
            raise RuntimeError("ASGI application returned an incomplete response")
        return httpx.Response(
            status_code,
            headers=response_headers,
            stream=_ChunkStream(tuple(response_chunks)),
        )


def is_trusted_route_tool_scope(scope: Mapping[str, Any]) -> bool:
    """True only for a scope constructed by this process's private wrapper."""

    return scope.get(_TRUST_SCOPE_KEY) is _TRUST_OBJECT


def trusted_route_tool_person(scope: Mapping[str, Any]) -> Person | None:
    """Return the exact in-process tool person, never a serialized identity."""

    if not is_trusted_route_tool_scope(scope):
        return None
    person = scope.get(_TRUST_PERSON_SCOPE_KEY)
    return person if isinstance(person, Person) else None


def _install_stream_cleanup(route: APIRoute) -> bool:
    """Close a trusted route's streaming body when dispatch is aborted."""

    if getattr(route, "_primer_route_tool_stream_cleanup", False):
        return False
    original_get_route_handler = route.get_route_handler

    def get_route_handler() -> Callable[[Request], Awaitable[Response]]:
        original_handler = original_get_route_handler()

        async def close_trusted_stream(request: Request) -> Response:
            response = await original_handler(request)
            if not is_trusted_route_tool_scope(request.scope) or not isinstance(
                response, StreamingResponse
            ):
                return response
            original_stream_response = response.stream_response
            body_iterator = response.body_iterator

            async def stream_response(send: Send) -> None:
                try:
                    await original_stream_response(send)
                finally:
                    aclose = getattr(body_iterator, "aclose", None)
                    if aclose is not None:
                        await aclose()

            response.stream_response = stream_response
            return response

        return close_trusted_stream

    route.get_route_handler = get_route_handler
    route.app = request_response(get_route_handler())
    route._primer_route_tool_stream_cleanup = True
    return True


def _install_generated_stream_cleanup(routes: Sequence[Any]) -> bool:
    changed = False
    for route in routes:
        if isinstance(route, _IncludedRouter):
            child_changed = _install_generated_stream_cleanup(
                route.original_router.routes
            )
            if child_changed:
                route.original_router._mark_routes_changed()  # noqa: SLF001
            changed = changed or child_changed
        elif isinstance(route, APIRoute) and any(
            metadata.mapping_state == "generated"
            for metadata in route_tool_metadata(route.endpoint)
        ):
            changed = _install_stream_cleanup(route) or changed
    return changed


def _response_result(
    *, status_code: int, headers: Mapping[str, str], content: bytes
) -> ToolCallResult:
    content_type = headers.get("content-type", "").split(";", 1)[0]
    if not content:
        payload: Any = None
    elif content_type in {"application/json", "application/problem+json"} or content_type.endswith("+json"):
        payload = json.loads(content)
    else:
        payload = {
            "content_type": content_type or "application/octet-stream",
            "body_base64": base64.b64encode(content).decode("ascii"),
        }
    return ToolCallResult(
        output=json.dumps(payload, separators=(",", ":"), default=str),
        is_error=status_code >= 400,
    )


@dataclass(frozen=True)
class GeneratedRouteTool:
    operation_id: str
    method: str
    path: str
    metadata: RouteToolMetadata
    bindings: tuple[_ArgumentBinding, ...]
    tool: Tool


@dataclass(frozen=True)
class GeneratedRouteToolSnapshot:
    """One deterministic, immutable application build of generated providers."""

    operations: tuple[GeneratedRouteTool, ...]
    providers: Mapping[str, InternalToolsetProvider]


# Inventory and catalogue readers use the latest deterministic build without
# owning its application. Provider handlers below resolve the app through a
# weak reference so this convenience pointer cannot extend an app's lifetime.
_LATEST_SNAPSHOT: GeneratedRouteToolSnapshot | None = None


def _walk_api_routes(
    routes: Sequence[Any],
    *,
    prefix: str = "",
    inherited_dependencies: tuple[Any, ...] = (),
) -> Sequence[tuple[str, APIRoute, tuple[Any, ...]]]:
    walked: list[tuple[str, APIRoute, tuple[Any, ...]]] = []
    for route in routes:
        if isinstance(route, _IncludedRouter):
            context = route.include_context
            walked.extend(
                _walk_api_routes(
                    route.original_router.routes,
                    prefix=prefix + context.prefix,
                    inherited_dependencies=(*inherited_dependencies, *context.dependencies),
                )
            )
        elif isinstance(route, APIRoute):
            walked.append((prefix + route.path, route, inherited_dependencies))
    return walked


class _IdentityKey:
    """Hash an otherwise mutable contract object strictly by identity."""

    __slots__ = ("value",)

    def __init__(self, value: Any) -> None:
        self.value = value

    def __hash__(self) -> int:
        return id(self.value)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _IdentityKey) and self.value is other.value


@dataclass(frozen=True)
class _RouteCompilationKey:
    """The stable source-route contract that controls generated descriptors.

    Prefixes, parameter/dependency shapes and unique-id generation remain per
    inclusion, so they are part of the key. Code objects and schema callables
    use identity wrappers: this avoids hashing mutable internals and prevents
    an object id from being reused while its bounded cache entry is live.
    """

    routes: tuple[tuple[Any, ...], ...]
    webhook_routes: tuple[_IdentityKey, ...]
    openapi_function: tuple[_IdentityKey, ...]
    openapi_schema: _IdentityKey | None
    openapi_version: str
    separate_input_output_schemas: bool
    unique_id_function: _IdentityKey


@dataclass(frozen=True)
class _StableRoutePartKey:
    """Identity guard for one immutable source route declaration."""

    full_path: str
    endpoint: _IdentityKey
    inherited_dependencies: tuple[tuple[Any, ...], ...]


@dataclass(frozen=True)
class _GeneratedRouteToolBlueprint:
    """App-independent descriptor data with its mutable schema serialized."""

    operation_id: str
    method: str
    path: str
    metadata: RouteToolMetadata
    bindings: tuple[_ArgumentBinding, ...]
    tool_id: str
    toolset_id: str
    description: str
    args_schema_encoded: bytes
    yields: bool
    requires_workspace: bool
    required_role: str | None
    tool_class: Literal["standard", "notifying"]
    interruptible: bool
    read_only: bool
    preview_args: tuple[str, ...] | None
    preview_lead: tuple[str, ...] | None


@dataclass(frozen=True)
class _CompiledRouteSchema:
    """App-free schema and optional immutable generated-tool blueprints."""

    encoded: bytes
    blueprints: tuple[_GeneratedRouteToolBlueprint, ...] | None = None


_COMPILED_ROUTE_CONTRACTS: OrderedDict[
    _RouteCompilationKey, _CompiledRouteSchema
] = OrderedDict()
_STABLE_ROUTE_CONTRACT_PARTS: OrderedDict[
    int,
    tuple[
        weakref.ReferenceType[APIRoute],
        dict[_StableRoutePartKey, tuple[Any, ...]],
    ],
] = OrderedDict()
# A weak-route cleanup can run during an allocation made under this lock.
_COMPILED_ROUTE_CONTRACTS_LOCK = threading.RLock()


def _openapi_callable_contract(app: FastAPI) -> tuple[_IdentityKey, ...]:
    value = app.openapi
    owner = getattr(value, "__self__", None)
    function = getattr(value, "__func__", None)
    if owner is app and function is FastAPI.openapi:
        return (_IdentityKey(function),)
    return (
        _IdentityKey(app),
        _IdentityKey(function if function is not None else value),
    )


def _callable_contract(value: Callable[..., Any] | None) -> _IdentityKey:
    function = getattr(value, "__func__", value)
    code = getattr(function, "__code__", None)
    return _IdentityKey(code if code is not None else function)


def _dependency_contract(dependency: Any) -> tuple[Any, ...]:
    call = getattr(dependency, "dependency", None)
    scopes = getattr(dependency, "scopes", ()) or ()
    return (
        _callable_contract(call),
        bool(getattr(dependency, "use_cache", True)),
        getattr(dependency, "scope", None),
        tuple(scopes),
    )


def _type_contract(value: Any) -> Any:
    origin = get_origin(value)
    if origin is not None:
        return (
            _IdentityKey(origin),
            tuple(_type_contract(argument) for argument in get_args(value)),
        )
    if isinstance(value, type):
        return _IdentityKey(value)
    if value is None or isinstance(value, (bool, int, float, str, bytes)):
        return value
    return repr(value)


def _value_contract(value: Any) -> Any:
    if isinstance(value, Mapping):
        return tuple(
            (str(key), _value_contract(item))
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        )
    if isinstance(value, (list, tuple)):
        return tuple(_value_contract(item) for item in value)
    if get_origin(value) is not None or isinstance(value, type):
        return _type_contract(value)
    if callable(value):
        return _callable_contract(value)
    if value is None or isinstance(value, (bool, int, float, str, bytes)):
        return value
    return repr(value)


def _field_contract(field: Any) -> tuple[str, str | None, Any, str]:
    adapter = getattr(field, "_type_adapter", None)
    return (
        str(getattr(field, "name", "")),
        getattr(field, "mode", None),
        _type_contract(getattr(adapter, "_type", None)),
        repr(adapter),
    )


def _dependant_contract(dependant: Any) -> tuple[Any, ...]:
    fields = tuple(
        (
            location,
            *(_field_contract(field)),
        )
        for location in (
            "path_params",
            "query_params",
            "header_params",
            "cookie_params",
            "body_params",
        )
        for field in getattr(dependant, location, ())
    )
    return (
        _callable_contract(getattr(dependant, "call", None)),
        fields,
        _value_contract(getattr(dependant, "own_oauth_scopes", None)),
        _value_contract(getattr(dependant, "parent_oauth_scopes", None)),
        _value_contract(getattr(dependant, "security_scopes", None)),
        _value_contract(getattr(dependant, "security_requirements", None)),
        getattr(dependant, "security_scopes_param_name", None),
        bool(getattr(dependant, "use_cache", True)),
        getattr(dependant, "scope", None),
        tuple(
            _dependant_contract(child)
            for child in getattr(dependant, "dependencies", ())
        ),
    )


def _route_contract_part(
    full_path: str,
    route: APIRoute,
    inherited_dependency_contracts: tuple[tuple[Any, ...], ...],
) -> tuple[Any, ...]:
    return (
        full_path,
        route.include_in_schema,
        tuple(sorted((route.methods or set()) & _HTTP_METHODS)),
        route.unique_id,
        _callable_contract(route.endpoint),
        route.operation_id,
        route.name,
        route.summary,
        route.description,
        route.response_description,
        route.status_code,
        _type_contract(route.response_model),
        route.response_model_by_alias,
        _value_contract(route.response_model_include),
        _value_contract(route.response_model_exclude),
        route.response_model_exclude_unset,
        route.response_model_exclude_defaults,
        route.response_model_exclude_none,
        _value_contract(route.response_class),
        tuple(route.tags or ()),
        _value_contract(route.responses),
        tuple(_IdentityKey(callback) for callback in route.callbacks),
        route.deprecated,
        _value_contract(route.openapi_extra),
        _dependant_contract(route.dependant),
        route_tool_metadata(route.endpoint),
        inherited_dependency_contracts,
    )


def _stable_route_contract_part(
    full_path: str,
    route: APIRoute,
    inherited_dependencies: tuple[Any, ...],
) -> tuple[Any, ...]:
    """Reuse a route part only for the exact source route and endpoint objects."""

    inherited_contracts = tuple(
        _dependency_contract(dependency)
        for dependency in inherited_dependencies
    )
    key = _StableRoutePartKey(
        full_path=full_path,
        endpoint=_IdentityKey(route.endpoint),
        inherited_dependencies=inherited_contracts,
    )
    route_id = id(route)
    with _COMPILED_ROUTE_CONTRACTS_LOCK:
        bucket = _STABLE_ROUTE_CONTRACT_PARTS.get(route_id)
        if bucket is not None and bucket[0]() is route:
            cached = bucket[1].get(key)
            if cached is not None:
                _STABLE_ROUTE_CONTRACT_PARTS.move_to_end(route_id)
                return cached
        elif bucket is not None:
            _STABLE_ROUTE_CONTRACT_PARTS.pop(route_id, None)

    compiled = _route_contract_part(full_path, route, inherited_contracts)

    def discard(dead_ref: weakref.ReferenceType[APIRoute]) -> None:
        with _COMPILED_ROUTE_CONTRACTS_LOCK:
            current = _STABLE_ROUTE_CONTRACT_PARTS.get(route_id)
            if current is not None and current[0] is dead_ref:
                _STABLE_ROUTE_CONTRACT_PARTS.pop(route_id, None)

    with _COMPILED_ROUTE_CONTRACTS_LOCK:
        bucket = _STABLE_ROUTE_CONTRACT_PARTS.get(route_id)
        if bucket is not None and bucket[0]() is route:
            route_parts = bucket[1]
        else:
            route_parts = {}
            _STABLE_ROUTE_CONTRACT_PARTS[route_id] = (
                weakref.ref(route, discard),
                route_parts,
            )
        existing = route_parts.get(key)
        if existing is not None:
            _STABLE_ROUTE_CONTRACT_PARTS.move_to_end(route_id)
            return existing
        route_parts[key] = compiled
        _STABLE_ROUTE_CONTRACT_PARTS.move_to_end(route_id)
        while len(_STABLE_ROUTE_CONTRACT_PARTS) > _STABLE_ROUTE_PART_CACHE_SIZE:
            _STABLE_ROUTE_CONTRACT_PARTS.popitem(last=False)
    return compiled


def _route_compilation_key_uncached(
    app: FastAPI,
    walked_routes: Sequence[tuple[str, APIRoute, tuple[Any, ...]]],
    *,
    stable_route_graph: bool = False,
) -> _RouteCompilationKey:
    route_contracts = tuple(
        (
            _stable_route_contract_part(
                full_path,
                route,
                inherited_dependencies,
            )
            if stable_route_graph
            else _route_contract_part(
                full_path,
                route,
                tuple(
                    _dependency_contract(dependency)
                    for dependency in inherited_dependencies
                ),
            )
        )
        for full_path, route, inherited_dependencies in walked_routes
        if route.include_in_schema
    )
    openapi_schema = getattr(app, "openapi_schema", None)
    unique_id_function = getattr(
        app.router,
        "generate_unique_id_function",
        None,
    )
    return _RouteCompilationKey(
        routes=route_contracts,
        webhook_routes=tuple(
            _IdentityKey(route)
            for route in getattr(app.webhooks, "routes", ())
            if getattr(route, "include_in_schema", True)
        ),
        openapi_function=_openapi_callable_contract(app),
        openapi_schema=(
            _IdentityKey(openapi_schema) if openapi_schema is not None else None
        ),
        openapi_version=str(app.openapi_version),
        separate_input_output_schemas=bool(app.separate_input_output_schemas),
        unique_id_function=_IdentityKey(unique_id_function),
    )


def _route_compilation_key(
    app: FastAPI,
    walked_routes: Sequence[tuple[str, APIRoute, tuple[Any, ...]]],
    *,
    stable_route_graph: bool = False,
) -> _RouteCompilationKey:
    """Return the structural key, reusing only exact stable route declarations.

    The house app factories promise their module-level route declarations are
    immutable after construction. Their identity cache never equates distinct
    closure endpoint objects. Dynamic apps keep deriving every route part.
    """

    return _route_compilation_key_uncached(
        app,
        walked_routes,
        stable_route_graph=stable_route_graph,
    )


def _compile_route_schema(
    app: FastAPI,
    walked_routes: Sequence[tuple[str, APIRoute, tuple[Any, ...]]],
) -> _CompiledRouteSchema:
    del walked_routes  # The already-derived key owns the route contract.
    schema = app.openapi()
    return _CompiledRouteSchema(
        encoded=json.dumps(
            {
                "components": schema.get("components") or {},
                "paths": schema.get("paths") or {},
            },
            separators=(",", ":"),
        ).encode(),
    )


def _compiled_route_schema(
    app: FastAPI,
    walked_routes: Sequence[tuple[str, APIRoute, tuple[Any, ...]]],
) -> dict[str, Any]:
    key = _route_compilation_key(app, walked_routes)
    compiled = _compiled_route_schema_entry(app, walked_routes, key)
    return json.loads(compiled.encoded)


def _compiled_route_schema_entry(
    app: FastAPI,
    walked_routes: Sequence[tuple[str, APIRoute, tuple[Any, ...]]],
    key: _RouteCompilationKey,
) -> _CompiledRouteSchema:
    """Return the one published app-free cache entry for ``key``."""

    with _COMPILED_ROUTE_CONTRACTS_LOCK:
        cached = _COMPILED_ROUTE_CONTRACTS.get(key)
        if cached is not None:
            _COMPILED_ROUTE_CONTRACTS.move_to_end(key)
            return cached

    compiled = _compile_route_schema(app, walked_routes)
    with _COMPILED_ROUTE_CONTRACTS_LOCK:
        existing = _COMPILED_ROUTE_CONTRACTS.get(key)
        if existing is not None:
            _COMPILED_ROUTE_CONTRACTS.move_to_end(key)
            published = existing
        else:
            _COMPILED_ROUTE_CONTRACTS[key] = compiled
            while len(_COMPILED_ROUTE_CONTRACTS) > _COMPILED_ROUTE_CONTRACT_CACHE_SIZE:
                _COMPILED_ROUTE_CONTRACTS.popitem(last=False)
            published = compiled
    return published


def _clear_compiled_route_tool_cache() -> None:
    """Reset the bounded process cache for focused cache-contract tests."""

    with _COMPILED_ROUTE_CONTRACTS_LOCK:
        _COMPILED_ROUTE_CONTRACTS.clear()
        _STABLE_ROUTE_CONTRACT_PARTS.clear()


def _compile_route_tool_blueprints(
    schema: Mapping[str, Any],
    walked_routes: Sequence[tuple[str, APIRoute, tuple[Any, ...]]],
) -> tuple[_GeneratedRouteToolBlueprint, ...]:
    """Derive and validate the app-independent half of generated tools."""

    components = schema.get("components") or {}
    by_path_method = {
        (path, method.upper()): operation
        for path, path_item in (schema.get("paths") or {}).items()
        for method, operation in path_item.items()
        if method.upper() in _HTTP_METHODS
    }
    generated: list[_GeneratedRouteToolBlueprint] = []
    seen_operations: set[str] = set()
    seen_tools: dict[tuple[str, str], str] = {}

    for full_path, route, inherited_dependencies in walked_routes:
        if not route.include_in_schema:
            continue
        for method in sorted((route.methods or set()) & _HTTP_METHODS):
            normalized_path = re.sub(r"\{([^}:]+):[^}]+\}", r"{\1}", full_path)
            operation = by_path_method.get((normalized_path, method))
            # Starlette may add an implicit HEAD method to a GET route. It is
            # not an OpenAPI operation and therefore has no independently
            # publishable schema or operationId.
            if operation is None:
                continue
            metadata = _metadata_for(route, method)
            if metadata is None or metadata.mapping_state != "generated":
                continue
            operation_id = operation.get("operationId")
            if not isinstance(operation_id, str) or not operation_id:
                raise ConfigError(
                    "declared route tool lacks operationId: "
                    f"{method} {route.path_format}"
                )
            if operation_id in seen_operations:
                raise ConfigError(f"duplicate generated operation id {operation_id!r}")
            seen_operations.add(operation_id)
            toolset_id, tool_id = _scoped_id(
                route,
                operation,
                metadata,
                normalized_path,
            )
            args_schema, bindings = derive_argument_schema(
                operation,
                components,
                metadata,
            )
            read_only = (
                metadata.read_only
                if metadata.read_only is not None
                else method in {"GET", "HEAD"}
            )
            tool = make_tool(
                id=tool_id,
                toolset_id=toolset_id,
                purpose=_purpose(route, metadata),
                when=_use_when(route, metadata),
                args_schema=args_schema,
                examples=[],
                yields=metadata.yields,
                requires_workspace=metadata.requires_workspace,
                required_role=_required_role(
                    route,
                    metadata,
                    inherited_dependencies,
                ),
                interruptible=(
                    metadata.interruptible
                    if metadata.interruptible is not None
                    else read_only
                ),
                read_only=read_only,
            )
            scoped_id = (toolset_id, tool_id)
            previous = seen_tools.get(scoped_id)
            if previous is not None:
                raise ConfigError(
                    f"generated scoped id collision {toolset_id}__{tool_id}: "
                    f"{previous!r} and {operation_id!r}; declare an explicit override"
                )
            seen_tools[scoped_id] = operation_id
            blueprint = _GeneratedRouteToolBlueprint(
                operation_id=operation_id,
                method=method,
                path=normalized_path,
                metadata=metadata,
                bindings=bindings,
                tool_id=tool.id,
                toolset_id=tool.toolset_id,
                description=tool.description,
                args_schema_encoded=json.dumps(
                    tool.args_schema,
                    separators=(",", ":"),
                ).encode(),
                yields=tool.yields,
                requires_workspace=tool.requires_workspace,
                required_role=tool.required_role,
                tool_class=tool.tool_class,
                interruptible=tool.interruptible,
                read_only=tool.read_only,
                preview_args=tool.preview_args,
                preview_lead=tool.preview_lead,
            )
            if _tool_from_blueprint(blueprint) != tool:
                raise ConfigError(
                    f"generated tool blueprint lost fields for {toolset_id}__{tool_id}"
                )
            generated.append(blueprint)

    return tuple(sorted(generated, key=lambda item: item.operation_id))


def _compiled_route_tool_blueprints(
    key: _RouteCompilationKey,
    compiled: _CompiledRouteSchema,
    walked_routes: Sequence[tuple[str, APIRoute, tuple[Any, ...]]],
) -> tuple[_GeneratedRouteToolBlueprint, ...]:
    """Return the one published immutable blueprint tuple for ``key``."""

    if compiled.blueprints is not None:
        return compiled.blueprints

    candidate = _compile_route_tool_blueprints(
        json.loads(compiled.encoded),
        walked_routes,
    )
    with _COMPILED_ROUTE_CONTRACTS_LOCK:
        current = _COMPILED_ROUTE_CONTRACTS.get(key)
        if current is not None and current.blueprints is not None:
            _COMPILED_ROUTE_CONTRACTS.move_to_end(key)
            return current.blueprints
        encoded = current.encoded if current is not None else compiled.encoded
        published = _CompiledRouteSchema(
            encoded=encoded,
            blueprints=candidate,
        )
        _COMPILED_ROUTE_CONTRACTS[key] = published
        _COMPILED_ROUTE_CONTRACTS.move_to_end(key)
        while len(_COMPILED_ROUTE_CONTRACTS) > _COMPILED_ROUTE_CONTRACT_CACHE_SIZE:
            _COMPILED_ROUTE_CONTRACTS.popitem(last=False)
    return candidate


def _tool_from_blueprint(blueprint: _GeneratedRouteToolBlueprint) -> Tool:
    """Create the fresh mutable Tool owned by one application snapshot."""

    return Tool(
        id=blueprint.tool_id,
        toolset_id=blueprint.toolset_id,
        description=blueprint.description,
        args_schema=json.loads(blueprint.args_schema_encoded),
        examples=[],
        yields=blueprint.yields,
        requires_workspace=blueprint.requires_workspace,
        required_role=blueprint.required_role,
        tool_class=blueprint.tool_class,
        interruptible=blueprint.interruptible,
        read_only=blueprint.read_only,
        preview_args=blueprint.preview_args,
        preview_lead=blueprint.preview_lead,
    )


def build_generated_route_toolsets(
    app: FastAPI,
    *,
    publish_default_gates: bool = False,
    _stable_route_graph: bool = False,
) -> GeneratedRouteToolSnapshot:
    """Build every declared generated provider exactly once for ``app``."""

    cached = getattr(app.state, "generated_route_toolsets", None)
    if isinstance(cached, GeneratedRouteToolSnapshot):
        if publish_default_gates:
            _publish_generated_default_gates(cached)
        return cached

    _install_generated_stream_cleanup(app.routes)
    walked_routes = tuple(_walk_api_routes(app.routes))
    compilation_key = _route_compilation_key(
        app,
        walked_routes,
        stable_route_graph=_stable_route_graph,
    )
    compiled = _compiled_route_schema_entry(
        app,
        walked_routes,
        compilation_key,
    )
    blueprints = _compiled_route_tool_blueprints(
        compilation_key,
        compiled,
        walked_routes,
    )
    generated: list[GeneratedRouteTool] = []
    provider_registries: dict[str, dict[str, tuple[Tool, Callable[..., Any]]]] = {}
    owning_app_ref = weakref.ref(app)
    blueprints_by_provider: dict[str, list[_GeneratedRouteToolBlueprint]] = {}
    for blueprint in blueprints:
        blueprints_by_provider.setdefault(blueprint.toolset_id, []).append(blueprint)

    for toolset_id, provider_blueprints in sorted(blueprints_by_provider.items()):
        registry = provider_registries.setdefault(toolset_id, {})
        for blueprint in provider_blueprints:
            tool = _tool_from_blueprint(blueprint)
            descriptor = GeneratedRouteTool(
                operation_id=blueprint.operation_id,
                method=blueprint.method,
                path=blueprint.path,
                metadata=blueprint.metadata,
                bindings=blueprint.bindings,
                tool=tool,
            )
            tool_id = tool.id

            async def handler(arguments: dict[str, Any], *, ctx: ToolContext | None = None, _descriptor=descriptor) -> ToolCallResult:
                owning_app = owning_app_ref()
                if owning_app is None:
                    return ToolCallResult(
                        output=json.dumps(
                            {
                                "type": "route-tool-app-unavailable",
                                "message": "the generated REST tool's application is no longer running",
                            }
                        ),
                        is_error=True,
                    )
                if _DISPATCH_DEPTH.get() >= 1:
                    return ToolCallResult(
                        output=json.dumps({"type": "route-tool-recursion", "message": "generated route tools cannot recursively dispatch"}),
                        is_error=True,
                    )
                person = person_for_tool(ctx)
                token = _DISPATCH_DEPTH.set(_DISPATCH_DEPTH.get() + 1)
                started = time.monotonic()
                status_code: int | None = None
                outcome = "dispatch_failed"
                try:
                    path = _format_path(_descriptor.path, _descriptor.bindings, arguments)
                    query, headers, body = _request_parts(_descriptor.bindings, arguments)
                    kwargs = _body_kwargs(body)
                    merged_headers = {
                        key: value
                        for key, value in {
                            **headers,
                            **kwargs.pop("headers", {}),
                        }.items()
                        if key.lower() != "accept-encoding"
                    }
                    # The byte cap applies to the representation the ASGI app
                    # sends. Forcing identity prevents production gzip
                    # middleware from turning a tiny compressed body into a
                    # large HTTPX-decoded allocation after the counter.
                    merged_headers["accept-encoding"] = "identity"
                    transport = _BoundedAsgiTransport(
                        app=_TrustedAsgiApp(owning_app, person),
                        max_response_bytes=_MAX_RESPONSE_BYTES,
                    )
                    async with httpx.AsyncClient(transport=transport, base_url="http://primer.internal") as client:
                        async with asyncio.timeout(_DISPATCH_TIMEOUT_S):
                            async with client.stream(
                                _descriptor.method,
                                path,
                                params=query,
                                headers=merged_headers,
                                **kwargs,
                            ) as response:
                                status_code = response.status_code
                                content = bytearray()
                                async for chunk in response.aiter_bytes():
                                    content.extend(chunk)
                                result = _response_result(
                                    status_code=response.status_code,
                                    headers=response.headers,
                                    content=bytes(content),
                                )
                    outcome = "ok" if status_code < 400 else "http_error"
                    logger.info(
                        "generated route tool dispatched",
                        extra={
                            "tool_scoped_id": f"{_descriptor.tool.toolset_id}__{_descriptor.tool.id}",
                            "operation_id": _descriptor.operation_id,
                            "status_code": status_code,
                            "actor_user_id": person.user_id,
                            "actor_role": person.role,
                        },
                    )
                    return result
                except _ResponseEncodingRejected as exc:
                    status_code = exc.status_code
                    outcome = "response_encoding_rejected"
                    return ToolCallResult(
                        output=json.dumps(
                            {
                                "type": "route-tool-response-encoding-rejected",
                                "message": (
                                    "generated REST response uses an unsupported "
                                    "content encoding"
                                ),
                                "content_encoding": exc.content_encoding,
                            },
                            separators=(",", ":"),
                        ),
                        is_error=True,
                    )
                except _ResponseLimitExceeded as exc:
                    status_code = exc.status_code
                    outcome = "response_too_large"
                    return ToolCallResult(
                        output=json.dumps(
                            {
                                "type": "route-tool-response-too-large",
                                "message": "generated REST response exceeds the tool limit",
                                "limit_bytes": _MAX_RESPONSE_BYTES,
                            },
                            separators=(",", ":"),
                        ),
                        is_error=True,
                    )
                except TimeoutError:
                    outcome = "timeout"
                    logger.warning("generated route tool timed out", extra={"operation_id": _descriptor.operation_id})
                    return ToolCallResult(
                        output=json.dumps({"type": "route-tool-timeout", "message": "generated REST dispatch timed out"}),
                        is_error=True,
                    )
                finally:
                    audit_logger.info(
                        "route_tool.invoke",
                        extra={
                            "tool_scoped_id": f"{_descriptor.tool.toolset_id}__{_descriptor.tool.id}",
                            "operation_id": _descriptor.operation_id,
                            "actor_user_id": person.user_id,
                            "actor_role": person.role,
                            "status_code": status_code,
                            "outcome": outcome,
                            "duration_ms": round((time.monotonic() - started) * 1000, 2),
                        },
                    )
                    _DISPATCH_DEPTH.reset(token)

            registry[tool_id] = (tool, handler)
            generated.append(descriptor)

    providers = {
        provider_id: InternalToolsetProvider(provider_id, registry)
        for provider_id, registry in sorted(provider_registries.items())
    }
    snapshot = GeneratedRouteToolSnapshot(
        operations=tuple(sorted(generated, key=lambda item: item.operation_id)),
        providers=MappingProxyType(providers),
    )
    if publish_default_gates:
        _publish_generated_default_gates(snapshot)
    app.state.generated_route_toolsets = snapshot
    global _LATEST_SNAPSHOT
    _LATEST_SNAPSHOT = snapshot
    return snapshot


def _publish_generated_default_gates(snapshot: GeneratedRouteToolSnapshot) -> None:
    """Publish production mutators into the existing shipped-gate source."""

    from primer.toolset.crud import register_generated_default_gates

    register_generated_default_gates(
        frozenset(
            (item.tool.toolset_id, item.tool.id)
            for item in snapshot.operations
            if not item.tool.read_only
        )
    )


def generated_route_toolset_providers() -> Mapping[str, InternalToolsetProvider]:
    """Return the providers from the most recent deterministic app build."""

    if _LATEST_SNAPSHOT is None:
        raise ConfigError("generated route tools have not been built")
    return _LATEST_SNAPSHOT.providers


__all__ = [
    "CrudToolDeclaration",
    "GeneratedRouteTool",
    "GeneratedRouteToolSnapshot",
    "RouteToolMetadata",
    "attach_route_tool_metadata",
    "build_generated_route_toolsets",
    "canonical_tool_id",
    "derive_argument_schema",
    "generated_route_toolset_providers",
    "is_trusted_route_tool_scope",
    "route_tool",
    "route_tool_metadata",
    "trusted_route_tool_person",
]

"""RFC 7807 problem+json error model and FastAPI handler registration.

Single ``ProblemDetails`` Pydantic model + an exception-handler chain
that maps every :class:`primer.model.except_.PrimerError` subclass to
the right HTTP status + problem-type URI. Type URIs are relative
(``/errors/<code>``) per RFC 7807 §3.1.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException
from primer.model.except_ import (
    AuthenticationError,
    AuthRequiredError,
    BadRequestError,
    ConfigError,
    ConflictError,
    DimensionMismatchError,
    PrimerError,
    ModelNotFoundError,
    NetworkError,
    NotFoundError,
    ProviderError,
    RateLimitError,
    ServerError,
    ToolsetUnreachableError,
    UnsupportedContentError,
    ValidationError,
)
from primer.model.problem_details import ProblemDetails


logger = logging.getLogger(__name__)


PROBLEM_JSON_MEDIA_TYPE = "application/problem+json"


# Order matters: handlers are checked most-specific first. We list the
# more-derived classes BEFORE their bases so a PrimerError-base lookup
# falls through to the most specific match.
_PRIMER_ERROR_MAP: list[tuple[type[PrimerError], int, str, str]] = [
    (BadRequestError, 400, "/errors/bad-request", "Bad Request"),
    # ToolsetUnreachableError subclasses BadRequestError but carries its own
    # type URI so the Console can detect it precisely and offer "Create
    # anyway". Dispatch is by exception class + MRO (not list order), so it
    # still gets its own handler; it is placed AFTER BadRequestError so the
    # shared 400 response description in _RESPONSES_BY_CODE stays the generic
    # "Bad Request" that every other 400 route documents.
    (ToolsetUnreachableError, 400, "/errors/toolset-unreachable", "Toolset Unreachable"),
    (AuthenticationError, 401, "/errors/authentication-failed", "Authentication Failed"),
    (AuthRequiredError, 401, "/errors/auth-required", "Authentication Required"),
    (ModelNotFoundError, 404, "/errors/model-not-found", "Model Not Found"),
    (NotFoundError, 404, "/errors/not-found", "Not Found"),
    (ConflictError, 409, "/errors/conflict", "Conflict"),
    (RateLimitError, 429, "/errors/rate-limited", "Rate Limited"),
    (DimensionMismatchError, 422, "/errors/dimension-mismatch", "Dimension Mismatch"),
    (ValidationError, 422, "/errors/validation-error", "Validation Error"),
    (UnsupportedContentError, 422, "/errors/unsupported-content", "Unsupported Content"),
    (ServerError, 502, "/errors/provider-server-error", "Provider Server Error"),
    (ProviderError, 502, "/errors/provider-error", "Provider Error"),
    (NetworkError, 504, "/errors/network-error", "Network Error"),
    (ConfigError, 503, "/errors/service-unavailable", "Service Unavailable"),
    (PrimerError, 500, "/errors/internal", "Internal Error"),
]


_RESPONSES_BY_CODE: dict[int, dict[str, Any]] = {}
for _exc, _status, _uri, _title in _PRIMER_ERROR_MAP:
    _RESPONSES_BY_CODE.setdefault(
        _status,
        {
            "model": ProblemDetails,
            "description": _title,
            "content": {PROBLEM_JSON_MEDIA_TYPE: {}},
        },
    )
_RESPONSES_BY_CODE.setdefault(
    422,
    {
        "model": ProblemDetails,
        "description": "Validation Error",
        "content": {PROBLEM_JSON_MEDIA_TYPE: {}},
    },
)


def common_responses(*codes: int) -> dict[int, dict[str, Any]]:
    """Return a FastAPI ``responses=`` map for the given status codes.

    Use on every route that can return one of the documented error codes
    so OpenAPI shows the ProblemDetails schema for each.

    Raises
    ------
    KeyError
        ``codes`` references a status code not in the error map.
    """
    return {code: _RESPONSES_BY_CODE[code] for code in codes}


def _problem_response(
    *,
    request: Request,
    status: int,
    type_uri: str,
    title: str,
    detail: str,
    extensions: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    # Thread the request-id from request.state (set by the
    # _install_request_id middleware in primer.api.app) into the
    # envelope's extensions so the UI's "Copy request id" link works
    # on the 4xx/5xx path. Skipped silently when the middleware is
    # absent (e.g. a unit test that constructs a Request without the
    # production app factory) so existing tests remain green.
    rid = getattr(getattr(request, "state", None), "request_id", None)
    if rid:
        extensions = {**(extensions or {}), "request_id": rid}
    problem = ProblemDetails(
        type=type_uri,
        title=title,
        status=status,
        detail=detail,
        instance=request.url.path,
        extensions=extensions,
    )
    return JSONResponse(
        status_code=status,
        content=problem.model_dump(exclude_none=True),
        media_type=PROBLEM_JSON_MEDIA_TYPE,
        headers=headers,
    )


def _make_primer_error_handler(status: int, type_uri: str, title: str):
    async def _handler(request: Request, exc: PrimerError) -> JSONResponse:
        extensions: dict[str, Any] | None = None
        if isinstance(exc, AuthRequiredError):
            extensions = {"auth_url": exc.auth_url}
        return _problem_response(
            request=request,
            status=status,
            type_uri=type_uri,
            title=title,
            detail=exc.message,
            extensions=extensions,
        )

    return _handler


def _jsonable(value: object) -> object:
    """Recursively coerce a Pydantic-error value tree into JSON-safe
    types. Pydantic's `errors()` may surface raw `bytes` (e.g. when
    the request body could not be decoded as JSON) or exception
    objects inside `ctx` (when a model_validator raised ValueError)
    which would crash JSONEncoder when the envelope is rendered."""
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return repr(value)
    if isinstance(value, BaseException):
        return str(value)
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    # Anything else (custom objects) — fall back to repr to avoid
    # crashing the response renderer.
    return repr(value)


async def _validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    return _problem_response(
        request=request,
        status=422,
        type_uri="/errors/validation-error",
        title="Validation Error",
        detail="One or more request parameters or body fields failed validation.",
        extensions={"errors": _jsonable(exc.errors())},
    )


# Status -> (type URI, title) for raw HTTPExceptions. Reuses the type-URI
# vocabulary of _PRIMER_ERROR_MAP so there is exactly ONE envelope shape and
# one set of problem types across the API. Deliberately an explicit table
# rather than a lookup derived from _PRIMER_ERROR_MAP: that list is ordered
# for OpenAPI/MRO purposes and its first 404 row is ModelNotFoundError, which
# would mistype a plain 404 (e.g. an unknown route) as /errors/model-not-found.
_HTTP_STATUS_PROBLEM: dict[int, tuple[str, str]] = {
    400: ("/errors/bad-request", "Bad Request"),
    401: ("/errors/authentication-failed", "Authentication Failed"),
    403: ("/errors/forbidden", "Forbidden"),
    404: ("/errors/not-found", "Not Found"),
    409: ("/errors/conflict", "Conflict"),
    422: ("/errors/validation-error", "Validation Error"),
    429: ("/errors/rate-limited", "Rate Limited"),
    500: ("/errors/internal", "Internal Error"),
    502: ("/errors/provider-error", "Provider Error"),
    503: ("/errors/service-unavailable", "Service Unavailable"),
    504: ("/errors/network-error", "Network Error"),
}


def _problem_for_status(status: int) -> tuple[str, str]:
    """Map an HTTP status onto its (type URI, title).

    Falls back to the IANA reason phrase for codes outside the table
    (e.g. 405, 501) so every status still gets a stable, sane type URI.
    """
    known = _HTTP_STATUS_PROBLEM.get(status)
    if known is not None:
        return known
    try:
        phrase = HTTPStatus(status).phrase
    except ValueError:  # non-standard status code
        return "/errors/unknown", "Error"
    return f"/errors/{phrase.lower().replace(' ', '-')}", phrase


def _detail_from_mapping(payload: Mapping[str, Any], fallback: str) -> str:
    """Reduce a dict ``detail`` to RFC 7807's string ``detail``.

    Several routers raise ``HTTPException(detail={"code": ..., "message":
    ...})``. Prefer a human-readable message when one is carried;
    otherwise fall back to the machine code (still far more useful than a
    stringified dict), then to the status title. The full mapping is kept
    verbatim in ``extensions``, so nothing is lost either way.
    """
    for key in ("message", "detail", "description"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    for key in ("code", "error"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return fallback


async def _http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> Response:
    """Render Starlette/FastAPI ``HTTPException`` as problem+json.

    Without this, every raw ``raise HTTPException(...)`` -- including all
    of require_auth / require_user / require_admin / require_scope and the
    whole auth router -- would bypass the RFC 7807 envelope and return
    FastAPI's default ``{"detail": ...}`` as application/json, contrary to
    the documented contract that every error is application/problem+json.
    """
    headers = getattr(exc, "headers", None)
    # 204/304 carry no body by definition; a problem envelope there would
    # violate the HTTP spec. Mirror Starlette's own default handling.
    if exc.status_code in (204, 304):
        return Response(status_code=exc.status_code, headers=headers)

    type_uri, title = _problem_for_status(exc.status_code)
    detail = exc.detail
    extensions: dict[str, Any] | None = None
    if isinstance(detail, Mapping):
        extensions = {k: _jsonable(v) for k, v in detail.items()}
        detail_str = _detail_from_mapping(detail, title)
    elif isinstance(detail, str) and detail:
        detail_str = detail
    else:
        detail_str = title

    return _problem_response(
        request=request,
        status=exc.status_code,
        type_uri=type_uri,
        title=title,
        detail=detail_str,
        extensions=extensions,
        headers=headers,
    )


async def _bare_exception_handler(
    request: Request, exc: Exception
) -> JSONResponse:
    logger.exception(
        "unhandled exception in API request",
        extra={"path": request.url.path, "exception_type": type(exc).__name__},
    )
    return _problem_response(
        request=request,
        status=500,
        type_uri="/errors/internal",
        title="Internal Error",
        detail="An unexpected error occurred. The incident has been logged.",
    )


def register_error_handlers(app: FastAPI) -> None:
    """Install one exception handler per row in the error map.

    The bare-exception handler is registered against HTTP status code
    500 (not the ``Exception`` class) because Starlette's
    ``ServerErrorMiddleware`` lives outside ``ExceptionMiddleware`` and
    only consults the status-code registry, not the class registry,
    for unhandled exceptions.
    """
    for exc_cls, status, type_uri, title in _PRIMER_ERROR_MAP:
        app.add_exception_handler(
            exc_cls, _make_primer_error_handler(status, type_uri, title)
        )
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    # Starlette's HTTPException (FastAPI's subclasses it, so this covers both).
    # Overrides the framework default, which renders `{"detail": ...}` as
    # application/json and would otherwise escape the problem+json contract on
    # every raw `raise HTTPException(...)` -- including all auth/authz
    # rejections and 404-for-unknown-route.
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    # Status-code 500 override; this is what catches unhandled non-PrimerError
    # exceptions inside ServerErrorMiddleware.
    app.add_exception_handler(500, _bare_exception_handler)


__all__ = [
    "PROBLEM_JSON_MEDIA_TYPE",
    "ProblemDetails",
    "common_responses",
    "register_error_handlers",
]

"""The /svc serving plane: static bundles for published services.

Mounted WITHOUT the /v1 prefix (these are app URLs, not API resources).
Auth is per-service: ``viewer_auth == CONSOLE`` applies the same
``require_user`` gate the entity routers use; ``NONE`` serves
anonymously (the auth middleware only populates identity, it never
blocks). Spec section 6; the functions gateway (section 7) is appended
by plan task 8.
"""

from __future__ import annotations

import mimetypes

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from primer.api.deps import get_artifact_storage_registry, require_user
from primer.model.service import ServiceViewerAuth
from primer.service.client_js import PRIMER_JS
from primer.service.gateway import (
    ArgsInvalid,
    FunctionNotFound,
    FunctionRaised,
    RunnerUnavailable,
    call_function,
)
from primer.service.serve import get_artifact_lru, get_resolver, pick_path

svc_serve_router = APIRouter(tags=["svc"])

_IMMUTABLE = "public, max-age=31536000, immutable"


def _wants_html(request: Request) -> bool:
    return "text/html" in (request.headers.get("accept") or "")


def _not_found(request: Request, title: str, body_html: str) -> Response:
    if _wants_html(request):
        return HTMLResponse(
            "<!doctype html><meta charset='utf-8'>"
            f"<title>{title}</title>"
            "<body style='font-family:system-ui;display:grid;"
            "place-items:center;min-height:90vh'>"
            f"<div style='text-align:center'>{body_html}</div>",
            status_code=404,
        )
    return JSONResponse(
        status_code=404,
        content={"type": "/errors/not-found", "title": title, "status": 404},
        media_type="application/problem+json",
    )


# Declared BEFORE the generic /svc/{name} routes: FastAPI matches in
# declaration order, and "_client" must never be treated as a service
# name (it is also a reserved slug at the model layer).
@svc_serve_router.get("/svc/_client/primer.js", include_in_schema=False)
async def svc_client_js() -> Response:
    return Response(
        content=PRIMER_JS,
        media_type="application/javascript",
        headers={"Cache-Control": "public, max-age=300"},
    )


@svc_serve_router.get("/svc/{name}", include_in_schema=False)
async def svc_root_redirect(name: str) -> RedirectResponse:
    return RedirectResponse(url=f"/svc/{name}/", status_code=307)


@svc_serve_router.post(
    "/svc/{name}/_gateway/functions/{fn}",
    summary="Call a bundle function in the python-runner sandbox",
)
async def svc_gateway_function(
    name: str, fn: str, request: Request
) -> Response:
    resolved = await get_resolver(request).resolve(name)
    if resolved is None or resolved[1] is None:
        return JSONResponse(
            status_code=404,
            content={
                "type": "/errors/not-found",
                "title": "no such service" if resolved is None else "not published",
                "status": 404,
            },
            media_type="application/problem+json",
        )
    service, version = resolved
    if service.viewer_auth is ServiceViewerAuth.CONSOLE:
        require_user(request)
    try:
        args = await request.json()
    except Exception:  # noqa: BLE001 - malformed body is a client error
        args = None
    if not isinstance(args, dict):
        return JSONResponse(
            status_code=422,
            content={
                "type": "/errors/invalid-args",
                "title": "the request body must be a JSON object of arguments",
                "status": 422,
            },
            media_type="application/problem+json",
        )
    spec_file = next(
        (s.source_file for s in version.functions if s.name == fn), None
    )
    source = ""
    if spec_file is not None:
        artifacts = await get_artifact_storage_registry(request).get_default()
        blob = await get_artifact_lru(request).get(
            artifacts, version.files[spec_file]
        )
        if blob is None:
            return JSONResponse(
                status_code=503,
                content={
                    "type": "/errors/subsystem-inactive",
                    "title": "the function source is missing its stored bytes",
                    "status": 503,
                },
                media_type="application/problem+json",
            )
        source = blob.data.decode("utf-8")
    try:
        value = await call_function(
            service_id=service.id,
            version=version,
            source=source,
            fn=fn,
            args=args,
        )
    except FunctionNotFound as exc:
        return JSONResponse(
            status_code=404,
            content={
                "type": "/errors/not-found",
                "title": str(exc),
                "status": 404,
            },
            media_type="application/problem+json",
        )
    except ArgsInvalid as exc:
        return JSONResponse(
            status_code=422,
            content={
                "type": "/errors/invalid-args",
                "title": "arguments do not match the published schema",
                "status": 422,
                "errors": exc.errors,
            },
            media_type="application/problem+json",
        )
    except RunnerUnavailable as exc:
        return JSONResponse(
            status_code=503,
            content={
                "type": "/errors/subsystem-inactive",
                "title": f"the sandbox runner is unavailable: {exc}",
                "status": 503,
            },
            media_type="application/problem+json",
        )
    except FunctionRaised as exc:
        body = {
            "type": "/errors/function-raised",
            "title": f"{exc.type_}: {exc.message}",
            "status": 500,
        }
        # Tracebacks are developer data: only console-authenticated
        # services expose them (spec section 7.1).
        if service.viewer_auth is ServiceViewerAuth.CONSOLE:
            body["traceback"] = exc.traceback
        return JSONResponse(
            status_code=500, content=body, media_type="application/problem+json"
        )
    return JSONResponse(status_code=200, content=value)


@svc_serve_router.get("/svc/{name}/{path:path}", summary="Serve a service asset")
async def svc_serve(name: str, path: str, request: Request) -> Response:
    resolved = await get_resolver(request).resolve(name)
    if resolved is None:
        return _not_found(
            request, "no such service",
            f"<h1>{name}</h1><p>No service is registered under this name.</p>",
        )
    service, version = resolved
    if service.viewer_auth is ServiceViewerAuth.CONSOLE:
        require_user(request)
    if version is None:
        return _not_found(
            request, "not published",
            f"<h1>{service.name}</h1><p>This service is not published yet.</p>",
        )
    target = pick_path(version, path)
    if target is None:
        return _not_found(
            request, "asset not found",
            f"<h1>{service.name}</h1><p>{path} does not exist in this version.</p>",
        )
    artifact_id = version.files[target]
    etag = f'"{artifact_id}"'
    base_headers = {"ETag": etag, "Cache-Control": _IMMUTABLE}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=base_headers)
    artifacts = await get_artifact_storage_registry(request).get_default()
    blob = await get_artifact_lru(request).get(artifacts, artifact_id)
    if blob is None:
        return _not_found(
            request, "asset not found",
            f"<h1>{service.name}</h1><p>{path} is missing its stored bytes.</p>",
        )
    if target == version.manifest.entry:
        media = "text/html"
    else:
        media = mimetypes.guess_type(target)[0] or "application/octet-stream"
    return Response(content=blob.data, media_type=media, headers=base_headers)


__all__ = ["svc_serve_router"]

"""The canvas HTTP surface (pivot P5, Tasks 13 to 15; design 8.6).

``canvas_public_router`` holds the routes the sandboxed frame loads, mounted OUTSIDE ``/v1`` with no auth dependency (the frame is a
cross-origin document, so it sends no cookies) and never in WORKER or serve-only mode:

* ``GET /canvas/frame``: the static frame shell with its own CSP (``primer.canvas.frame``);
* ``GET /canvas/sessions/{session_id}/files/{token}``: a workspace file the session's canvas shows, by capability URL
  (``primer.canvas.files``). Every refusal is the same 404, so it tells a caller nothing about which check failed.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import Response

from primer.api.deps import get_storage_provider, get_workspace_registry
from primer.canvas import files as canvas_files
from primer.canvas.frame import build_frame_shell, frame_headers, own_origin_of
from primer.model.canvas import SessionCanvas
from primer.model.except_ import BadRequestError, NotFoundError
from primer.model.workspace_session import WorkspaceSession
from primer.workspace.reserved import reserved_tree_for

canvas_public_router = APIRouter(tags=["canvas"])

#: The one answer of every refused file URL.
FILE_REFUSED = "This canvas file link is not valid, or it has expired."


def _ui_dir():
    """The console's ``ui/`` (read when asked, so a test that points the console elsewhere is followed)."""
    from primer.api import _app_middleware

    return _app_middleware._UI_DIR


@canvas_public_router.get("/canvas/frame", include_in_schema=False)
async def canvas_frame(request: Request, v: str | None = Query(default=None, max_length=64)) -> Response:
    """The frame shell. Cached for a year when ``v`` is the current version; never cached otherwise."""
    shell = build_frame_shell(_ui_dir())
    return Response(
        content=shell.body,
        media_type="text/html; charset=utf-8",
        headers=frame_headers(
            version_ok=v == shell.version, own_origin=own_origin_of(request.url.scheme, request.headers.get("host")),
        ),
    )


def _file_refused() -> HTTPException:
    return HTTPException(status_code=404, detail=FILE_REFUSED)


def _too_large() -> HTTPException:
    return HTTPException(
        status_code=413,
        detail={
            "code": "canvas_file_too_large",
            "message": f"The canvas shows workspace files of at most {canvas_files.MAX_FILE_BYTES} bytes.",
        },
    )


def _file_headers(media_type: str) -> dict[str, str]:
    """A served file: for the sandboxed frame (another origin), kept private and for as long as its URL lives, never run as a page."""
    return {
        "Content-Type": media_type,
        "Cache-Control": f"private, max-age={canvas_files.FILE_URL_TTL_S}",
        "Cross-Origin-Resource-Policy": "cross-origin",
        "Content-Security-Policy": "default-src 'none'; sandbox",
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": "inline",
        "Referrer-Policy": "no-referrer",
    }


@canvas_public_router.get("/canvas/sessions/{session_id}/files/{token}", include_in_schema=False)
async def canvas_file(
    request: Request,
    session_id: str = Path(..., max_length=256),
    token: str = Path(..., max_length=2048),
    storage_provider: Any = Depends(get_storage_provider),
    registry: Any = Depends(get_workspace_registry),
) -> Response:
    """The workspace file a capability URL names (see ``primer.canvas.files`` for the checks, in this order)."""
    verified = canvas_files.verify_file_token(
        canvas_files.canvas_file_secret(request.app.state), token, max_age=canvas_files.FILE_URL_TTL_S,
    )
    if verified is None or verified[0] != session_id:
        raise _file_refused()
    path = verified[1]
    media_type = canvas_files.media_type_of(path)
    if media_type is None or not canvas_files.safe_relative_path(path):
        raise _file_refused()
    session = await storage_provider.get_storage(WorkspaceSession).get(session_id)
    canvas = await storage_provider.get_storage(SessionCanvas).get(session_id)
    if session is None or canvas is None or path not in canvas_files.referenced_files(canvas.elements):
        raise _file_refused()
    try:
        workspace = await registry.get_workspace(session.workspace_id)
        if reserved_tree_for(workspace, path) is not None:
            raise _file_refused()
        entry = await workspace.file_info(path)
        if entry.kind != "file":
            raise _file_refused()
        if entry.size_bytes > canvas_files.MAX_FILE_BYTES:
            raise _too_large()
        raw = await workspace.read_file(path)
    except (NotFoundError, BadRequestError):
        raise _file_refused() from None
    if len(raw) > canvas_files.MAX_FILE_BYTES:
        raise _too_large()
    return Response(content=raw, media_type=media_type, headers=_file_headers(media_type))


__all__ = ["FILE_REFUSED", "canvas_public_router"]

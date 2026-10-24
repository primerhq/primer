"""The canvas HTTP surface (pivot P5, Tasks 13 to 15; design 8.6).

``canvas_public_router`` holds the routes the sandboxed frame loads, mounted OUTSIDE ``/v1`` with no auth dependency (the frame is a
cross-origin document, so it sends no cookies) and never in WORKER or serve-only mode:

* ``GET /canvas/frame``: the static frame shell with its own CSP (``primer.canvas.frame``).
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response

from primer.canvas.frame import build_frame_shell, frame_headers, own_origin_of

canvas_public_router = APIRouter(tags=["canvas"])


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


__all__ = ["canvas_public_router"]

"""REST routes for the user-facing documentation system.

Mounts ``/v1/user_docs/manifest``, ``/v1/user_docs/{slug:path}``, and
``/v1/user_docs/embeds/manifest``. See spec section 5.2 for the
contract.

The router pulls the live :class:`UserDocsService` instance off
``app.state.user_docs_service`` (set in the lifespan handler in
``primer.api.app``). Hot-reload semantics are owned by the service; the
router is a thin HTTP adapter.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request


logger = logging.getLogger(__name__)


user_docs_router = APIRouter(tags=["user-docs"])


def _service(request: Request):
    svc = getattr(request.app.state, "user_docs_service", None)
    if svc is None:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "subsystem_not_bootstrapped",
                "message": (
                    "user docs service not initialised; check server "
                    "lifespan logs"
                ),
            },
        )
    return svc


@user_docs_router.get(
    "/user_docs/manifest",
    summary="Section tree + per-doc metadata for nav and search index",
)
async def get_manifest(request: Request) -> dict[str, Any]:
    svc = _service(request)
    return {"sections": svc.list_sections()}


@user_docs_router.get(
    "/user_docs/embeds/manifest",
    summary="Registered React embed ids (for lint + render-time checks)",
)
async def get_embeds_manifest(request: Request) -> dict[str, Any]:
    ids = getattr(request.app.state, "user_docs_embeds", []) or []
    return {"embeds": [{"id": i} for i in ids]}


@user_docs_router.get(
    "/user_docs/{slug:path}",
    summary="One doc's source + parsed frontmatter + headings",
)
async def get_doc(slug: str, request: Request) -> dict[str, Any]:
    svc = _service(request)
    entry = svc.get_doc(slug)
    if entry is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "not_found",
                "message": f"no doc at slug {slug!r}",
            },
        )
    return {
        "slug": entry.slug,
        "section": entry.section,
        "title": entry.title,
        "summary": entry.summary,
        "source": entry.body,
        "frontmatter": entry.frontmatter,
        "headings": entry.headings,
    }


__all__ = ["user_docs_router"]

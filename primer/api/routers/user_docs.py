"""REST routes for the user-facing documentation system.

Mounts ``/v1/user_docs/manifest``, ``/v1/user_docs/{slug:path}``,
``/v1/user_docs/embeds/manifest``, and
``/v1/user_docs/_fixtures/{name}.json``. See spec section 5.2 for the
contract.

The router pulls the live :class:`UserDocsService` instance off
``app.state.user_docs_service`` (set in the lifespan handler in
``primer.api.app``). Hot-reload semantics are owned by the service; the
router is a thin HTTP adapter.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import primer
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse


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
    "/user_docs/_ai/{slug}",
    summary="Mirror an agent-facing doc as if it were a user doc",
)
async def get_ai_doc_mirror(slug: str, request: Request) -> dict[str, Any]:
    svc = _service(request)
    data = svc.get_ai_doc(slug)
    if data is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "not_found",
                "message": f"no AI doc at docs/agents/{slug}.md",
            },
        )
    return data


_FIXTURES_DIR = Path(primer.__file__).resolve().parent / "user_docs" / "_fixtures"


@user_docs_router.get(
    "/user_docs/_fixtures/{name}",
    summary="Serve a fixture JSON file for docs embed previews",
)
async def get_fixture(name: str) -> JSONResponse:
    # Guard against path traversal: reject names containing '..' or '/'.
    if ".." in name or "/" in name:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_name", "message": "Fixture name must not contain '..' or '/'."},
        )
    # The route captures everything after /_fixtures/ including the .json
    # suffix. Validate the name ends with .json and strip it for the stem.
    if not name.endswith(".json"):
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": f"No fixture named {name!r}."},
        )
    fixture_path = _FIXTURES_DIR / name
    if not fixture_path.exists() or not fixture_path.is_file():
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": f"No fixture named {name!r}."},
        )
    data = json.loads(fixture_path.read_text(encoding="utf-8"))
    return JSONResponse(content=data)


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

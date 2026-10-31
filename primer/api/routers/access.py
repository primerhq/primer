"""The access sub-resource of every owned entity, and the user lookup the Share dialog types into (spec 6.2, 9).

Thin routes over :mod:`primer.access.service`; the system tools call the same service.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Body, Depends, Path, Query, Request

from primer.access.person import person_from_request
from primer.access.service import LOOKUP_MAX, AccessUpdate, AccessView, UserRef, get_access_view, lookup_users, set_access
from primer.api.deps import get_storage_provider
from primer.api.errors import common_responses
from primer.model.user import User


def _users(request: Request):
    return get_storage_provider(request).get_storage(User)


def _harness_exists(request: Request):
    from primer.model.harness import Harness

    async def _exists(harness_id: str) -> bool:
        return await get_storage_provider(request).get_storage(Harness).get(harness_id) is not None

    return _exists


def make_access_router(
    *,
    model_cls: type,
    plural: str,
    storage_dep: Callable[..., Any],
    tag: str,
    allow_public_tools: bool = False,
    after_write: Callable[[Request, Any], Awaitable[None]] | None = None,
    use_only: bool = False,
) -> APIRouter:
    """``GET`` / ``PUT /{plural}/{entity_id}/access`` for an owned model. Include it BEFORE the model's CRUD router.

    ``after_write(request, row)`` runs once a PUT has landed (a harness fans its access out to what it installed). ``use_only`` is the
    grantable-infrastructure shape (:func:`primer.access.service.set_access`): the route is on the user tier, and the service lets only
    admins change it.
    """
    router = APIRouter(tags=[tag])
    name = model_cls.__name__

    @router.get(
        f"/{plural}/{{entity_id}}/access",
        response_model=AccessView,
        summary=f"Who {name} is shared with",
        responses=common_responses(404, 500),
    )
    async def _get_access(
        request: Request,
        entity_id: str = Path(..., description="Entity id."),
        storage=Depends(storage_dep),
        users=Depends(_users),
    ) -> AccessView:
        return await get_access_view(
            storage, users, entity_id, person_from_request(request), what=f"{name} {entity_id!r}", use_only=use_only,
        )

    @router.put(
        f"/{plural}/{{entity_id}}/access",
        response_model=AccessView,
        summary=f"Change who {name} is shared with (owners only)",
        responses=common_responses(403, 404, 409, 422, 500),
    )
    async def _put_access(
        request: Request,
        entity_id: str = Path(..., description="Entity id."),
        body: AccessUpdate = Body(...),
        storage=Depends(storage_dep),
        users=Depends(_users),
    ) -> AccessView:
        return await set_access(
            storage,
            users,
            entity_id,
            person_from_request(request),
            body,
            what=f"{name} {entity_id!r}",
            allow_public_tools=allow_public_tools,
            after_write=(lambda row: after_write(request, row)) if after_write is not None else None,
            use_only=use_only,
            harness_exists=_harness_exists(request),
        )

    return router


users_lookup_router = APIRouter(tags=["users"])


@users_lookup_router.get(
    "/users/lookup",
    response_model=list[UserRef],
    summary="Find users to share with by username prefix",
    responses=common_responses(403, 500),
)
async def _lookup_users(
    request: Request,
    q: str = Query(default="", max_length=64, description="Username prefix, case-insensitive."),
    limit: int = Query(default=10, ge=1, le=LOOKUP_MAX),
    users=Depends(_users),
) -> list[UserRef]:
    return await lookup_users(users, person_from_request(request), q, limit)


__all__ = ["make_access_router", "users_lookup_router"]

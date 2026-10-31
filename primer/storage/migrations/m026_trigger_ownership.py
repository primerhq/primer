"""Migration 26: give existing triggers an owner, and every subscription its trigger's owner (access-control Phase 3, Task 4).

Before access control a trigger recorded who saved it (``Trigger.owner``, a principal the fires run as) but nothing about who
may see or change it. A trigger whose ``access`` block is absent or empty (no owners, no grants, not public) gets one:

* the user behind its run-as owner owns it: a ``user`` owner is that user, an ``api_token`` owner the token's user, when that
  user still exists and is enabled;
* otherwise (a ``system`` owner, no owner, or an owner that no longer resolves) the first admin owns it (the enabled admin
  ``User`` with the oldest ``created_at``); with no such admin the row is left as it is (admins reach every row regardless).

A subscription saved before owners were recorded (``owner`` absent) gets its trigger's run-as owner, or the first admin when the
trigger has none, so that owner can see and manage it, AND it is disabled (``enabled=false``): nobody recorded who saved it, so
it may have been planted by another user, and the adopted identity would hand it the owner's role and reach (their private
workspaces and sessions). The trigger's owner reviews it and re-enables it; the re-enable is an ordinary update, which needs
full on the trigger and makes the re-enabler the subscription's owner. After this step the fire-time check refuses a
subscription with no owner (:func:`primer.trigger.reach.refuse_fire_out_of_reach`, fail closed).

A row that already carries owners, grants or ``public`` (a subscription that already has an owner) is left alone, so re-running
converges. Rows are read in full before any write, through migration-local views with ``extra="allow"``, as ``m021`` does.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from pydantic import ConfigDict

from primer.int.storage_provider import StorageProvider
from primer.model.common import Identifiable
from primer.model.storage import OffsetPage

logger = logging.getLogger(__name__)

_PAGE = 200


class _View(Identifiable):
    model_config = ConfigDict(extra="allow")


# The class name selects the table (the storage layer lowercases it).
class Trigger(_View):  # noqa: N801
    access: dict[str, Any] | None = None
    owner: dict[str, Any] | None = None


class Subscription(_View):  # noqa: N801
    trigger_id: str
    owner: dict[str, Any] | None = None
    enabled: bool = True


class User(_View):  # noqa: N801
    username: str | None = None
    role: str = "user"
    disabled: bool = False
    created_at: datetime | None = None


class ApiToken(_View):  # noqa: N801
    user_id: str | None = None


def _owned_by(user_id: str) -> dict[str, Any]:
    return {"owners": [user_id], "grants": {}, "public": False, "public_tools": [], "members": [f"u:{user_id}"], "rev": 0}


def _unset(access: dict[str, Any] | None) -> bool:
    return not access or not (access.get("owners") or access.get("grants") or access.get("public"))


async def _read_all(store) -> list[Any]:
    rows: list[Any] = []
    offset = 0
    while True:
        page = await store.list(OffsetPage(offset=offset, length=_PAGE))
        rows.extend(page.items)
        if len(page.items) < _PAGE:
            return rows
        offset += _PAGE


class M026TriggerOwnership:
    """Stamp an owner on every trigger that has none; give every ownerless subscription its trigger's owner."""

    version = 26
    description = "give existing triggers an owner and every subscription its trigger's owner"

    async def apply(self, sp: StorageProvider) -> None:
        users = {u.id: u for u in await _read_all(sp.get_storage(User))}
        admins = [u for u in users.values() if u.role == "admin" and not u.disabled and u.created_at is not None]
        first_admin = min(admins, key=lambda u: u.created_at) if admins else None
        tokens = sp.get_storage(ApiToken)

        async def user_of(owner: dict[str, Any] | None) -> str | None:
            if not owner:
                return None
            if owner.get("type") == "user":
                user_id = owner.get("user_id") or owner.get("id")
            elif owner.get("type") == "api_token":
                token = await tokens.get(owner.get("id") or "")
                user_id = token.user_id if token is not None else None
            else:
                return None
            user = users.get(user_id) if user_id else None
            return user_id if user is not None and not user.disabled else None

        triggers = sp.get_storage(Trigger)
        rows = await _read_all(triggers)
        stamped = 0
        for row in rows:
            if not _unset(row.access):
                continue
            owner_id = await user_of(row.owner) or (first_admin.id if first_admin is not None else None)
            if owner_id is None:
                continue
            await triggers.update(row.model_copy(update={"access": _owned_by(owner_id)}))
            stamped += 1

        run_as = {row.id: row.owner for row in rows}
        admin_ref = (
            {"type": "user", "id": first_admin.id, "display": first_admin.username or first_admin.id, "role": "admin",
             "source": "local", "user_id": first_admin.id}
            if first_admin is not None else None
        )
        subs = sp.get_storage(Subscription)
        adopted = 0
        for sub in await _read_all(subs):
            if sub.owner is not None:
                continue
            owner = run_as.get(sub.trigger_id) or admin_ref
            if owner is None:
                continue
            # Disabled as well: the adopted identity must not run a row nobody can vouch for until its owner re-enables it.
            await subs.update(sub.model_copy(update={"owner": owner, "enabled": False}))
            adopted += 1
        if stamped:
            logger.info("gave existing triggers an owner", extra={"triggers": stamped})
        if adopted:
            logger.warning(
                "disabled legacy subscriptions that had no owner; their triggers' owners must review and re-enable them",
                extra={"subscriptions": adopted},
            )


__all__ = ["M026TriggerOwnership"]

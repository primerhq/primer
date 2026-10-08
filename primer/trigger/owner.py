"""Who a trigger-fired run runs as (security review A-20).

A fresh-session subscription starts an agent or graph run nobody is sitting in front of. That run used to carry
``PrincipalRef(type="trigger", role=None)``, which :func:`primer.authz._role_allows` waved through every role floor, so a
``role=user`` account could build a trigger whose run called admin-only tools. Now:

* The run is attributed to the subscription's ``owner`` (who chose the agent or graph, the workspace and the payload).
* It is ranked by the LOWER of the subscription owner's and the trigger owner's roles, so neither a user subscribing to an
  admin's trigger nor an admin subscribing to a user's trigger yields a run above the less trusted of the two.
* Roles are re-read at fire time, not trusted from the snapshot taken when the row was saved: a ``user`` owner is looked up by
  id, an ``api_token`` owner through its token to the token's user. An owner that cannot be resolved (deleted, disabled,
  revoked or expired token) ranks at most ``user``. A ``system`` owner (an auth-disabled deployment, where every request is
  ``system``) stays ``system``.
* A row saved before owners were recorded has none and ranks at most ``user`` (fail closed). A subscription with no owner
  keeps the old trigger-typed attribution (``type="trigger"``, the trigger id) so the run stays traceable, now with
  ``role="user"``.

The trigger provenance (``trigger_id``, ``subscription_id``, ``fire_id``) stays in the session metadata.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from primer.authz import _ROLE_RANK
from primer.model.principal import PrincipalRef
from primer.model.trigger import Subscription, Trigger

logger = logging.getLogger(__name__)

#: The rank an owner that cannot be resolved (or a row with no owner) is held to.
_FAIL_CLOSED_ROLE = "user"


def _lower(a: str | None, b: str | None) -> str | None:
    """The lower-ranked of two roles; an unranked role (``None`` or unknown) is the lowest of all."""
    return a if _ROLE_RANK.get(a, -1) <= _ROLE_RANK.get(b, -1) else b


async def _current_role(owner: PrincipalRef | None, storage_provider: Any) -> str | None:
    """The role ``owner`` holds now, capped at :data:`_FAIL_CLOSED_ROLE` when it cannot be resolved.

    Returns ``"system"`` for a system owner (not a role: the caller keeps the system principal as is).
    """
    if owner is None:
        return _FAIL_CLOSED_ROLE
    if owner.type == "system":
        return "system"
    user_id: str | None = None
    if owner.type == "user":
        user_id = owner.id
    elif owner.type == "api_token":
        from primer.model.api_token import ApiToken

        token = await storage_provider.get_storage(ApiToken).get(owner.id)
        expires = token.expires_at if token is not None else None
        if expires is not None and expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if token is not None and token.revoked_at is None and (expires is None or expires > datetime.now(timezone.utc)):
            user_id = token.user_id
    if user_id is not None:
        from primer.model.user import User

        user = await storage_provider.get_storage(User).get(user_id)
        if user is not None and not user.disabled:
            return user.role
    logger.info(
        "trigger owner %s:%s could not be resolved; the fired run ranks at most %r",
        owner.type, owner.id, _FAIL_CLOSED_ROLE,
    )
    return _lower(owner.role, _FAIL_CLOSED_ROLE)


async def principal_for_fire(sub: Subscription, storage_provider: Any) -> PrincipalRef:
    """The ``initiated_by`` of a run ``sub`` fires: its owner, ranked by the lower of its owner's and its trigger owner's roles."""
    trigger = await storage_provider.get_storage(Trigger).get(sub.trigger_id)
    trigger_role = await _current_role(trigger.owner if trigger is not None else None, storage_provider)
    sub_role = await _current_role(sub.owner, storage_provider)
    if sub_role == "system" and trigger_role == "system":
        return PrincipalRef.system()
    # A system side imposes no cap of its own; the other side decides.
    role = trigger_role if sub_role == "system" else sub_role if trigger_role == "system" else _lower(sub_role, trigger_role)
    owner = sub.owner
    if owner is None or owner.type == "system":
        return PrincipalRef(type="trigger", id=sub.trigger_id, display=sub.trigger_id, role=role, source="internal")
    return owner.model_copy(update={"role": role})


__all__ = ["principal_for_fire"]

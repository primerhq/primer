"""Sign-in hook: make sure a full user has their private default workspace (access-control P2 T8, spec 6.7)."""

from __future__ import annotations

import logging
import time

from fastapi import Request

from primer.api.deps import get_claim_engine, get_storage_provider, get_workspace_registry
from primer.bootstrap.prime import ensure_user_default_workspace
from primer.model.user import User

logger = logging.getLogger(__name__)

#: user id -> monotonic time before which a failed status-probe ensure is not retried.
_FAILED_UNTIL: dict[str, float] = {}
_BACKOFF_S = 60.0


async def ensure_default_for_request(request: Request, user: User, *, fast: bool = False) -> str | None:
    """The user's default workspace id after making sure it exists, or ``None`` for a user who has none (restricted, disabled).

    Never raises: a sign-in must not fail because a workspace backend is down or the install is not set up yet (no default template,
    no operator agent); the next sign-in or ``/auth/status`` tries again. ``fast`` returns a recorded id without touching storage
    again, for the status probe that runs on every page load; after a failure the probe waits 60 s before retrying (a sign-in always retries).
    """
    if user.role not in ("user", "admin") or user.disabled:
        return None
    if fast and user.default_workspace_id:
        return user.default_workspace_id
    if fast and _FAILED_UNTIL.get(user.id, 0.0) > time.monotonic():
        return None  # the probe gates the console's boot: do not re-run a failing ensure on every call
    try:
        from primer.workspace.session_factory import SessionFactoryDeps

        sp = get_storage_provider(request)
        registry = get_workspace_registry(request)
        return await ensure_user_default_workspace(
            sp,
            registry,
            user,
            template_id=request.app.state.config.default_workspace_template,
            session_deps=SessionFactoryDeps(
                storage_provider=sp, claim_engine=get_claim_engine(request),
                scheduler=getattr(request.app.state, "scheduler", None), workspace_registry=registry,
            ),
        )
    except Exception:  # noqa: BLE001
        logger.exception("could not ensure the default workspace of user %s", user.id)
        _FAILED_UNTIL[user.id] = time.monotonic() + _BACKOFF_S
        return user.default_workspace_id

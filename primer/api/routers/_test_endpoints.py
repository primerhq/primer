"""Instrumentation endpoints for distributed-mode test scenarios.

These routes are mounted ONLY when the ``PRIMER_ENABLE_TEST_ENDPOINTS``
environment variable is set to ``1``.  They are never included in the
production OpenAPI schema and must not be relied upon by application
code outside of ``tests/``.

Mount point: ``/v1/_test/*``
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from primer.model.except_ import NotFoundError
from primer.model.workspace_session import WorkspaceSession


router = APIRouter(tags=["_test"])


@router.post("/_test/acquire_rate_limit")
async def acquire_rate_limit(
    key: str,
    max_concurrency: int,
    sleep_ms: int,
    request: Request,
) -> dict[str, bool]:
    """Acquire a rate-limit lease, sleep, then release.

    Used by the S1 distributed scenario to measure peak concurrency
    under burst across processes.

    Query parameters
    ----------------
    key : str
        Rate-limiter key (e.g. ``"provider:some-id"``).
    max_concurrency : int
        Maximum concurrent holders allowed for *key*.
    sleep_ms : int
        How long to hold the lease (milliseconds).
    """
    coordinator = request.app.state.coordinator
    async with await coordinator.rate_limiter.acquire(
        key, max_concurrency=max_concurrency
    ):
        await asyncio.sleep(sleep_ms / 1000)
    return {"ok": True}


class ParkSessionBody(BaseModel):
    """Deterministic park injection - the counterpart to a real yielding
    tool call, without needing an actual LLM turn.

    Writes through the real ``WorkspaceSession`` model + storage layer
    (not raw SQL), so a ``parked_state`` shape drift fails this call
    immediately instead of a hand-rolled ``jsonb_set`` chain silently
    mismatching production (see US-011e's seam assessment:
    ``tests/ui_e2e/test_approvals_journey.py``'s ``_inject_approval_park``
    duplicated the contract this way).

    Deliberately mirrors exactly what a real park leaves on the row (see
    :class:`primer.int.claim.ParkRequest` /
    :func:`primer.session.yields.respond_to_yield`): no claim lease is
    armed here, same as a genuine park drops its lease. Resuming this
    session by calling the real ``/tool_approval/respond`` or
    ``/ask_user/respond`` endpoint therefore exercises the UNMODIFIED
    production resume path end-to-end (including lease re-arming via
    :func:`primer.session.yields.durably_wake_session`), not a second
    injection - the full park -> respond -> resume cycle becomes
    BDD-verifiable, not just the parked-state rendering.
    """

    parked_state: dict[str, Any] = Field(
        ...,
        description=(
            "Full parked_state blob, e.g. {tool_call_id, yielded: "
            "{tool_name, event_key, resume_metadata}, ...} - shape "
            "documented in the yielding-tools design spec section 5.2."
        ),
    )
    parked_event_key: str = Field(
        ..., description="Must match parked_state['yielded']['event_key'].",
    )
    parked_until: datetime | None = Field(
        default=None,
        description="Optional deadline; leave unset for parks with no timeout.",
    )


@router.post("/_test/park_session/{session_id}")
async def test_park_session(
    session_id: str,
    body: ParkSessionBody,
    request: Request,
) -> dict:
    """Deterministically park an existing session for BDD/e2e tests.

    Targets a session created through the normal API (POST
    /workspaces/{id}/sessions) and stamps its park fields through the
    real model - never touches anything the session's own creation path
    did not already set up correctly.
    """
    storage = request.app.state.storage_provider.get_storage(WorkspaceSession)
    session = await storage.get(session_id)
    if session is None:
        raise NotFoundError(f"Session {session_id!r} does not exist")
    updated = session.model_copy(update={
        "parked_status": "parked",
        "parked_state": body.parked_state,
        "parked_event_key": body.parked_event_key,
        "parked_until": body.parked_until,
        "parked_at": datetime.now(timezone.utc),
    })
    saved = await storage.update(updated)
    return {"ok": True, "parked_status": saved.parked_status}


__all__ = ["router"]

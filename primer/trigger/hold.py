"""Interactive webhook hold: fire, then await the fired runs' terminals.

S6 section 9 fixes the mechanism: the hold is an async wait on the
completion bus, never a poll. The subscription opens BEFORE the fire so a
run that reaches a terminal state while ``fire_trigger`` is still returning
cannot be missed.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from primer.model.workspace_session import WorkspaceSession
from primer.trigger.dispatch import FireResult, fire_trigger
from primer.trigger.subscribers import DispatchDeps

logger = logging.getLogger(__name__)

#: Hard upper bound on one hold, matching the worker's global yield cap
#: (primer/session/dispatch.py). The caller-visible wait cap is shorter and
#: is applied by the endpoint; this only stops an orphaned wait living
#: forever.
HOLD_MAX_SECONDS = 3600.0

_TERMINAL_PREFIX = "session:"
_TERMINAL_SUFFIX = ":terminal"


def _terminal_session_id(event_key: str) -> str | None:
    """Return the session id inside ``session:<sid>:terminal``, else None."""
    if not event_key.startswith(_TERMINAL_PREFIX):
        return None
    if not event_key.endswith(_TERMINAL_SUFFIX):
        return None
    return event_key[len(_TERMINAL_PREFIX):-len(_TERMINAL_SUFFIX)] or None


def held_targets(result: FireResult) -> list[str]:
    """Artefacts the hold waits on: dispatched, non-skipped, id-bearing."""
    out: list[str] = []
    for envelope in result.results:
        if (
            envelope.get("ok")
            and not envelope.get("skipped")
            and envelope.get("artefact_id")
        ):
            out.append(envelope["artefact_id"])
    return out


@dataclass
class HeldFire:
    """Outcome of one held fire."""

    fire_result: FireResult | None = None
    results: list[dict] = field(default_factory=list)
    timed_out: bool = False


async def fire_and_hold(
    *,
    trigger_id: str,
    extra_context: dict,
    deps: DispatchDeps,
    workspace_registry: Any,
    wait_timeout: float,
) -> HeldFire:
    """Fire ``trigger_id`` and hold until every fired run reaches a terminal."""
    if deps.event_bus is None:
        # No bus, no completion signal: fire and report the timeout shape so
        # the caller falls back to the 202 + poll path rather than claiming
        # a result it never observed.
        result = await fire_trigger(
            trigger_id=trigger_id, scheduled_for=None, deps=deps,
            extra_context=extra_context,
        )
        return HeldFire(fire_result=result, timed_out=True)

    sub = deps.event_bus.subscribe()
    try:
        result = await fire_trigger(
            trigger_id=trigger_id, scheduled_for=None, deps=deps,
            extra_context=extra_context,
        )
        targets = held_targets(result)
        if not targets:
            return HeldFire(fire_result=result)
        pending = set(targets)
        try:
            async with asyncio.timeout(wait_timeout):
                async for event in sub:
                    sid = _terminal_session_id(event.event_key)
                    if sid is not None and sid in pending:
                        pending.discard(sid)
                        if not pending:
                            break
        except TimeoutError:
            return HeldFire(fire_result=result, timed_out=True)
    finally:
        await sub.aclose()

    results = []
    for sid in targets:
        results.append({
            "artefact_id": sid,
            "final_text": await _final_text(
                deps.storage_provider, workspace_registry, sid,
            ),
        })
    return HeldFire(fire_result=result, results=results)


async def _final_text(
    storage_provider: Any, workspace_registry: Any, session_id: str,
) -> str | None:
    """Derive the run's final text exactly as the channel relay does."""
    if workspace_registry is None:
        return None
    # Function-local import: primer.channel is an optional subsystem, so the
    # trigger layer reaches it lazily (mirrors primer/session/dispatch.py).
    from primer.channel.session_relay import read_session_final_text

    try:
        row = await storage_provider.get_storage(WorkspaceSession).get(
            session_id
        )
        if row is None:
            return None
        workspace = await workspace_registry.get_workspace(row.workspace_id)
        if workspace is None:
            return None
        return await read_session_final_text(workspace, session_id)
    except Exception:  # noqa: BLE001 - a read failure must not fail the hold
        logger.warning(
            "webhook hold: final-text derivation failed for %s", session_id,
            exc_info=True,
        )
        return None


__all__ = ["HOLD_MAX_SECONDS", "HeldFire", "fire_and_hold", "held_targets"]

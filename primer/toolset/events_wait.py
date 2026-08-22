"""``wait_for_event``: park the agent's turn on the platform event log.

The handler creates a one-shot :class:`EventSubscription` with a
``session_wake`` sink and yields; the leader-elected dispatcher
matches the next event that passes the filter, publishes the park's
event key on the wake bus with the event envelope as payload, and the
normal yield-resume machinery delivers it as the tool result.

Missed-event safety: the subscription's cursor is pinned to the log
head at CREATE time, so anything appended after this call is
considered even if it lands before the worker finishes writing the
park row (the dispatcher holds delivery until the park is visible).
On timeout or cancel the orphaned subscription is garbage-collected
by the dispatcher the next time a matching event arrives and the
session is no longer parked on the key.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, ValidationError

from primer.model.event import (
    EventFilter,
    EventSubscription,
    FieldMatcher,
    SessionWakeSink,
)
from primer.model.chat import ToolCallResult
from primer.model.yield_ import ToolContext, Yielded
from primer.toolset._helpers import err as _err, ok as _ok
from primer.toolset._system_common import _err_from_validation

if TYPE_CHECKING:
    from primer.int.storage_provider import StorageProvider


class _WaitForEventArgs(BaseModel):
    """Filter for the event to wait on (three tiers, all optional)."""

    event_types: list[str] = Field(
        ...,
        min_length=1,
        description=(
            "Event-type globs to wake on, e.g. "
            '["collection.document_pushed"] or ["agent.*"].'
        ),
    )
    fields: list[FieldMatcher] | None = Field(
        default=None,
        description=(
            "Structural conditions, each {path, op: eq|prefix|regex, "
            "value}; paths resolve into the envelope then the payload "
            '(e.g. "payload.collection_id").'
        ),
    )
    expr: str | None = Field(
        default=None,
        description=(
            "Optional rego module (package primer.event_filter, boolean "
            "`match` rule) evaluated with the event as input."
        ),
    )
    timeout_seconds: float | None = Field(
        default=None,
        gt=0,
        description=(
            "Give up after this long; the tool then returns "
            "{timed_out: true}. Default: the global yield cap."
        ),
    )


def make_wait_for_event_handler(storage_provider: "StorageProvider"):
    async def _wait_for_event_handler(
        arguments: dict[str, Any],
        *,
        ctx: ToolContext,
    ) -> ToolCallResult | Yielded:
        try:
            args = _WaitForEventArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        if ctx is None or not ctx.session_id:
            return _err(
                "wait_for_event needs a workspace session to park; it is "
                "not available on this surface",
                error_type="unsupported-surface",
            )
        for matcher in args.fields or []:
            if matcher.op == "regex":
                try:
                    re.compile(matcher.value)
                except re.error as exc:
                    return _err(
                        f"invalid regex {matcher.value!r}: {exc}",
                        error_type="validation-error",
                    )

        event_key = f"evwait:{ctx.session_id}:{ctx.tool_call_id}"
        subscription = EventSubscription(
            description=(
                f"wait_for_event park for session {ctx.session_id}"
            ),
            filter=EventFilter(
                event_types=args.event_types,
                fields=list(args.fields or []),
                expr=args.expr,
            ),
            sink=SessionWakeSink(
                event_key=event_key,
                session_id=ctx.session_id,
            ),
            managed_by="system",
        )
        subs = storage_provider.get_storage(EventSubscription)
        created = await subs.create(subscription)
        store = storage_provider.get_event_store()
        # Pin the cursor at the current head: everything after this
        # call is in scope, nothing before it replays.
        await store.set_cursor(created.id, await store.max_id())
        return Yielded(
            tool_name="",  # stamped by the provider
            event_key=event_key,
            timeout=args.timeout_seconds,
            resume_metadata={
                "subscription_id": created.id,
                "event_types": args.event_types,
            },
        )

    return _wait_for_event_handler


def wait_for_event_resume(
    yield_metadata: dict[str, Any],
    event_payload: Any,
    ctx: Any,
) -> ToolCallResult:
    """Format the woken park's result.

    The dispatcher delivered the full event envelope as the bus
    payload; synthetic YieldTimeout / YieldCancelled payloads surface
    as {timed_out} / {cancelled}. The one-shot subscription is deleted
    by the dispatcher on delivery; a timed-out or cancelled park's
    subscription is garbage-collected by the dispatcher later, so the
    hook stays storage-free (module-level, one registration).
    """
    from primer.model.yield_ import YieldCancelled, YieldTimeout

    if isinstance(event_payload, YieldTimeout):
        return _ok({
            "timed_out": True,
            "elapsed_seconds": event_payload.elapsed_seconds,
            "event_types": yield_metadata.get("event_types"),
        })
    if isinstance(event_payload, YieldCancelled):
        return _ok({
            "cancelled": True,
            "cancel_reason": event_payload.reason,
        })
    return _ok({"event": event_payload})


from primer.worker.yield_resume_registry import register_resume_hook  # noqa: E402

register_resume_hook("wait_for_event", wait_for_event_resume)


__all__ = [
    "make_wait_for_event_handler",
    "wait_for_event_resume",
    "_WaitForEventArgs",
]

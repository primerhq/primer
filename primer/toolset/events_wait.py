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
session is no longer parked on the key, or waits on it under ANOTHER
subscription (a later ``wait_for_event`` under the same tool_call_id:
the park's entry names the subscription that tool call created, and
the orphan delivers nothing into it).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, ValidationError

from primer.agent.inner_dispatch import run_principal
from primer.authz import _role_allows
from primer.events.filters import validate_filter_expr
from primer.events.redaction import redact_payload
from primer.events.visibility import user_safe_glob, user_safe_kinds
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
            '["collection.document_pushed"] or ["agent.*"]. A run whose '
            "principal is not an admin may name only the kinds the event "
            "feed shows a non-admin (see the tool description)."
        ),
    )
    fields: list[FieldMatcher] | None = Field(
        default=None,
        description=(
            "Structural conditions, each {path, op: eq|prefix|regex, "
            "value}; paths resolve into the envelope then the payload "
            '(e.g. "payload.collection_id"). The regex op is for admin '
            "runs only."
        ),
    )
    expr: str | None = Field(
        default=None,
        description=(
            "Optional rego module (package primer.event_filter, boolean "
            "`match` rule) evaluated with the event as input. References "
            "to the opa package are refused. Admin runs only."
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


_NOT_ADMIN = "this run's principal is not an admin, so "


def _non_admin_refusal(ctx: ToolContext, args: _WaitForEventArgs) -> str | None:
    """Why this run may not make this wait (the ``forbidden`` message), or ``None``: always ``None`` for an admin.

    The tool is role user (pivot P1). A run whose principal is not an admin (``run_principal`` under the role floor's own predicate: a
    context with no initiator is not an admin, the system principal is) is held to three limits, checked in this order:

    * the kinds: the event that wakes the wait becomes the run's tool result, so it may wait only on what ``GET /v1/events`` shows a
      non-admin (``primer.events.visibility``, the feed's own rule; a glob that could match a hidden kind is refused too);
    * no ``expr``: the dispatcher evaluates the rego module server-side; every expr is also checked separately for references to the
      host ``opa`` package, including for an admin;
    * no ``regex`` field matcher: patterns run in the shared, leader-elected dispatcher loop, so a catastrophic one stalls every
      subscription. ``eq`` and ``prefix`` stay open.
    """
    if _role_allows(run_principal(ctx), "admin"):
        return None
    hidden = [glob for glob in args.event_types if not user_safe_glob(glob)]
    if hidden:
        return (
            _NOT_ADMIN + "it may wait only on the event kinds the event feed shows a non-admin ("
            + ", ".join(user_safe_kinds()) + "); refused: " + ", ".join(repr(glob) for glob in hidden)
        )
    if args.expr is not None:
        return _NOT_ADMIN + "it may not pass an expr (a rego module the platform evaluates); filter with fields matchers instead"
    regex = [matcher.path for matcher in args.fields or [] if matcher.op == "regex"]
    if regex:
        return (
            _NOT_ADMIN + "its fields matchers may use only the eq and prefix ops; refused regex on: "
            + ", ".join(repr(path) for path in regex)
        )
    return None


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
        # Before anything is stored: a refused wait leaves no subscription for the dispatcher to evaluate.
        refusal = _non_admin_refusal(ctx, args)
        if refusal is not None:
            return _err(refusal, error_type="forbidden")
        try:
            validate_filter_expr(args.expr)
        except ValueError as exc:
            return _err(str(exc), error_type="validation-error")
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
    payload, with stored secrets masked (``redact_payload``, the same
    masking GET /v1/events applies); synthetic YieldTimeout / YieldCancelled payloads surface
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
    # The dispatcher already publishes a redacted envelope; mask again
    # here so the agent's transcript never holds a stored secret even if
    # a wake reaches this hook by another path (SEC-04).
    if isinstance(event_payload, dict) and "payload" in event_payload:
        event_payload = {
            **event_payload,
            "payload": redact_payload(event_payload["payload"]),
        }
    return _ok({"event": event_payload})


from primer.worker.yield_resume_registry import register_resume_hook  # noqa: E402

register_resume_hook("wait_for_event", wait_for_event_resume)


__all__ = [
    "make_wait_for_event_handler",
    "wait_for_event_resume",
    "_WaitForEventArgs",
]

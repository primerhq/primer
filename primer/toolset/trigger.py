"""``trigger`` internal toolset — mirrors the Trigger REST API.

Spec §11.1 (management tools) + §11.2 (yielding tool). Management
tools delegate to :mod:`primer.trigger.service` so the toolset and
the REST router share one mutation path.

Management tools (one-to-one with ``/v1/triggers/*`` endpoints):

* ``trigger__list`` — list with optional ``kind`` / ``enabled`` filters.
* ``trigger__get`` — fetch one trigger by id.
* ``trigger__create`` — create a delayed / scheduled trigger.
* ``trigger__update`` — partial update.
* ``trigger__delete`` — cascade-delete trigger + subscriptions.
* ``trigger__fire_now`` — synchronous fire (testing aid).
* ``trigger__list_subscriptions`` — list subs for a trigger.
* ``trigger__get_subscription`` — fetch one sub.
* ``trigger__create_subscription`` — create a non-``parked_session`` sub.
* ``trigger__update_subscription`` — partial sub update.
* ``trigger__delete_subscription`` — delete a sub.

Plus the yielding tool (Spec §9 / §11.2):

* ``subscribe_to_trigger`` — park the calling session until the
  trigger fires next. Resumes with the fire context as the tool
  result. Persists a one-shot ``parked_session`` Subscription which
  the matching dispatcher consumes on fire.

Each tool translates typed service exceptions to ``ToolCallResult`` error
envelopes using the spec §14 error codes. Argument validation errors
(Pydantic) surface as ``type=validation-error``.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from pydantic import BaseModel, Field, ValidationError

from primer.model.chat import Tool, ToolCallResult
from primer.model.trigger import (
    ParkedSessionSubConfig,
    Subscription,
    SubscriptionConfig,
    Trigger,
    TriggerConfig,
)
from primer.model.yield_ import ToolContext, Yielded
from primer.toolset.internal import InternalToolsetProvider, ToolHandler
from primer.trigger.cron import CronInvalid, TimezoneInvalid
from primer.trigger.service import (
    ParkedSessionOnlyFromYield,
    ServiceDeps,
    SubscriptionNotFound,
    TriggerKindImmutable,
    TriggerNotFound,
    TriggerSlugConflict,
    create_subscription,
    create_trigger,
    delete_subscription,
    delete_trigger,
    fire_now,
    get_subscription,
    get_trigger,
    list_subscriptions,
    list_triggers,
    update_subscription,
    update_trigger,
)


if TYPE_CHECKING:
    from primer.int.storage_provider import StorageProvider


logger = logging.getLogger(__name__)

TRIGGER_TOOLSET_ID = "trigger"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ok(payload: Any) -> ToolCallResult:
    if isinstance(payload, BaseModel):
        return ToolCallResult(output=payload.model_dump_json(), is_error=False)
    if isinstance(payload, list):
        body = [
            p.model_dump(mode="json") if isinstance(p, BaseModel) else p
            for p in payload
        ]
        return ToolCallResult(
            output=json.dumps(body, default=str), is_error=False,
        )
    return ToolCallResult(
        output=json.dumps(payload, default=str), is_error=False,
    )


def _err(message: str, *, error_type: str = "tool-error") -> ToolCallResult:
    return ToolCallResult(
        output=json.dumps({"type": error_type, "message": message}),
        is_error=True,
    )


def _err_validation(exc: ValidationError) -> ToolCallResult:
    return _err(
        "argument validation failed: " + json.dumps(exc.errors(), default=str),
        error_type="validation-error",
    )


# ---------------------------------------------------------------------------
# Argument models
# ---------------------------------------------------------------------------


class _ListArgs(BaseModel):
    kind: str | None = Field(
        default=None,
        description="Optional filter by trigger kind (e.g. 'delayed', 'scheduled').",
    )
    enabled: bool | None = Field(
        default=None, description="Optional filter by enabled flag.",
    )


class _IdArgs(BaseModel):
    id: str = Field(..., min_length=1, description="Trigger id.")


class _CreateArgs(BaseModel):
    slug: str = Field(..., min_length=2, max_length=64)
    name: str = Field(..., min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    config: TriggerConfig
    enabled: bool = True


class _UpdateArgs(BaseModel):
    id: str = Field(..., min_length=1)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    enabled: bool | None = None
    config: TriggerConfig | None = None


class _SubListArgs(BaseModel):
    trigger_id: str = Field(..., min_length=1)


class _SubIdArgs(BaseModel):
    trigger_id: str = Field(..., min_length=1)
    subscription_id: str = Field(..., min_length=1)


class _SubCreateArgs(BaseModel):
    trigger_id: str = Field(..., min_length=1)
    config: SubscriptionConfig
    payload_template: str | None = None
    parallelism: str = "skip"
    description: str | None = Field(default=None, max_length=2000)
    enabled: bool = True


class _SubUpdateArgs(BaseModel):
    """Subscription partial update.

    Nullable fields (``payload_template``, ``description``) use the same
    "field-set" detection as the REST router: a key omitted from the
    body leaves the existing value alone; an explicit ``null`` clears it.
    """

    trigger_id: str = Field(..., min_length=1)
    subscription_id: str = Field(..., min_length=1)
    payload_template: str | None = None
    parallelism: str | None = None
    enabled: bool | None = None
    description: str | None = None


class _SubscribeArgs(BaseModel):
    trigger_id: str = Field(..., min_length=1, description="Trigger id to subscribe to.")


# ---------------------------------------------------------------------------
# Tool descriptors
# ---------------------------------------------------------------------------


TOOL_LIST = Tool(
    id="list",
    toolset_id=TRIGGER_TOOLSET_ID,
    description=(
        "List triggers. Optional filters: ``kind`` (delayed / scheduled) "
        "and ``enabled`` (true / false). Returns an array of Trigger "
        "objects."
    ),
    args_schema=_ListArgs.model_json_schema(),
)

TOOL_GET = Tool(
    id="get",
    toolset_id=TRIGGER_TOOLSET_ID,
    description=(
        "Get one trigger by id. Returns the full Trigger row or "
        "``is_error=true type=trigger_not_found``."
    ),
    args_schema=_IdArgs.model_json_schema(),
)

TOOL_CREATE = Tool(
    id="create",
    toolset_id=TRIGGER_TOOLSET_ID,
    description=(
        "Create a new trigger. ``config`` is the discriminated union "
        "(delayed: ``{kind:'delayed', fire_at}``; scheduled: "
        "``{kind:'scheduled', cron, timezone?, catchup?}``). Returns the "
        "created Trigger. Conflicts on duplicate slug surface as "
        "``type=trigger_slug_conflict``; bad cron / timezone surface as "
        "``type=cron_invalid`` / ``type=timezone_invalid``."
    ),
    args_schema=_CreateArgs.model_json_schema(),
)

TOOL_UPDATE = Tool(
    id="update",
    toolset_id=TRIGGER_TOOLSET_ID,
    description=(
        "Partial update of a trigger. Changing ``config.kind`` is "
        "rejected with ``type=trigger_kind_immutable`` — operators "
        "delete + recreate to switch kinds. Returns the updated Trigger."
    ),
    args_schema=_UpdateArgs.model_json_schema(),
)

TOOL_DELETE = Tool(
    id="delete",
    toolset_id=TRIGGER_TOOLSET_ID,
    description=(
        "Delete a trigger and cascade-delete its subscriptions. Returns "
        "``{ok: true}`` on success or ``type=trigger_not_found``."
    ),
    args_schema=_IdArgs.model_json_schema(),
)

TOOL_FIRE_NOW = Tool(
    id="fire_now",
    toolset_id=TRIGGER_TOOLSET_ID,
    description=(
        "Synchronously fire a trigger (operator/testing aid). Bypasses "
        "the scheduler. Returns ``{fire_id, results: [...]}`` where each "
        "result mirrors the per-subscription dispatch envelope."
    ),
    args_schema=_IdArgs.model_json_schema(),
)

TOOL_LIST_SUBS = Tool(
    id="list_subscriptions",
    toolset_id=TRIGGER_TOOLSET_ID,
    description=(
        "List subscriptions bound to a trigger. Returns an array of "
        "Subscription rows. ``type=trigger_not_found`` if the parent "
        "trigger doesn't exist."
    ),
    args_schema=_SubListArgs.model_json_schema(),
)

TOOL_GET_SUB = Tool(
    id="get_subscription",
    toolset_id=TRIGGER_TOOLSET_ID,
    description=(
        "Get one subscription scoped to its trigger. Returns the full "
        "Subscription row or ``type=subscription_not_found``."
    ),
    args_schema=_SubIdArgs.model_json_schema(),
)

TOOL_CREATE_SUB = Tool(
    id="create_subscription",
    toolset_id=TRIGGER_TOOLSET_ID,
    description=(
        "Create a subscription bound to a trigger. ``config`` is the "
        "subscription-kind discriminated union "
        "(chat_message / agent_fresh_session / graph_fresh_session). "
        "Subs of kind ``parked_session`` are rejected with "
        "``type=parked_session_only_from_yield`` — only the "
        "``subscribe_to_trigger`` yielding tool may create those."
    ),
    args_schema=_SubCreateArgs.model_json_schema(),
)

TOOL_UPDATE_SUB = Tool(
    id="update_subscription",
    toolset_id=TRIGGER_TOOLSET_ID,
    description=(
        "Partial update of a subscription. Only the fields supplied are "
        "modified. Returns the updated Subscription or "
        "``type=subscription_not_found``."
    ),
    args_schema=_SubUpdateArgs.model_json_schema(),
)

TOOL_DELETE_SUB = Tool(
    id="delete_subscription",
    toolset_id=TRIGGER_TOOLSET_ID,
    description=(
        "Delete a subscription by id (scoped to its trigger). Returns "
        "``{ok: true}`` on success or ``type=subscription_not_found``."
    ),
    args_schema=_SubIdArgs.model_json_schema(),
)

TOOL_SUBSCRIBE = Tool(
    id="subscribe_to_trigger",
    toolset_id=TRIGGER_TOOLSET_ID,
    description=(
        "Yielding tool. Park the calling session until ``trigger_id`` "
        "next fires. Resumes with the fire context dict as the tool "
        "result. Validates the trigger exists and is enabled — "
        "otherwise returns ``type=trigger_not_found_or_disabled``. "
        "Persists a one-shot ``parked_session`` Subscription bound to "
        "the caller's (session_id, tool_call_id); the matching "
        "dispatcher deletes the row after delivering the resume payload."
    ),
    args_schema=_SubscribeArgs.model_json_schema(),
)


# ---------------------------------------------------------------------------
# Handler factories
# ---------------------------------------------------------------------------


def _make_deps(
    storage_provider: "StorageProvider",
    claim_engine: Any,
    event_bus: Any,
) -> ServiceDeps:
    return ServiceDeps(
        storage_provider=storage_provider,
        claim_engine=claim_engine,
        event_bus=event_bus,
    )


def _make_list_handler(
    storage_provider: "StorageProvider",
    claim_engine: Any,
    event_bus: Any,
) -> ToolHandler:
    async def _handler(arguments: dict[str, Any]) -> ToolCallResult:
        try:
            args = _ListArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_validation(exc)
        deps = _make_deps(storage_provider, claim_engine, event_bus)
        items = await list_triggers(
            kind=args.kind, enabled=args.enabled, deps=deps,
        )
        return _ok(items)

    return _handler


def _make_get_handler(
    storage_provider: "StorageProvider",
    claim_engine: Any,
    event_bus: Any,
) -> ToolHandler:
    async def _handler(arguments: dict[str, Any]) -> ToolCallResult:
        try:
            args = _IdArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_validation(exc)
        deps = _make_deps(storage_provider, claim_engine, event_bus)
        try:
            trigger = await get_trigger(trigger_id=args.id, deps=deps)
        except TriggerNotFound as exc:
            return _err(str(exc), error_type="trigger_not_found")
        return _ok(trigger)

    return _handler


def _make_create_handler(
    storage_provider: "StorageProvider",
    claim_engine: Any,
    event_bus: Any,
) -> ToolHandler:
    async def _handler(arguments: dict[str, Any]) -> ToolCallResult:
        try:
            args = _CreateArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_validation(exc)
        deps = _make_deps(storage_provider, claim_engine, event_bus)
        try:
            trigger = await create_trigger(
                slug=args.slug,
                name=args.name,
                description=args.description,
                config=args.config,
                enabled=args.enabled,
                deps=deps,
            )
        except TriggerSlugConflict as exc:
            return _err(str(exc), error_type="trigger_slug_conflict")
        except CronInvalid as exc:
            return _err(str(exc), error_type="cron_invalid")
        except TimezoneInvalid as exc:
            return _err(str(exc), error_type="timezone_invalid")
        return _ok(trigger)

    return _handler


def _make_update_handler(
    storage_provider: "StorageProvider",
    claim_engine: Any,
    event_bus: Any,
) -> ToolHandler:
    async def _handler(arguments: dict[str, Any]) -> ToolCallResult:
        try:
            args = _UpdateArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_validation(exc)
        deps = _make_deps(storage_provider, claim_engine, event_bus)
        try:
            trigger = await update_trigger(
                trigger_id=args.id,
                name=args.name,
                description=args.description,
                enabled=args.enabled,
                config=args.config,
                deps=deps,
            )
        except TriggerNotFound as exc:
            return _err(str(exc), error_type="trigger_not_found")
        except TriggerKindImmutable as exc:
            return _err(str(exc), error_type="trigger_kind_immutable")
        except CronInvalid as exc:
            return _err(str(exc), error_type="cron_invalid")
        except TimezoneInvalid as exc:
            return _err(str(exc), error_type="timezone_invalid")
        return _ok(trigger)

    return _handler


def _make_delete_handler(
    storage_provider: "StorageProvider",
    claim_engine: Any,
    event_bus: Any,
) -> ToolHandler:
    async def _handler(arguments: dict[str, Any]) -> ToolCallResult:
        try:
            args = _IdArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_validation(exc)
        deps = _make_deps(storage_provider, claim_engine, event_bus)
        try:
            await delete_trigger(trigger_id=args.id, deps=deps)
        except TriggerNotFound as exc:
            return _err(str(exc), error_type="trigger_not_found")
        return _ok({"ok": True})

    return _handler


def _make_fire_now_handler(
    storage_provider: "StorageProvider",
    claim_engine: Any,
    event_bus: Any,
) -> ToolHandler:
    async def _handler(arguments: dict[str, Any]) -> ToolCallResult:
        try:
            args = _IdArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_validation(exc)
        deps = _make_deps(storage_provider, claim_engine, event_bus)
        try:
            result = await fire_now(trigger_id=args.id, deps=deps)
        except TriggerNotFound as exc:
            return _err(str(exc), error_type="trigger_not_found")
        return _ok({
            "skipped": result.skipped,
            "fire_id": result.fire_id,
            "results": result.results,
        })

    return _handler


def _make_list_subs_handler(
    storage_provider: "StorageProvider",
    claim_engine: Any,
    event_bus: Any,
) -> ToolHandler:
    async def _handler(arguments: dict[str, Any]) -> ToolCallResult:
        try:
            args = _SubListArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_validation(exc)
        deps = _make_deps(storage_provider, claim_engine, event_bus)
        # Mirror the router: surface 404 when the parent trigger doesn't
        # exist instead of returning an empty list silently.
        try:
            await get_trigger(trigger_id=args.trigger_id, deps=deps)
        except TriggerNotFound as exc:
            return _err(str(exc), error_type="trigger_not_found")
        items = await list_subscriptions(
            trigger_id=args.trigger_id, deps=deps,
        )
        return _ok(items)

    return _handler


def _make_get_sub_handler(
    storage_provider: "StorageProvider",
    claim_engine: Any,
    event_bus: Any,
) -> ToolHandler:
    async def _handler(arguments: dict[str, Any]) -> ToolCallResult:
        try:
            args = _SubIdArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_validation(exc)
        deps = _make_deps(storage_provider, claim_engine, event_bus)
        try:
            sub = await get_subscription(
                trigger_id=args.trigger_id,
                subscription_id=args.subscription_id,
                deps=deps,
            )
        except SubscriptionNotFound as exc:
            return _err(str(exc), error_type="subscription_not_found")
        return _ok(sub)

    return _handler


def _make_create_sub_handler(
    storage_provider: "StorageProvider",
    claim_engine: Any,
    event_bus: Any,
) -> ToolHandler:
    async def _handler(arguments: dict[str, Any]) -> ToolCallResult:
        try:
            args = _SubCreateArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_validation(exc)
        deps = _make_deps(storage_provider, claim_engine, event_bus)
        try:
            sub = await create_subscription(
                trigger_id=args.trigger_id,
                config=args.config,
                payload_template=args.payload_template,
                parallelism=args.parallelism,
                description=args.description,
                enabled=args.enabled,
                deps=deps,
            )
        except TriggerNotFound as exc:
            return _err(str(exc), error_type="trigger_not_found")
        except ParkedSessionOnlyFromYield as exc:
            return _err(str(exc), error_type="parked_session_only_from_yield")
        return _ok(sub)

    return _handler


def _make_update_sub_handler(
    storage_provider: "StorageProvider",
    claim_engine: Any,
    event_bus: Any,
) -> ToolHandler:
    async def _handler(arguments: dict[str, Any]) -> ToolCallResult:
        try:
            args = _SubUpdateArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_validation(exc)
        deps = _make_deps(storage_provider, claim_engine, event_bus)
        # Only forward fields the caller actually supplied so missing
        # keys don't clobber existing values with None. Mirrors the
        # router's ``model_fields_set`` handling.
        sent = args.model_fields_set
        kwargs: dict[str, Any] = {}
        if "payload_template" in sent:
            kwargs["payload_template"] = args.payload_template
        if "parallelism" in sent:
            kwargs["parallelism"] = args.parallelism
        if "enabled" in sent:
            kwargs["enabled"] = args.enabled
        if "description" in sent:
            kwargs["description"] = args.description
        try:
            sub = await update_subscription(
                trigger_id=args.trigger_id,
                subscription_id=args.subscription_id,
                deps=deps,
                **kwargs,
            )
        except SubscriptionNotFound as exc:
            return _err(str(exc), error_type="subscription_not_found")
        return _ok(sub)

    return _handler


def _make_delete_sub_handler(
    storage_provider: "StorageProvider",
    claim_engine: Any,
    event_bus: Any,
) -> ToolHandler:
    async def _handler(arguments: dict[str, Any]) -> ToolCallResult:
        try:
            args = _SubIdArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_validation(exc)
        deps = _make_deps(storage_provider, claim_engine, event_bus)
        try:
            await delete_subscription(
                trigger_id=args.trigger_id,
                subscription_id=args.subscription_id,
                deps=deps,
            )
        except SubscriptionNotFound as exc:
            return _err(str(exc), error_type="subscription_not_found")
        return _ok({"ok": True})

    return _handler


def _make_subscribe_handler(
    storage_provider: "StorageProvider",
) -> ToolHandler:
    """Yielding-tool handler for ``subscribe_to_trigger``.

    The :class:`InternalToolsetProvider` injects a :class:`ToolContext`
    because this handler declares ``ctx`` as a keyword argument. We
    use ``ctx.session_id`` + ``ctx.tool_call_id`` to populate the
    ``parked_session`` Subscription config — those identify which park
    the dispatcher will reach on fire.

    The Subscription row is written BEFORE returning :class:`Yielded`
    so a fire racing the park still finds the row. The dispatcher's
    park-state check then guards against a stale fire (it verifies the
    target session is actually parked on the matching tool_call_id
    before publishing the resume payload).
    """

    async def _handler(
        arguments: dict[str, Any],
        *,
        ctx: ToolContext,
    ) -> ToolCallResult | Yielded:
        try:
            args = _SubscribeArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_validation(exc)

        trigger_storage = storage_provider.get_storage(Trigger)
        trigger = await trigger_storage.get(args.trigger_id)
        if trigger is None or not trigger.enabled:
            return _err(
                f"trigger {args.trigger_id!r} does not exist or is disabled",
                error_type="trigger_not_found_or_disabled",
            )

        # Chat-only callers have no session to park; refuse rather than
        # write an orphan row the dispatcher would just skip on fire.
        if ctx.session_id is None:
            return _err(
                "subscribe_to_trigger requires a session-bound caller; "
                "chat-only invocations have no session to park",
                error_type="trigger_not_found_or_disabled",
            )

        sub_id = f"sb-{uuid4().hex[:12]}"
        sub = Subscription(
            id=sub_id,
            trigger_id=args.trigger_id,
            config=ParkedSessionSubConfig(
                session_id=ctx.session_id,
                tool_call_id=ctx.tool_call_id,
                parked_at=datetime.now(timezone.utc),
            ),
            payload_template=None,
            parallelism="skip",  # field unused for parked_session
            enabled=True,
            created_at=datetime.now(timezone.utc),
        )
        await storage_provider.get_storage(Subscription).create(sub)
        return Yielded(
            tool_name="subscribe_to_trigger",
            event_key=f"trigger:{args.trigger_id}",
            timeout=None,  # honour the global yield cap
            resume_metadata={
                "subscription_id": sub_id,
                "trigger_id": args.trigger_id,
            },
        )

    return _handler


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------


def build_trigger_toolset_provider(
    *,
    storage_provider: "StorageProvider",
    claim_engine: Any = None,
    event_bus: Any = None,
    toolset_id: str = TRIGGER_TOOLSET_ID,
) -> InternalToolsetProvider:
    """Construct the ``trigger`` internal toolset."""
    registry: dict[str, tuple[Tool, ToolHandler]] = {
        "list": (
            TOOL_LIST,
            _make_list_handler(storage_provider, claim_engine, event_bus),
        ),
        "get": (
            TOOL_GET,
            _make_get_handler(storage_provider, claim_engine, event_bus),
        ),
        "create": (
            TOOL_CREATE,
            _make_create_handler(storage_provider, claim_engine, event_bus),
        ),
        "update": (
            TOOL_UPDATE,
            _make_update_handler(storage_provider, claim_engine, event_bus),
        ),
        "delete": (
            TOOL_DELETE,
            _make_delete_handler(storage_provider, claim_engine, event_bus),
        ),
        "fire_now": (
            TOOL_FIRE_NOW,
            _make_fire_now_handler(storage_provider, claim_engine, event_bus),
        ),
        "list_subscriptions": (
            TOOL_LIST_SUBS,
            _make_list_subs_handler(storage_provider, claim_engine, event_bus),
        ),
        "get_subscription": (
            TOOL_GET_SUB,
            _make_get_sub_handler(storage_provider, claim_engine, event_bus),
        ),
        "create_subscription": (
            TOOL_CREATE_SUB,
            _make_create_sub_handler(storage_provider, claim_engine, event_bus),
        ),
        "update_subscription": (
            TOOL_UPDATE_SUB,
            _make_update_sub_handler(storage_provider, claim_engine, event_bus),
        ),
        "delete_subscription": (
            TOOL_DELETE_SUB,
            _make_delete_sub_handler(storage_provider, claim_engine, event_bus),
        ),
        "subscribe_to_trigger": (
            TOOL_SUBSCRIBE,
            _make_subscribe_handler(storage_provider),
        ),
    }
    logger.info(
        "trigger toolset assembled with %d tools (id=%s)",
        len(registry),
        toolset_id,
    )
    return InternalToolsetProvider(toolset_id=toolset_id, registry=registry)


__all__ = ["TRIGGER_TOOLSET_ID", "build_trigger_toolset_provider"]

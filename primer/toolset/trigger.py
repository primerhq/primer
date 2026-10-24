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

The yielding tool ``subscribe_to_trigger`` (Spec §9 / §11.2) - park
the calling session until the trigger fires next, resuming with the
fire context as the tool result - has MOVED to the ``workspace_ext``
reserved toolset (it parks a workspace session, so it is kept out of
chat context). Its descriptor and handler factory remain defined in
this module and are imported by ``primer.toolset.workspace_ext``.

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

from primer.common.validation_errors import without_input
from primer.model.chat import Tool, ToolCallResult, ToolExample
from primer.model.event_matcher import (
    EventMatcher,
    EventMatcherPatternError,
    validate_text_pattern,
)
from primer.model.trigger import (
    SLUG_PATTERN,
    Parallelism,
    ParkedSessionSubConfig,
    Subscription,
    SubscriptionConfig,
    Trigger,
    TriggerConfig,
    TriggerKind,
)
from primer.model.yield_ import ToolContext, Yielded
from primer.session.peer_messages import caller_reach
from primer.toolset._describe import make_tool
from primer.toolset._helpers import err as _err, ok as _ok
from primer.toolset.internal import InternalToolsetProvider, ToolHandler
from primer.trigger.cron import CronInvalid, TimezoneInvalid
from primer.trigger.owner import view_for
from primer.trigger.reach import SUBSCRIPTION_LIST_REACH_NOTE, SUBSCRIPTION_REACH_NOTE
from primer.trigger.service import (
    ParkedSessionOnlyFromYield,
    ServiceDeps,
    SubscriptionNotFound,
    SubscriptionTargetNotFound,
    TriggerForbidden,
    TriggerKindImmutable,
    TriggerNotFound,
    TriggerOutranked,
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
    from primer.model.principal import PrincipalRef


logger = logging.getLogger(__name__)

TRIGGER_TOOLSET_ID = "trigger"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _err_validation(exc: ValidationError) -> ToolCallResult:
    return _err(
        "argument validation failed: " + json.dumps(without_input(exc.errors()), default=str),
        error_type="validation-error",
    )


# ---------------------------------------------------------------------------
# Argument models
# ---------------------------------------------------------------------------


class _ListArgs(BaseModel):
    kind: str | None = Field(
        default=None,
        description="Optional filter by trigger kind: 'delayed', 'scheduled', 'webhook' or 'channel'.",
    )
    enabled: bool | None = Field(
        default=None, description="Optional filter by enabled flag.",
    )


class _IdArgs(BaseModel):
    id: str = Field(..., min_length=1, description="Trigger id.")


class _CreateArgs(BaseModel):
    slug: str = Field(
        ...,
        min_length=2,
        max_length=64,
        pattern=SLUG_PATTERN,
        description="Unique: a lowercase letter, then lowercase letters, digits or '-' (2 to 64 characters).",
    )
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
    parallelism: Parallelism = "skip"
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
    parallelism: Parallelism | None = None
    enabled: bool | None = None
    description: str | None = None


class _SubscribeArgs(BaseModel):
    trigger_id: str = Field(..., min_length=1, description="Trigger id to subscribe to.")


class _SubscribeChannelArgs(BaseModel):
    trigger_id: str = Field(..., min_length=1, description="Channel trigger id to subscribe to.")
    event_matcher: EventMatcher | None = Field(
        default=None,
        description="Predicate gating which channel event resumes this session (AND of present fields). Omit to resume on any event for the trigger.",
    )


# ---------------------------------------------------------------------------
# Tool descriptors
# ---------------------------------------------------------------------------


#: What a session outside prime reads about the tools that act on a whole trigger (review of #874, B1).
_TRIGGER_REACH_NOTE = (
    " From a session outside the default workspace (prime) a trigger with a "
    "subscription that starts or feeds a session outside your own workspace "
    "answers ``type=trigger_not_found``, as an unknown trigger does."
)


TOOL_LIST = make_tool(
    id="list",
    toolset_id=TRIGGER_TOOLSET_ID,
    purpose="List triggers as an array of Trigger objects.",
    when=(
        "Use when you need to enumerate triggers, optionally filtered by "
        "``kind`` (delayed / scheduled / webhook / channel) or ``enabled`` "
        "(true / false); not for one known id (use ``get``)."
    ),
    args_schema=_ListArgs.model_json_schema(),
    examples=[
        ToolExample(args={}, returns="all triggers"),
        ToolExample(
            args={"kind": "scheduled", "enabled": True},
            returns="enabled cron triggers",
        ),
    ],
    required_role="user",
)

TOOL_GET = make_tool(
    id="get",
    toolset_id=TRIGGER_TOOLSET_ID,
    purpose="Get one trigger by id, returning the full Trigger row.",
    when=(
        "Use when you already know the trigger id and want its full row; "
        "to enumerate or filter many triggers use ``list`` instead. "
        "A missing id returns ``type=trigger_not_found``."
    ),
    args_schema=_IdArgs.model_json_schema(),
    examples=[
        ToolExample(args={"id": "trg-1"}, returns="the Trigger row"),
    ],
    required_role="user",
)

TOOL_CREATE = make_tool(
    id="create",
    toolset_id=TRIGGER_TOOLSET_ID,
    # The trigger row, then its claim (a separate lease write): a Stop that cancelled in between would leave a trigger
    # that never fires. A Stop waits for the call (same for update, delete and fire_now below).
    interruptible=False,
    purpose="Create a new trigger and return the created Trigger.",
    when=(
        "Use when registering a new trigger. ``config`` is the "
        "discriminated union (delayed: ``{kind:'delayed', fire_at}``, an ISO 8601 "
        "time, read as UTC when it has no zone; "
        "scheduled: ``{kind:'scheduled', cron, timezone?, catchup?}``). "
        "Duplicate slug surfaces as ``type=trigger_slug_conflict``; bad "
        "cron / timezone surface as ``type=cron_invalid`` / "
        "``type=timezone_invalid``."
    ),
    args_schema=_CreateArgs.model_json_schema(),
    examples=[
        ToolExample(
            args={
                "slug": "nightly",
                "name": "Nightly digest",
                "config": {"kind": "scheduled", "cron": "0 2 * * *"},
            },
            returns="the created Trigger",
        ),
    ],
    required_role="user",
)

TOOL_UPDATE = make_tool(
    id="update",
    toolset_id=TRIGGER_TOOLSET_ID,
    interruptible=False,
    purpose="Partially update a trigger and return the updated Trigger.",
    when=(
        "Use when changing a trigger's name / description / enabled / "
        "config. Only supplied fields change. Changing ``config.kind`` is "
        "rejected with ``type=trigger_kind_immutable`` (delete + recreate "
        "to switch kinds). For subscription fields use "
        "``update_subscription`` instead." + _TRIGGER_REACH_NOTE
    ),
    args_schema=_UpdateArgs.model_json_schema(),
    examples=[
        ToolExample(
            args={"id": "trg-1", "enabled": False},
            returns="the updated Trigger",
            note="disable without deleting",
        ),
    ],
    required_role="user",
)

TOOL_DELETE = make_tool(
    id="delete",
    toolset_id=TRIGGER_TOOLSET_ID,
    interruptible=False,
    purpose="Delete a trigger and cascade-delete its subscriptions.",
    when=(
        "Use when permanently removing a trigger; this cascade-deletes all "
        "its subscriptions. To remove a single subscription only, use "
        "``delete_subscription``. Returns ``{ok: true}`` or "
        "``type=trigger_not_found``." + _TRIGGER_REACH_NOTE
    ),
    args_schema=_IdArgs.model_json_schema(),
    examples=[
        ToolExample(args={"id": "trg-1"}, returns="{ok: true}"),
    ],
    required_role="user",
)

TOOL_FIRE_NOW = make_tool(
    id="fire_now",
    toolset_id=TRIGGER_TOOLSET_ID,
    interruptible=False,
    purpose="Synchronously fire a trigger now (operator/testing aid).",
    when=(
        "Use when you need to fire a trigger immediately, bypassing the "
        "scheduler, e.g. for testing dispatch. It dispatches every "
        "subscription kind, as the console's Fire now does, and returns "
        "``{skipped, fire_id, results: [...]}``: one result per enabled "
        "subscription (``subscription_id``, ``ok``, ``skipped``, "
        "``error_code``, ``error_message``, ``artefact_id``: the session it "
        "started or fed). A disabled trigger answers ``{skipped: true, "
        "fire_id: null, results: []}`` and fires nothing. A fire runs as the "
        "trigger's owner, so you may fire a trigger only when you rank at "
        "least as high as its owner (an admin fires any; with no calling "
        "session you may also fire your own); otherwise ``type=forbidden`` "
        "and nothing fires." + _TRIGGER_REACH_NOTE + " The reach check comes "
        "first, so such a trigger answers ``trigger_not_found``, not "
        "``forbidden``."
    ),
    args_schema=_IdArgs.model_json_schema(),
    examples=[
        ToolExample(
            args={"id": "trg-1"},
            returns="{skipped: false, fire_id, results: [...]}",
            note="bypasses the scheduler",
        ),
    ],
    required_role="user",
)

TOOL_LIST_SUBS = make_tool(
    id="list_subscriptions",
    toolset_id=TRIGGER_TOOLSET_ID,
    purpose="List the subscriptions bound to one trigger.",
    when=(
        "Use when you need all subscriptions for a given ``trigger_id``; "
        "for one known subscription use ``get_subscription``. Returns an "
        "array of Subscription rows, or ``type=trigger_not_found`` if the "
        "parent trigger doesn't exist." + SUBSCRIPTION_LIST_REACH_NOTE
    ),
    args_schema=_SubListArgs.model_json_schema(),
    examples=[
        ToolExample(
            args={"trigger_id": "trg-1"},
            returns="subscriptions of trg-1",
        ),
    ],
    required_role="user",
)

TOOL_GET_SUB = make_tool(
    id="get_subscription",
    toolset_id=TRIGGER_TOOLSET_ID,
    purpose="Get one subscription scoped to its trigger.",
    when=(
        "Use when you know both the ``trigger_id`` and the "
        "``subscription_id`` and want that subscription's full row; to "
        "enumerate all of a trigger's subscriptions use "
        "``list_subscriptions``. Missing returns "
        "``type=subscription_not_found``." + SUBSCRIPTION_REACH_NOTE
    ),
    args_schema=_SubIdArgs.model_json_schema(),
    examples=[
        ToolExample(
            args={"trigger_id": "trg-1", "subscription_id": "sub-1"},
            returns="the Subscription row",
        ),
    ],
    required_role="user",
)

TOOL_CREATE_SUB = make_tool(
    id="create_subscription",
    toolset_id=TRIGGER_TOOLSET_ID,
    purpose="Create a subscription bound to a trigger.",
    when=(
        "Use when attaching a delivery to a trigger. ``config`` is the "
        "subscription-kind discriminated union "
        "(agent_fresh_session / graph_fresh_session / session_append). Subs "
        "of kind ``parked_session`` are rejected with "
        "``type=parked_session_only_from_yield`` (only the "
        "``subscribe_to_trigger`` yielding tool may create those). From a "
        "session outside the default workspace (prime) the config may name "
        "only your own workspace or a session in it: another one answers "
        "type=not-found, as an unknown id does, and every fire checks it "
        "again."
    ),
    args_schema=_SubCreateArgs.model_json_schema(),
    examples=[
        ToolExample(
            args={
                "trigger_id": "trg-1",
                "config": {
                    "kind": "agent_fresh_session",
                    "workspace_id": "ws-1",
                    "agent_id": "code-reviewer",
                },
            },
            returns="the created Subscription",
        ),
    ],
    required_role="user",
)

TOOL_UPDATE_SUB = make_tool(
    id="update_subscription",
    toolset_id=TRIGGER_TOOLSET_ID,
    purpose="Partially update a subscription and return it.",
    when=(
        "Use when changing a subscription's payload_template / parallelism "
        "/ enabled / description; only supplied fields are modified. To "
        "change trigger-level fields use ``update``. Missing returns "
        "``type=subscription_not_found``." + SUBSCRIPTION_REACH_NOTE
    ),
    args_schema=_SubUpdateArgs.model_json_schema(),
    examples=[
        ToolExample(
            args={
                "trigger_id": "trg-1",
                "subscription_id": "sub-1",
                "enabled": False,
            },
            returns="the updated Subscription",
        ),
    ],
    required_role="user",
)

TOOL_DELETE_SUB = make_tool(
    id="delete_subscription",
    toolset_id=TRIGGER_TOOLSET_ID,
    purpose="Delete one subscription, scoped to its trigger.",
    when=(
        "Use when removing a single subscription while keeping the trigger "
        "and its other subscriptions; to remove the whole trigger use "
        "``delete``. Returns ``{ok: true}`` or "
        "``type=subscription_not_found``." + SUBSCRIPTION_REACH_NOTE
    ),
    args_schema=_SubIdArgs.model_json_schema(),
    examples=[
        ToolExample(
            args={"trigger_id": "trg-1", "subscription_id": "sub-1"},
            returns="{ok: true}",
        ),
    ],
    required_role="user",
)

TOOL_SUBSCRIBE = make_tool(
    id="subscribe_to_trigger",
    toolset_id=TRIGGER_TOOLSET_ID,
    purpose=(
        "Park the calling session until ``trigger_id`` next fires, then "
        "resume with the fire context as the tool result."
    ),
    when=(
        "Use when the current session should wait for a trigger to fire "
        "before continuing. With no session to park (a chat) it returns "
        "``type=unsupported-surface``. Validates the trigger exists and is "
        "enabled, otherwise returns ``type=trigger_not_found_or_disabled``. "
        "Persists a one-shot ``parked_session`` Subscription bound to the "
        "caller's (session_id, tool_call_id); the matching dispatcher "
        "deletes the row after delivering the resume payload. The call "
        "answers ``{ok, fire_context, payload}`` on the fire, "
        "``{timed_out: true, elapsed_seconds, trigger_id}`` at the global "
        "yield cap and ``{cancelled: true, cancel_reason}`` on a cancel."
    ),
    args_schema=_SubscribeArgs.model_json_schema(),
    examples=[
        ToolExample(
            args={"trigger_id": "trg-1"},
            returns="{ok, fire_context, payload} on the next fire",
            note="yielding: parks the session until the trigger fires",
        ),
    ],
    yields=True,
    required_role="user",
)

TOOL_SUBSCRIBE_CHANNEL = make_tool(
    id="subscribe_to_channel_event",
    toolset_id=TRIGGER_TOOLSET_ID,
    purpose=(
        "Park the calling session until a matching channel event fires the "
        "given channel trigger, then resume with the event in the tool result."
    ),
    when=(
        "Use when a workflow should wait for a specific channel event (e.g. a "
        "slash command in a watched room) before continuing. With no "
        "session to park (a chat) it returns ``type=unsupported-surface``. "
        "Validates the trigger exists, is enabled, and is a channel-kind "
        "trigger, else returns ``type=trigger_not_found_or_disabled``. "
        "Persists a one-shot parked_session subscription carrying the "
        "matcher; the channel dispatch loop resumes the session only on a "
        "matching event. The call answers ``{ok, fire_context, payload}`` "
        "on that event (the event is ``fire_context.event``), "
        "``{timed_out: true, elapsed_seconds, trigger_id}`` at the global "
        "yield cap and ``{cancelled: true, cancel_reason}`` on a cancel."
    ),
    args_schema=_SubscribeChannelArgs.model_json_schema(),
    examples=[
        ToolExample(
            args={
                "trigger_id": "trg-ch-1",
                "event_matcher": {
                    "event_type": "command.invoked",
                    "command_name": "approve",
                },
            },
            returns="{ok, fire_context: {event, ...}, payload} on the next matching event",
            note="yielding: parks the session until a matching channel event",
        ),
    ],
    yields=True,
    required_role="user",
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
    async def _handler(
        arguments: dict[str, Any], *, ctx: ToolContext | None = None,
    ) -> ToolCallResult:
        try:
            args = _ListArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_validation(exc)
        deps = _make_deps(storage_provider, claim_engine, event_bus)
        items = await list_triggers(
            kind=args.kind, enabled=args.enabled, deps=deps,
        )
        # The webhook token only for its owner or an admin (A-20 round 2).
        caller = _caller(ctx)
        return _ok([await view_for(t, caller, storage_provider) for t in items])

    return _handler


def _caller(ctx: ToolContext | None) -> "PrincipalRef | None":
    """The calling run's identity, recorded as the owner of the trigger or subscription it saves (security review A-20).

    A call with no ``ToolContext`` (the MCP endpoint dispatches handlers without one) records no owner, so what it saves fires
    as an ordinary user: fail closed.
    """
    return ctx.initiated_by if ctx is not None else None


def _fire_caller(ctx: ToolContext | None) -> Any:
    """Who fires a trigger by hand, for the rank rule (ticket 01a16e65-12d1): the calling run's principal
    (:func:`primer.agent.inner_dispatch.run_principal`, an ordinary user when the run names nobody), or, with no ``ToolContext`` (the MCP endpoint),
    the request's actor on ``current_actor``. ``None`` (no actor at all) ranks as an ordinary user (:func:`primer.authz.principal_rank`)."""
    if ctx is not None:
        from primer.agent.inner_dispatch import run_principal

        return run_principal(ctx)
    from primer.mcp.server import current_actor

    return current_actor.get()


def _make_get_handler(
    storage_provider: "StorageProvider",
    claim_engine: Any,
    event_bus: Any,
) -> ToolHandler:
    async def _handler(
        arguments: dict[str, Any], *, ctx: ToolContext | None = None,
    ) -> ToolCallResult:
        try:
            args = _IdArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_validation(exc)
        deps = _make_deps(storage_provider, claim_engine, event_bus)
        try:
            trigger = await get_trigger(trigger_id=args.id, deps=deps)
        except TriggerNotFound as exc:
            return _err(str(exc), error_type="trigger_not_found")
        # The webhook token only for its owner or an admin (A-20 round 2).
        return _ok(await view_for(trigger, _caller(ctx), storage_provider))

    return _handler


def _make_create_handler(
    storage_provider: "StorageProvider",
    claim_engine: Any,
    event_bus: Any,
) -> ToolHandler:
    async def _handler(
        arguments: dict[str, Any], *, ctx: ToolContext | None = None,
    ) -> ToolCallResult:
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
                owner=_caller(ctx),
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
    async def _handler(
        arguments: dict[str, Any], *, ctx: ToolContext | None = None,
    ) -> ToolCallResult:
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
                owner=_caller(ctx),
                # From a session outside prime: refused as an unknown trigger when a subscription of it reaches outside (review of #874, B1).
                reach=await caller_reach(storage_provider, ctx),
                deps=deps,
            )
        except TriggerNotFound as exc:
            return _err(str(exc), error_type="trigger_not_found")
        except TriggerKindImmutable as exc:
            return _err(str(exc), error_type="trigger_kind_immutable")
        except TriggerForbidden as exc:
            return _err(str(exc), error_type="forbidden")
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
    async def _handler(arguments: dict[str, Any], *, ctx: ToolContext | None = None) -> ToolCallResult:
        try:
            args = _IdArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_validation(exc)
        deps = _make_deps(storage_provider, claim_engine, event_bus)
        try:
            await delete_trigger(trigger_id=args.id, reach=await caller_reach(storage_provider, ctx), deps=deps)
        except TriggerNotFound as exc:
            return _err(str(exc), error_type="trigger_not_found")
        return _ok({"ok": True})

    return _handler


def _make_fire_now_handler(
    storage_provider: "StorageProvider",
    claim_engine: Any,
    event_bus: Any,
    scheduler: Any = None,
    workspace_registry: Any = None,
) -> ToolHandler:
    async def _handler(arguments: dict[str, Any], *, ctx: ToolContext | None = None) -> ToolCallResult:
        try:
            args = _IdArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_validation(exc)
        # The one call here that DISPATCHES: it fires through service.fire_now with what POST /v1/triggers/{id}/fire_now passes (the claim engine,
        # the event bus, the scheduler and the workspace registry), so a fresh-session or session_append subscription is delivered from the tool
        # as from the route. Without the registry every one of them answered dispatch_failed (ticket 01a16ddb, ruling 1).
        deps = ServiceDeps(
            storage_provider=storage_provider,
            claim_engine=claim_engine,
            event_bus=event_bus,
            scheduler=scheduler,
            workspace_registry=workspace_registry,
        )
        # Reach first (an out-of-reach trigger is unknown), then the rank rule against the trigger's owner (ticket 01a16e65-12d1): from a session
        # the run's principal, which never gets the owner rule; with no calling session (the MCP endpoint) the request's actor, which does.
        try:
            result = await fire_now(
                trigger_id=args.id, caller=_fire_caller(ctx), from_run=ctx is not None, reach=await caller_reach(storage_provider, ctx), deps=deps,
            )
        except TriggerNotFound as exc:
            return _err(str(exc), error_type="trigger_not_found")
        except TriggerOutranked as exc:
            return _err(str(exc), error_type="forbidden")
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
    async def _handler(
        arguments: dict[str, Any], *, ctx: ToolContext | None = None,
    ) -> ToolCallResult:
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
        # From a session outside prime, a subscription whose target is out of reach is left out (ticket 01a16c22).
        items = await list_subscriptions(
            trigger_id=args.trigger_id, reach=await caller_reach(storage_provider, ctx), deps=deps,
        )
        # A non-manager sees only the owner's display and role (A-20).
        caller = _caller(ctx)
        return _ok([await view_for(s, caller, storage_provider) for s in items])

    return _handler


def _make_get_sub_handler(
    storage_provider: "StorageProvider",
    claim_engine: Any,
    event_bus: Any,
) -> ToolHandler:
    async def _handler(
        arguments: dict[str, Any], *, ctx: ToolContext | None = None,
    ) -> ToolCallResult:
        try:
            args = _SubIdArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_validation(exc)
        deps = _make_deps(storage_provider, claim_engine, event_bus)
        try:
            sub = await get_subscription(
                trigger_id=args.trigger_id,
                subscription_id=args.subscription_id,
                # Out of the caller's reach answers as an unknown subscription (ticket 01a16c22).
                reach=await caller_reach(storage_provider, ctx),
                deps=deps,
            )
        except SubscriptionNotFound as exc:
            return _err(str(exc), error_type="subscription_not_found")
        return _ok(await view_for(sub, _caller(ctx), storage_provider))

    return _handler


def _make_create_sub_handler(
    storage_provider: "StorageProvider",
    claim_engine: Any,
    event_bus: Any,
) -> ToolHandler:
    async def _handler(
        arguments: dict[str, Any], *, ctx: ToolContext | None = None,
    ) -> ToolCallResult:
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
                owner=_caller(ctx),
                # A session outside prime may name only its own workspace or a session in it; the row records that reach for every fire.
                reach=await caller_reach(storage_provider, ctx),
                deps=deps,
            )
        except TriggerNotFound as exc:
            return _err(str(exc), error_type="trigger_not_found")
        except ParkedSessionOnlyFromYield as exc:
            return _err(str(exc), error_type="parked_session_only_from_yield")
        except SubscriptionTargetNotFound as exc:
            return _err(str(exc), error_type="not-found")
        return _ok(sub)

    return _handler


def _make_update_sub_handler(
    storage_provider: "StorageProvider",
    claim_engine: Any,
    event_bus: Any,
) -> ToolHandler:
    async def _handler(
        arguments: dict[str, Any], *, ctx: ToolContext | None = None,
    ) -> ToolCallResult:
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
                owner=_caller(ctx),
                # A subscription whose target is out of the caller's reach answers as an unknown one (review of #874, C1).
                reach=await caller_reach(storage_provider, ctx),
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
    async def _handler(arguments: dict[str, Any], *, ctx: ToolContext | None = None) -> ToolCallResult:
        try:
            args = _SubIdArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_validation(exc)
        deps = _make_deps(storage_provider, claim_engine, event_bus)
        try:
            await delete_subscription(
                trigger_id=args.trigger_id,
                subscription_id=args.subscription_id,
                # Out of the caller's reach answers as an unknown subscription and deletes nothing (ticket 01a16c22).
                reach=await caller_reach(storage_provider, ctx),
                deps=deps,
            )
        except SubscriptionNotFound as exc:
            return _err(str(exc), error_type="subscription_not_found")
        return _ok({"ok": True})

    return _handler


def _trigger_wake_key(session_id: str, trigger_id: str) -> str:
    """The event key a session parked on a trigger subscription waits on: ``trigger:{session_id}:{trigger_id}``.

    The key used to be ``trigger:{trigger_id}``, shared by every session subscribed to that trigger. The park stamp fences a single park, but a graph park is
    exempt from it, so one session's fire woke another session's graph park with the first one's payload, past the second's own subscription checks (the A-20
    rank guard, a channel subscription's matcher). The dispatcher publishes the key the park STORED, so a park in flight keeps the key it has (ticket
    01a1208d, #702 review B1).
    """
    return f"trigger:{session_id}:{trigger_id}"


def _no_session_to_park(tool: str, ctx: ToolContext | None) -> ToolCallResult | None:
    """``unsupported-surface`` for a call with no session to park (a chat, or no context), else ``None``; asked before the trigger is looked up.

    As ``system__wait_for_event`` answers it. It used to be ``trigger_not_found_or_disabled``, the answer to an unknown trigger, and was asked
    after the lookup, so an unknown trigger and a chat read alike (ticket 01a16ddb, ruling 6). Nothing is written: a row with no session to wake
    would only be skipped by the next fire.
    """
    if ctx is not None and ctx.session_id is not None:
        return None
    return _err(
        f"{tool} needs a workspace session to park; it is not available on this surface",
        error_type="unsupported-surface",
    )


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
        ctx: ToolContext | None = None,
    ) -> ToolCallResult | Yielded:
        try:
            args = _SubscribeArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_validation(exc)
        if (refusal := _no_session_to_park("subscribe_to_trigger", ctx)) is not None:
            return refusal

        trigger_storage = storage_provider.get_storage(Trigger)
        trigger = await trigger_storage.get(args.trigger_id)
        if trigger is None or not trigger.enabled:
            return _err(
                f"trigger {args.trigger_id!r} does not exist or is disabled",
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
            owner=_caller(ctx),
            # The target is the calling session itself, always within its reach; recorded so the fire checks it like every other row.
            reach=await caller_reach(storage_provider, ctx),
        )
        await storage_provider.get_storage(Subscription).create(sub)
        return Yielded(
            tool_name="subscribe_to_trigger",
            event_key=_trigger_wake_key(ctx.session_id, args.trigger_id),
            timeout=None,  # honour the global yield cap
            resume_metadata={
                "subscription_id": sub_id,
                "trigger_id": args.trigger_id,
            },
        )

    return _handler


def _make_subscribe_channel_handler(
    storage_provider: "StorageProvider",
) -> ToolHandler:
    """Yielding-tool handler for ``subscribe_to_channel_event``.

    Generalizes :func:`_make_subscribe_handler` to a channel trigger gated
    by an :class:`EventMatcher`. The session parks and only a matching
    channel event resumes it via the existing ``parked_session`` dispatcher
    path; the matcher is stored on the one-shot :class:`Subscription` so the
    channel dispatch loop can honour it before resuming.

    The resume key (``trigger:<session_id>:<id>``) and ``resume_metadata`` shape match
    ``subscribe_to_trigger`` so the existing resume bridge works unchanged.

    The Subscription row is written BEFORE returning :class:`Yielded` so a
    fire racing the park still finds the row.
    """

    async def _handler(
        arguments: dict[str, Any],
        *,
        ctx: ToolContext | None = None,
    ) -> ToolCallResult | Yielded:
        try:
            args = _SubscribeChannelArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_validation(exc)
        if (refusal := _no_session_to_park("subscribe_to_channel_event", ctx)) is not None:
            return refusal
        try:
            validate_text_pattern(
                args.event_matcher.text_pattern
                if args.event_matcher is not None
                else None
            )
        except EventMatcherPatternError as exc:
            return _err(str(exc), error_type="validation-error")

        trigger_storage = storage_provider.get_storage(Trigger)
        trigger = await trigger_storage.get(args.trigger_id)
        if (
            trigger is None
            or not trigger.enabled
            or trigger.config.kind != TriggerKind.CHANNEL.value
        ):
            return _err(
                f"channel trigger {args.trigger_id!r} does not exist, is "
                "disabled, or is not a channel-kind trigger",
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
            event_matcher=args.event_matcher,
            payload_template=None,
            parallelism="skip",  # field unused for parked_session
            enabled=True,
            created_at=datetime.now(timezone.utc),
            owner=_caller(ctx),
            reach=await caller_reach(storage_provider, ctx),
        )
        await storage_provider.get_storage(Subscription).create(sub)
        return Yielded(
            tool_name="subscribe_to_channel_event",
            event_key=_trigger_wake_key(ctx.session_id, args.trigger_id),
            timeout=None,  # honour the global yield cap
            resume_metadata={
                "subscription_id": sub_id,
                "trigger_id": args.trigger_id,
            },
        )

    return _handler


def subscribe_resume(
    yield_metadata: dict[str, Any],
    event_payload: Any,
    ctx: Any,
) -> ToolCallResult:
    """Format the woken park of ``subscribe_to_trigger`` and ``subscribe_to_channel_event``, as ``wait_for_event_resume`` does (ticket 01a16ddb, ruling 2).

    * a fire: what the ``parked_session`` dispatcher delivered, ``{ok, fire_context, payload}`` (a channel trigger's ``fire_context`` carries
      its ``event``; with no ``payload_template`` the payload is the fire context);
    * the yield cap: ``{timed_out: true, elapsed_seconds, trigger_id}``;
    * a cancel: ``{cancelled: true, cancel_reason}``.

    Storage-free, as ``wait_for_event_resume`` is: a fire deletes its one-shot ``parked_session`` subscription, and one a timeout or a cancel
    left behind is skipped and deleted as an orphan by the next fire that reaches it (the trigger's next fire; for a channel subscription, the
    next event its matcher matches); a disabled trigger keeps it until the trigger is deleted. A wake that reaches the park's
    key from the event log carries an event envelope, not a fire: it is masked as ``wait_for_event_resume`` masks one (stored secrets masked, a
    ``canvas.event``'s ``value`` and ``data`` removed: the input never leaves its session, #872).
    """
    del ctx  # the result needs nothing beyond the wake and the park's metadata
    from primer.events.redaction import redact_payload
    from primer.events.visibility import payload_without_input
    from primer.model.yield_ import YieldCancelled, YieldTimeout

    if isinstance(event_payload, YieldTimeout):
        return _ok({
            "timed_out": True,
            "elapsed_seconds": event_payload.elapsed_seconds,
            "trigger_id": yield_metadata.get("trigger_id"),
        })
    if isinstance(event_payload, YieldCancelled):
        return _ok({
            "cancelled": True,
            "cancel_reason": event_payload.reason,
        })
    if isinstance(event_payload, dict) and isinstance(event_payload.get("event_type"), str) and "payload" in event_payload:
        event_payload = {
            **event_payload,
            "payload": redact_payload(payload_without_input(event_payload["event_type"], event_payload["payload"])),
        }
    return _ok(event_payload)


from primer.worker.yield_resume_registry import register_resume_hook  # noqa: E402

# The provider stamps a park with the tool's bare name (InternalToolsetProvider.call), so the hooks are keyed by both names.
register_resume_hook("subscribe_to_trigger", subscribe_resume)
register_resume_hook("subscribe_to_channel_event", subscribe_resume)


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------


def build_trigger_toolset_provider(
    *,
    storage_provider: "StorageProvider",
    claim_engine: Any = None,
    event_bus: Any = None,
    scheduler: Any = None,
    workspace_registry: Any = None,
    toolset_id: str = TRIGGER_TOOLSET_ID,
) -> InternalToolsetProvider:
    """Construct the ``trigger`` internal toolset.

    ``scheduler`` and ``workspace_registry`` are used only by ``fire_now``, which dispatches: the fresh-session and ``session_append``
    dispatchers need the registry to reach a session's on-disk slot. The lifespan passes what the REST ``fire_now`` route reads from
    ``app.state``; without them those subscriptions answer ``dispatch_failed`` from the tool.
    """
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
            _make_fire_now_handler(storage_provider, claim_engine, event_bus, scheduler, workspace_registry),
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
        # NOTE: ``subscribe_to_trigger`` moved to the ``workspace_ext``
        # reserved toolset (it parks a workspace session; keep it out of
        # chat context). Its descriptor (TOOL_SUBSCRIBE) + handler factory
        # (_make_subscribe_handler) remain defined in this module and are
        # imported by ``primer.toolset.workspace_ext``.
    }
    # create, update and delete are gated by default like the crud tools they twin (audit G6-1), so their approval cards show what the crud
    # tools' cards show. Function-local: primer.toolset.crud imports this module.
    from primer.toolset.crud import apply_twin_previews

    apply_twin_previews(TRIGGER_TOOLSET_ID, registry)
    logger.info(
        "trigger toolset assembled with %d tools (id=%s)",
        len(registry),
        toolset_id,
    )
    return InternalToolsetProvider(toolset_id=toolset_id, registry=registry)


__all__ = ["TRIGGER_TOOLSET_ID", "build_trigger_toolset_provider"]

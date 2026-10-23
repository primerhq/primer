"""``canvas`` reserved internal toolset: draw on the session's canvas and read what the user did there (pivot P5, Tasks 9 and 10; design 8.3).

Tool catalog (3 tools)
----------------------

* ``canvas_get``   - the canvas as a Canvas Lang program (or the stored JSON), the values the user set and the events since the last read.
* ``canvas_apply`` - one atomic batch of Canvas Lang statements (or JSON ops); every bad line is reported with its signature, and a batch
  with any error applies nothing.
* ``canvas_wait``  - PAUSES the turn until the user acts on the canvas: a one-shot ``EventSubscription`` on ``canvas.event``, parked like
  ``wait_for_event``; it returns at once when a matching event arrived since the agent's last read. An id that names no element is refused
  first, as in ``canvas_get``, so a typo never parks.

All three act on the CALLING session's canvas (``ToolContext.session_id``), so they are role ``user``, need a workspace session (they are
dropped from chat tool contexts and are not exposable over MCP), and have no argument that names another session. ``canvas_apply`` is not
interruptible (the snapshot write, then the record and the announcement); the other two are.

The state, its rules and its announcements live in :class:`primer.canvas.service.CanvasService`; this module is the agent's door to it:
argument models, the error shapes an agent can act on, and the descriptions (copied character for character from the plan).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from primer.canvas.decompile import MAX_EVENT_LINES, decompile, preorder
from primer.canvas.lang import CanvasLangError, LangError, parse_program
from primer.canvas.ops import (
    MAX_BATCH_STATEMENTS,
    MAX_OPS_CHARS,
    MAX_SOURCE_CHARS,
    CanvasOp,
    ensure_ops_fit,
    ops_errors,
    ops_to_statements,
)
from primer.canvas.service import (
    CanvasApplyRefused,
    CanvasRevConflict,
    CanvasService,
)
from primer.canvas.wait import (
    CANVAS_WAIT_TOOL,
    canvas_wait_event_key,
    canvas_wait_filter,
    canvas_wait_matches,
    consume_events,
)
from primer.model.chat import Tool, ToolCallResult, ToolExample
from primer.model.event import EventSubscription, SessionWakeSink
from primer.model.except_ import ConflictError, NotFoundError
from primer.model.yield_ import ToolContext, YieldCancelled, YieldTimeout, Yielded
from primer.toolset._describe import make_tool
from primer.toolset._system_common import _err_from_validation
from primer.toolset.internal import InternalToolsetProvider, ToolHandler
from primer.worker.yield_resume_registry import ResumeContext, register_resume_hook

if TYPE_CHECKING:
    from primer.int.storage_provider import StorageProvider

logger = logging.getLogger(__name__)

CANVAS_TOOLSET_ID = "canvas"

#: An element id as the JSON schema states it (``primer.canvas.lang.IDENT_RE`` without its Python-only ``\Z`` anchor; a test pins that
#: the two accept the same words).
ID_PATTERN = r"^[a-z][a-z0-9_]{0,31}$"
_MAX_IDS = 50

_NO_SESSION = "the canvas belongs to a workspace session; this call has none"

_ElementId = Annotated[str, Field(pattern=ID_PATTERN)]
#: Every kind of event a canvas element sends (``primer.model.canvas.CANVAS_EVENT_KINDS``; a test pins that they are the same).
_EventKind = Literal["click", "change", "emit", "set_value", "play", "pause", "ended", "open"]


# ===========================================================================
# Results
# ===========================================================================


def _dump(payload: Any) -> str:
    """JSON for the model: readable (a canvas in any language is not escaped to \\uXXXX, which costs tokens) and never fails on a stray type."""
    return json.dumps(payload, ensure_ascii=False, default=str)


def _ok(payload: Any) -> ToolCallResult:
    return ToolCallResult(output=_dump(payload), is_error=False)


def _error(error_type: str, message: str, **more: Any) -> ToolCallResult:
    return ToolCallResult(output=_dump({"type": error_type, "message": message, **more}), is_error=True)


def _no_session() -> ToolCallResult:
    return _error("unsupported-surface", _NO_SESSION)


def _scope(ctx: ToolContext | None) -> tuple[str, str] | None:
    """``(session_id, workspace_id)`` of the calling session, or ``None`` for a call that has none (a chat, a bare call)."""
    if ctx is None or not ctx.session_id or not ctx.workspace_id:
        return None
    return ctx.session_id, ctx.workspace_id


def _entries(errors: list[LangError] | tuple[LangError, ...]) -> list[dict[str, Any]]:
    """Errors as an agent reads them: ``{"line" or "op": n, "message", "expected"}`` (``expected`` only when there is a signature to show)."""
    return [
        {e.unit: e.line, "message": e.message, **({"expected": e.expected} if e.expected else {})} for e in errors
    ]


def _refused(errors: list[LangError] | tuple[LangError, ...], rev: int) -> ToolCallResult:
    return _error(
        "validation-error", f"{len(errors)} problem(s); nothing was applied", errors=_entries(errors), rev=rev,
    )


def _unknown_ids(ids: Sequence[str], elements: Mapping[str, Any]) -> ToolCallResult | None:
    """The refusal for ids that name no element of the canvas, or ``None`` when every id is there (``canvas_get`` and ``canvas_wait``
    judge their ``ids`` alike).

    Every unknown id is named, once and in the order it was given, so one reply is enough to fix all the typos.
    """
    missing = list(dict.fromkeys(element_id for element_id in ids if element_id not in elements))
    if not missing:
        return None
    noun = "element" if len(missing) == 1 else "elements"
    return _error("validation-error", f"no {noun} named " + ", ".join(f"'{element_id}'" for element_id in missing))


# ===========================================================================
# Arguments
# ===========================================================================


class _CanvasGetArgs(BaseModel):
    ids: list[_ElementId] | None = Field(
        default=None,
        max_length=_MAX_IDS,
        description="Element ids to read, with everything under them (at most 50). Omit for the whole canvas.",
    )
    full: bool = Field(
        default=False,
        description="Write Html bodies out in full instead of `...` (the Canvas Lang program only).",
    )
    format: Literal["lang", "json"] = Field(
        default="lang",
        description="`lang` (default) returns a Canvas Lang program; `json` returns the stored form.",
    )


class _CanvasApplyArgs(BaseModel):
    source: str | None = Field(
        default=None,
        max_length=MAX_SOURCE_CHARS,
        description=(
            f"Canvas Lang statements, one per line (at most {MAX_BATCH_STATEMENTS} statements and {MAX_SOURCE_CHARS} characters). "
            "Give this or ops."
        ),
    )
    ops: list[CanvasOp] | None = Field(
        default=None,
        description=(
            f"JSON ops instead of source (at most {MAX_BATCH_STATEMENTS} ops and {MAX_OPS_CHARS} characters of JSON), applied in order: "
            '{op: "set", id, type, props?, children?}, {op: "prop", id, prop, value}, {op: "append", id, children}, '
            '{op: "delete", ids}, {op: "clear"}. Give this or source.'
        ),
    )
    base_rev: int | None = Field(
        default=None,
        ge=0,
        description=(
            "The canvas rev your batch was written against. The batch is refused, and the user's changes since then listed, "
            "when the canvas is at another rev."
        ),
    )

    @field_validator("ops", mode="before")
    @classmethod
    def _ops_fit(cls, raw: Any) -> Any:
        return ensure_ops_fit(raw)


class _CanvasWaitArgs(BaseModel):
    ids: list[_ElementId] | None = Field(
        default=None,
        max_length=_MAX_IDS,
        description="Wait only for events on these elements (at most 50). Omit for any element.",
    )
    kinds: list[_EventKind] | None = Field(
        default=None,
        description="Wait for events of these kinds, whether or not they wake you. Omit to wait for an event that wakes you.",
    )
    timeout_s: float = Field(
        default=600,
        ge=1,
        le=86400,
        description="Give up after this many seconds (1 to 86400, default 600); the tool then returns {timeout: true}.",
    )


# ===========================================================================
# canvas_get
# ===========================================================================


def _make_canvas_get(service: CanvasService):
    async def _canvas_get(arguments: dict[str, Any], *, ctx: ToolContext | None = None) -> ToolCallResult:
        scope = _scope(ctx)
        if scope is None:
            return _no_session()
        try:
            args = _CanvasGetArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        session_id, workspace_id = scope
        row = await service.snapshot(session_id, workspace_id=workspace_id)
        state = service.state_of(row)
        refused = _unknown_ids(args.ids or [], state.elements)
        if refused is not None:
            return refused
        # The events are read from the same row as the canvas, so the program and its event list describe one moment. At most
        # MAX_EVENT_LINES are shown (and, in the program, the rest counted); only those are marked seen, so the others are still
        # "since your last read" the next time and no event is ever hidden from the agent for good.
        unseen = row.unseen_events()
        shown = unseen[:MAX_EVENT_LINES]
        if args.format == "json":
            keep = preorder(state, args.ids) if args.ids else list(state.elements)
            kept = set(keep)
            result: dict[str, Any] = {
                "rev": row.rev,
                "format": "json",
                "elements": {eid: state.elements[eid] for eid in keep},
                "values": {eid: value for eid, value in state.values.items() if eid in kept},
                "value_set_by": {eid: who for eid, who in state.value_set_by.items() if eid in kept},
                "events": [event.public() for event in shown],
            }
            if len(unseen) > len(shown):
                result["more_events"] = len(unseen) - len(shown)
        else:
            result = {
                "rev": row.rev,
                "format": "lang",
                "source": decompile(
                    state,
                    ids=args.ids,
                    full=args.full,
                    events=[event.public() for event in unseen],
                    now=datetime.now(UTC),
                ),
            }
        if shown:
            await service.mark_seen(session_id, shown[-1].seq)
        return _ok(result)

    return _canvas_get


# ===========================================================================
# canvas_apply
# ===========================================================================


def _make_canvas_apply(service: CanvasService):
    async def _canvas_apply(arguments: dict[str, Any], *, ctx: ToolContext | None = None) -> ToolCallResult:
        scope = _scope(ctx)
        if scope is None:
            return _no_session()
        session_id, workspace_id = scope
        try:
            args = _CanvasApplyArgs.model_validate(arguments)
        except ValidationError as exc:
            malformed = ops_errors(exc)
            if malformed:
                return _refused(malformed, (await service.snapshot(session_id, workspace_id=workspace_id)).rev)
            return _err_from_validation(exc)
        if (args.source is None) == (args.ops is None):
            return _error("validation-error", "give source or ops, not both and not neither")
        if args.source is not None:
            try:
                # Off the loop: a program of the largest size is a few hundred milliseconds of parsing.
                statements = await asyncio.to_thread(parse_program, args.source)
            except CanvasLangError as exc:
                return _refused(exc.errors, (await service.snapshot(session_id, workspace_id=workspace_id)).rev)
            unit: Literal["line", "op"] = "line"
        else:
            statements = ops_to_statements(args.ops or [])
            unit = "op"
        try:
            outcome = await service.apply(
                session_id=session_id,
                workspace_id=workspace_id,
                statements=statements,
                unit=unit,
                base_rev=args.base_rev,
                source="agent",
                by="agent",
                call_id=ctx.tool_call_id if ctx is not None else None,
            )
        except CanvasApplyRefused as exc:
            return _refused(exc.errors, exc.rev)
        except CanvasRevConflict as exc:
            return _error("conflict", str(exc), rev=exc.rev, changes=exc.changes)
        except ConflictError as exc:
            return _error("conflict", getattr(exc, "message", str(exc)))
        return _ok({
            "rev": outcome.rev,
            "changed": {
                "added": list(outcome.changed.added),
                "updated": list(outcome.changed.updated),
                "removed": list(outcome.changed.removed),
            },
            "warnings": list(outcome.warnings),
        })

    return _canvas_apply


# ===========================================================================
# canvas_wait
# ===========================================================================


def _make_canvas_wait(service: CanvasService, storage_provider: "StorageProvider"):
    async def _canvas_wait(arguments: dict[str, Any], *, ctx: ToolContext | None = None) -> ToolCallResult | Yielded:
        scope = _scope(ctx)
        if scope is None or ctx is None:
            return _no_session()
        try:
            args = _CanvasWaitArgs.model_validate(arguments)
        except ValidationError as exc:
            return _err_from_validation(exc)
        session_id, workspace_id = scope
        ids, kinds = list(args.ids or []), list(args.kinds or [])
        meta = {"ids": ids, "kinds": kinds}
        # Inside a graph node the key carries the node (two concurrent siblings of one superstep can share a raw provider id), as
        # ``ask_user``'s and ``sleep``'s do. Lazy import: primer.graph imports the toolsets transitively at package-init time.
        from primer.graph._node_identity import current_graph_node_id

        event_key = canvas_wait_event_key(ctx, current_graph_node_id())

        # The cursor is pinned FIRST, then the canvas is checked for events the agent has not read (#790 review). The other order
        # leaves a gap: a click recorded after the check and before the pin is in the log before the cursor, so the subscription
        # never sees it, and the check never did. In this order an event is either already in the canvas when it is checked, or is
        # emitted after the pin and so reaches the subscription; the dispatcher holds a delivery until the park is visible.
        subscriptions = storage_provider.get_storage(EventSubscription)
        created = await subscriptions.create(
            EventSubscription(
                description=f"canvas_wait park for session {session_id}",
                filter=canvas_wait_filter(session_id, ids=ids, kinds=kinds),
                sink=SessionWakeSink(event_key=event_key, session_id=session_id),
                managed_by="system",
            )
        )
        events = storage_provider.get_event_store()
        try:
            await events.set_cursor(created.id, await events.max_id())
            row = await service.snapshot(session_id, workspace_id=workspace_id)
        except Exception:
            await _discard(storage_provider, created.id)
            raise
        # An id that names no element would match no event and the wait would sit until its timeout: refuse it, as canvas_get does,
        # before any unread event is taken. The canvas read above (after the pin) judges the ids too, so the wait still reads once.
        refused = _unknown_ids(ids, row.elements)
        if refused is not None:
            await _discard(storage_provider, created.id)
            return refused
        waiting = [event.public() for event in row.unseen_events() if canvas_wait_matches(meta, event.public())]
        if waiting:
            await _discard(storage_provider, created.id)
            await consume_events(service, storage_provider, session_id, waiting)
            return _ok({"events": waiting, "values": row.values, "rev": row.rev})
        return Yielded(
            tool_name="",  # stamped by the provider
            event_key=event_key,
            timeout=args.timeout_s,
            resume_metadata={
                "subscription_id": created.id,
                "ids": ids,
                "kinds": kinds,
                "after_event_seq": row.event_seq,
            },
        )

    return _canvas_wait


async def _discard(storage_provider: "StorageProvider", subscription_id: str) -> None:
    """Delete a wait's subscription and its cursor when nothing will deliver to it. Best effort: the dispatcher collects an orphan anyway.

    The two deletes are tried apart (a cursor is worth removing even when the subscription is already gone), and a subscription that is
    already gone is not a problem: the dispatcher deletes a one-shot when it delivers.
    """
    try:
        await storage_provider.get_storage(EventSubscription).delete(subscription_id)
    except NotFoundError:
        pass
    except Exception:  # noqa: BLE001 - an orphaned one-shot is garbage-collected by the dispatcher
        logger.warning("canvas_wait: subscription %s could not be removed", subscription_id, exc_info=True)
    try:
        await storage_provider.get_event_store().delete_cursor(subscription_id)
    except Exception:  # noqa: BLE001 - a cursor with no subscription is harmless and pruned with the log
        logger.warning("canvas_wait: the cursor of subscription %s could not be removed", subscription_id, exc_info=True)


async def canvas_wait_resume(meta: dict[str, Any], payload: Any, rctx: ResumeContext) -> ToolCallResult:
    """Format the result of a woken ``canvas_wait`` park.

    ``payload`` is the event envelope the dispatcher published (the ``canvas.event`` itself is its ``payload`` key, and the result lists
    it under ``events``), or a synthetic timeout or cancel.

    With ``rctx.resolve_provider`` the result also carries the canvas's ``rev`` and ``values`` as they are now. The event that ended the
    wait is marked seen and its queued copy is dropped (a click that arrived while the turn was running was queued as a steer), so the
    agent gets the click once. A timeout or a cancel removes the wait's subscription, which nothing will deliver to. Without the
    registry the result is the event alone. A canvas that cannot be read never fails the resume: the wake is the answer.
    """
    service, storage_provider = await _resolve_canvas(rctx)
    state: dict[str, Any] = {}
    if service is not None and rctx.session_id:
        try:
            row = await service.current(rctx.session_id)
        except Exception:  # noqa: BLE001 - the wake is the answer; the extras are not
            logger.warning("canvas_wait: the canvas of session %s could not be read", rctx.session_id, exc_info=True)
        else:
            state = {"rev": row.rev if row is not None else 0, "values": row.values if row is not None else {}}
    if isinstance(payload, (YieldTimeout, YieldCancelled)):
        # Nothing will ever deliver to this wait's subscription (a delivery deletes its one-shot): remove it now instead of leaving it for
        # the dispatcher to collect the next time an event of the session passes, so a wait on a quiet element leaves nothing behind.
        stale = meta.get("subscription_id") if isinstance(meta, dict) else None
        if storage_provider is not None and isinstance(stale, str):
            await _discard(storage_provider, stale)
        if isinstance(payload, YieldTimeout):
            return _ok({"timeout": True, **state})
        return _ok({"cancelled": True, "cancel_reason": payload.reason})
    found = payload.get("payload") if isinstance(payload, dict) else None
    events = [found] if isinstance(found, dict) else []
    if service is not None and storage_provider is not None and rctx.session_id:
        await consume_events(service, storage_provider, rctx.session_id, events)
    return _ok({"events": events, **state})


async def _resolve_canvas(rctx: ResumeContext) -> tuple[CanvasService | None, Any]:
    """The canvas service (and its storage) behind the registry, or ``(None, None)`` where the resume has no registry to ask."""
    if rctx.resolve_provider is None:
        return None, None
    try:
        provider = await rctx.resolve_provider(CANVAS_TOOLSET_ID)
        return provider.service, provider.storage_provider
    except Exception:  # noqa: BLE001 - a resume without the extras is still a resume
        logger.warning("canvas_wait: the canvas toolset could not be resolved for session %s", rctx.session_id, exc_info=True)
        return None, None


register_resume_hook(CANVAS_WAIT_TOOL, canvas_wait_resume)


# ===========================================================================
# The toolset
# ===========================================================================


class CanvasToolsetProvider(InternalToolsetProvider):
    """The ``canvas`` toolset. ``service`` is the canvas service its tools act through; ``storage_provider`` is the storage behind it.

    Both are public so a resume hook, which reaches the toolset through the registry, can read the canvas and the session's queue
    without a second path to the same storage.
    """

    def __init__(
        self,
        registry: dict[str, tuple[Tool, ToolHandler]],
        *,
        service: CanvasService,
        storage_provider: "StorageProvider",
        toolset_id: str = CANVAS_TOOLSET_ID,
    ) -> None:
        super().__init__(toolset_id=toolset_id, registry=registry)
        self.service = service
        self.storage_provider = storage_provider


def build_canvas_toolset(
    *,
    storage_provider: "StorageProvider",
    event_bus: Any = None,
    workspace_registry: Any = None,
    toolset_id: str = CANVAS_TOOLSET_ID,
) -> CanvasToolsetProvider:
    """Construct the immutable ``canvas`` toolset.

    ``event_bus`` publishes the canvas frame on the session's delta channel and the record ticks; ``workspace_registry`` lets an
    operator's batch write its record into the session log. Both are optional (a bare or test build has neither): the canvas is
    written, announced on the platform event log and read just the same.
    """
    service = CanvasService(storage_provider, event_bus=event_bus, workspace_registry=workspace_registry)
    registry: dict[str, tuple[Tool, ToolHandler]] = {
        "canvas_get": (
            make_tool(
                id="canvas_get",
                toolset_id=toolset_id,
                purpose=(
                    "Read this session's canvas, the panel above the chat, as a Canvas Lang program you can edit and resend, with the "
                    "values the user set and the canvas events since your last read."
                ),
                when=(
                    "Use when you need the canvas's current state before changing it, or to see what the user clicked or chose. `...` "
                    "in an argument means keep the current value (Html bodies are shown that way unless full=true). ids limits the "
                    "program to those elements and their children; format=json returns the stored form."
                ),
                args_schema=_CanvasGetArgs.model_json_schema(),
                examples=[
                    ToolExample(args={}, returns='``{rev, format: "lang", source}``'),
                    ToolExample(
                        args={"ids": ["controls"], "full": True}, returns="the program of controls and its children",
                    ),
                ],
                requires_workspace=True,
                required_role="user",
            ),
            _make_canvas_get(service),
        ),
        "canvas_apply": (
            make_tool(
                id="canvas_apply",
                toolset_id=toolset_id,
                purpose=(
                    "Change this session's canvas, the panel above the chat, with one atomic batch of Canvas Lang statements (or JSON "
                    "ops); returns the new rev and what changed."
                ),
                when=(
                    "Use when the user should see or answer something richer than text: buttons, choices, images, audio, links or your "
                    "own HTML. One statement per line: `id = Type(args)` creates or replaces an element; `id.prop = value` sets one "
                    "property (null removes it); `id += [a, b]` appends children; `del a, b` removes elements and their children; "
                    "`clear` empties the canvas; `#` starts a comment. Types: Column(children, gap?, align?), Row(children, gap?, "
                    "align?, wrap?), Card(children, title?), Divider(), Text(text, style?), Button(label, style?, disabled?, wake?), "
                    "Select(label, options, value?, multiple?, wake?), Image(src, alt, fit?, height?), Audio(src, title?, loop?), "
                    "Link(label, href), Html(html, height?); every element also takes hidden and disabled. Ids are lowercase letters, "
                    "digits and _; root is a Column that always exists and every element must be reachable from it. Arguments go in "
                    "signature order; after one you skip, use name=value. Strings use double quotes; Html may use \"\"\"triple "
                    "quotes\"\"\". src is an https URL, file:<workspace path> or a small data: URI. A Button click wakes canvas_wait; "
                    "a Select change does not unless wake=true. If any line is wrong nothing is applied and "
                    "every error names its line and the expected signature: fix those lines and resend."
                ),
                args_schema=_CanvasApplyArgs.model_json_schema(),
                examples=[
                    ToolExample(
                        args={
                            "source": (
                                'root = Column([title, pick, go])\ntitle = Text("Pick a genre", "h2")\n'
                                'pick = Select("Genre", ["Jazz", "Lo-fi"], "Jazz")\ngo = Button("Play", "primary")'
                            ),
                        },
                        returns="``{rev: 1, changed: {added: [...]}}``",
                    ),
                    ToolExample(
                        args={"source": 'go.disabled = true\nnote = Text("Loading...", "caption")\nroot += [note]', "base_rev": 1},
                        returns="``{rev: 2, ...}``",
                        note="base_rev refuses the batch if the user changed the canvas since rev 1",
                    ),
                ],
                requires_workspace=True,
                required_role="user",
                # The snapshot write, then the record and canvas.updated: a cancel between would leave a canvas the transcript does not show.
                interruptible=False,
            ),
            _make_canvas_apply(service),
        ),
        "canvas_wait": (
            make_tool(
                id="canvas_wait",
                toolset_id=toolset_id,
                purpose=(
                    "Pause this turn until the user acts on the canvas; returns the matching events with the current values, or "
                    "{timeout: true}."
                ),
                when=(
                    "Use when your next step depends on a click or a choice and you have nothing else to do meanwhile. ids limits it "
                    "to some elements, kinds to some event kinds (click, change, emit, set_value, play, pause, ended, open); without "
                    "kinds it waits for an event that wakes (a Button click or an Html emit). It returns at once if such an event "
                    "arrived since your last canvas read. timeout_s defaults to 600 (at most 86400)."
                ),
                args_schema=_CanvasWaitArgs.model_json_schema(),
                examples=[
                    ToolExample(
                        args={"ids": ["ok", "cancel"]},
                        returns="``{events: [{id, element, kind, ...}], values, rev}``",
                        note="yielding; worker released",
                    ),
                ],
                yields=True,
                requires_workspace=True,
                required_role="user",
            ),
            _make_canvas_wait(service, storage_provider),
        ),
    }
    logger.info("canvas toolset assembled with %d tools (id=%s)", len(registry), toolset_id)
    return CanvasToolsetProvider(registry, service=service, storage_provider=storage_provider, toolset_id=toolset_id)


__all__ = [
    "CANVAS_TOOLSET_ID",
    "ID_PATTERN",
    "CanvasToolsetProvider",
    "build_canvas_toolset",
    "canvas_wait_resume",
]

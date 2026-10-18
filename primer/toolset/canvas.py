"""``canvas`` reserved internal toolset: draw on the session's canvas and read what the user did there (pivot P5, Task 9; design 8.3).

Tool catalog (2 tools so far)
-----------------------------

* ``canvas_get``   - the canvas as a Canvas Lang program (or the stored JSON), the values the user set and the events since the last read.
* ``canvas_apply`` - one atomic batch of Canvas Lang statements (or JSON ops); every bad line is reported with its signature, and a batch
  with any error applies nothing.

Both act on the CALLING session's canvas (``ToolContext.session_id``), so they are role ``user``, need a workspace session (they are
dropped from chat tool contexts and are not exposable over MCP), and have no argument that names another session. ``canvas_apply`` is not
interruptible (the snapshot write, then the record and the announcement); ``canvas_get`` is.

The state, its rules and its announcements live in :class:`primer.canvas.service.CanvasService`; this module is the agent's door to it:
argument models, the error shapes an agent can act on, and the descriptions (copied character for character from the plan).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from primer.canvas.decompile import MAX_EVENT_LINES, decompile, preorder
from primer.canvas.lang import CanvasLangError, LangError, parse_program
from primer.canvas.ops import (
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
from primer.model.chat import Tool, ToolCallResult, ToolExample
from primer.model.except_ import ConflictError
from primer.model.yield_ import ToolContext
from primer.toolset._describe import make_tool
from primer.toolset._system_common import _err_from_validation
from primer.toolset.internal import InternalToolsetProvider, ToolHandler

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
        description=f"Canvas Lang statements, one per line (at most {MAX_SOURCE_CHARS} characters). Give this or ops.",
    )
    ops: list[CanvasOp] | None = Field(
        default=None,
        description=(
            f"JSON ops instead of source (at most {MAX_OPS_CHARS} characters of JSON), applied in order: "
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
        for element_id in args.ids or []:
            if element_id not in state.elements:
                return _error("validation-error", f"no element named '{element_id}'")
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
                    "quotes\"\"\". src is an https URL, file:<workspace path> or a small data: URI. A Button click wakes you with a "
                    "<canvas_event> message; a Select change does not unless wake=true. If any line is wrong nothing is applied and "
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
    }
    logger.info("canvas toolset assembled with %d tools (id=%s)", len(registry), toolset_id)
    return CanvasToolsetProvider(registry, service=service, storage_provider=storage_provider, toolset_id=toolset_id)


__all__ = [
    "CANVAS_TOOLSET_ID",
    "ID_PATTERN",
    "CanvasToolsetProvider",
    "build_canvas_toolset",
]

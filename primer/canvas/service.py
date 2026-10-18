"""The canvas service: apply a batch, record a user's interaction, keep the row fenced (pivot P5, Tasks 6 and 7; design 8.5).

Every write to a ``SessionCanvas`` goes through this class and is ONE field-scoped ``patch_if`` fenced on the ``rev`` AND the
``event_seq`` that were read (the first write of a session is a ``create``). A writer that lost the race reads the fresh row and
decides again, up to five times, then raises ``ConflictError``. So an API pod recording a click and a worker applying an agent's
batch both land, ``rev`` and ``event_seq`` only ever increase, and no value is lost; nothing here ever writes the whole row.

After the write, and best effort (a canvas that has been written never fails because its announcement could not be made):

* the transcript record (``primer.canvas.records``), under the record rule of plan Decision 2;
* the platform event (``canvas.updated`` for a batch, ``canvas.event`` for an interaction);
* the ``canvas`` frame on the session's delta channel, so a console refetches the snapshot.

A refused batch or a refused event writes nothing and announces nothing.

The workspace an announcement is made in is the one the canvas row names (``SessionCanvas.workspace_id``), never the caller's: the
non-admin event feed is read per workspace, and the record is appended through the workspace's own log, so a caller's idea of the
workspace (a route's path segment, a stale context) must not move where a session's canvas speaks. The caller's ``workspace_id`` only
names the workspace of a canvas that does not exist yet, and is the row's from then on.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic_core import to_jsonable_python

from primer.canvas.catalog import EVENT_KINDS
from primer.canvas.decompile import event_line
from primer.canvas.html_check import check_html_scripts
from primer.canvas.lang import Clear, LangError, Statement, clip
from primer.canvas.ops import statements_to_ops
from primer.canvas.records import append_canvas_record
from primer.canvas.settings import read_canvas_settings
from primer.canvas.state import (
    CanvasState,
    Changed,
    Limits,
    apply_batch,
    check_json_value,
    check_select_value,
    json_equal,
)
from primer.events.recorder import recorder_for
from primer.model.canvas import MAX_EMIT_DATA_BYTES, MAX_EVENT_VALUE_CHARS, CanvasEvent, CanvasSettings, SessionCanvas
from primer.model.except_ import ConflictError, NotFoundError
from primer.model.workspace_session import SessionMessageKind
from primer.storage.cas import patch_if_checked
from primer.tap.delta import publish_canvas_frame

logger = logging.getLogger(__name__)

#: Attempts a write makes before it gives up: each rejected fence means another writer got in between the read and the write.
_ATTEMPTS = 5
_CONFLICT = "the canvas changed too often; try again"

#: The ops a ``canvas_op`` record carries; a bigger batch is recorded without them (``ops_truncated``).
_MAX_OPS_RECORD_BYTES = 16384

#: The event kinds that set the value of their element (and so move ``rev``).
_VALUE_CHANGING = ("change", "set_value")
_AUDIO_KINDS = ("play", "pause", "ended")

_WAKE_WINDOW = timedelta(seconds=60)
_EMIT_NAME = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
_MAX_POSITION_SECONDS = 86400

#: The prop an event's ``label`` is read from, per element type.
_LABEL_PROP = {"Button": "label", "Select": "label", "Link": "label", "Audio": "title"}


@dataclass(frozen=True)
class ApplyOutcome:
    """What an applied batch did: the new ``rev``, which elements changed, and the warnings the batch carried."""

    rev: int
    changed: Changed
    warnings: tuple[str, ...]


class CanvasApplyRefused(Exception):
    """The batch has errors, so nothing was applied. ``errors`` names every one; ``rev`` is the canvas rev that was current."""

    def __init__(self, errors: Sequence[LangError], rev: int) -> None:
        self.errors = tuple(errors)
        self.rev = rev
        super().__init__("; ".join(error.render() for error in self.errors) or "the batch was refused")


class CanvasRevConflict(Exception):
    """The caller's ``base_rev`` is not the canvas's rev, so nothing was applied.

    ``changes`` lists what happened since ``base_rev``: the value changes the users made that the ring still holds (as event
    lines) and, last, a count of the updates it cannot name.
    """

    def __init__(self, rev: int, base_rev: int, changes: list[str]) -> None:
        self.rev = rev
        self.base_rev = base_rev
        self.changes = changes
        super().__init__(f"the canvas is at rev {rev}, not {base_rev}; nothing was applied")


class CanvasEventRefused(Exception):
    """The event was refused, so nothing was recorded. ``code`` is what a caller maps to a status; ``message`` is for a person."""

    def __init__(
        self,
        code: Literal["unknown_element", "kind_not_allowed", "invalid_value", "element_disabled", "rate_limited"],
        message: str,
    ) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def _json_time(moment: datetime) -> str:
    """A moment as the stored document spells it, so a patch needs no canonicalizing rewrite."""
    return to_jsonable_python(moment)


def _json_length(value: Any) -> int:
    """Characters of the compact JSON of ``value`` (which has passed ``check_json_value``, so this cannot raise)."""
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False))


def _option_order(props: dict[str, Any], chosen: list[str]) -> list[str]:
    """``chosen`` in the order of the Select's options (a choice is a set; the stored form must not depend on the click order)."""
    picked = set(chosen)
    return [value for value in (o if isinstance(o, str) else o[0] for o in props["options"]) if value in picked]


def _is_unavailable(row: SessionCanvas, element: str) -> bool:
    """Whether the user cannot act on ``element``: it is hidden or disabled, or a container above it is hidden."""
    if row.elements[element]["props"].get("hidden") or row.elements[element]["props"].get("disabled"):
        return True
    parent_of = {child: parent for parent, el in row.elements.items() for child in el.get("children", ())}
    seen: set[str] = set()
    current = element
    while current in parent_of and current not in seen:
        seen.add(current)
        current = parent_of[current]
        if row.elements.get(current, {}).get("props", {}).get("hidden"):
            return True
    return False


@dataclass(frozen=True)
class _Accepted:
    """An event the checks passed: what the stored event is made of."""

    etype: str
    source: str
    wake: bool
    label: str | None
    value: Any
    name: str | None
    data: Any
    position: float | None


def _check_event(
    row: SessionCanvas,
    settings: CanvasSettings,
    now: datetime,
    *,
    element: str,
    kind: str,
    value: Any,
    name: Any,
    data: Any,
    position: Any,
) -> _Accepted:
    """Judge an event against the canvas as it is now; raise ``CanvasEventRefused`` (the first problem found) or return it."""
    found = row.elements.get(element) if isinstance(element, str) else None
    if found is None:
        raise CanvasEventRefused("unknown_element", f"'{clip(str(element))}' is not on the canvas any more")
    etype = found["type"]
    allowed = EVENT_KINDS.get(etype, {})
    if not isinstance(kind, str) or kind not in allowed:
        raise CanvasEventRefused("kind_not_allowed", f"{etype} elements do not send {clip(str(kind))} events")
    if _is_unavailable(row, element):
        raise CanvasEventRefused("element_disabled", f"'{element}' is disabled or hidden")
    props = found["props"]
    kept_value: Any = None
    kept_name: str | None = None
    kept_data: Any = None
    kept_position: float | None = None
    if kind == "change":
        if props.get("multiple") and isinstance(value, (list, tuple)) and len(value) > len(props["options"]):
            # check_select_value walks the list once per option, so a request body full of choices would hold the loop; a list longer
            # than the options can only repeat one or invent one, and is refused without being read.
            raise CanvasEventRefused(
                "invalid_value", f"'{element}' has {len(props['options'])} options; the value lists {len(value)}",
            )
        problem = check_select_value(element, props, value)
        if problem:
            raise CanvasEventRefused("invalid_value", problem)
        kept_value = _option_order(props, value) if props.get("multiple") else value
        if _json_length(kept_value) > MAX_EVENT_VALUE_CHARS:
            raise CanvasEventRefused("invalid_value", f"a value is at most {MAX_EVENT_VALUE_CHARS} characters")
    elif kind == "set_value":
        if value is not None:
            problem = check_json_value(value)
            if problem:
                raise CanvasEventRefused("invalid_value", problem)
            if _json_length(value) > MAX_EVENT_VALUE_CHARS:
                raise CanvasEventRefused("invalid_value", f"a value is at most {MAX_EVENT_VALUE_CHARS} characters")
            kept_value = copy.deepcopy(value)
    elif kind == "emit":
        if not isinstance(name, str) or _EMIT_NAME.fullmatch(name) is None:
            raise CanvasEventRefused(
                "invalid_value", "an emit name is 1 to 64 letters, digits, _ . : or -, and does not start with /",
            )
        kept_name = name
        if data is not None:
            problem = check_json_value(data)
            if problem:
                raise CanvasEventRefused("invalid_value", problem)
            if len(json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")) > MAX_EMIT_DATA_BYTES:
                raise CanvasEventRefused("invalid_value", f"emit data is at most {MAX_EMIT_DATA_BYTES} bytes of JSON")
            kept_data = copy.deepcopy(data)
    elif kind in _AUDIO_KINDS and position is not None:
        # Compared before it is converted: an int too big for a float must be refused, not raise.
        if (
            isinstance(position, bool)
            or not isinstance(position, (int, float))
            or (isinstance(position, float) and not math.isfinite(position))
            or not 0 <= position <= _MAX_POSITION_SECONDS
        ):
            raise CanvasEventRefused("invalid_value", f"a position is 0 to {_MAX_POSITION_SECONDS} seconds")
        kept_position = float(position)
    wake = bool(props["wake"]) if "wake" in props else allowed[kind]
    if wake:
        recent = [t for t in row.wake_times if (t if t.tzinfo else t.replace(tzinfo=UTC)) > now - _WAKE_WINDOW]
        if len(recent) >= settings.max_waking_events_per_minute:
            raise CanvasEventRefused(
                "rate_limited",
                f"The agent takes at most {settings.max_waking_events_per_minute} canvas wakes a minute. "
                "Wait a moment, then try again.",
            )
    label_prop = _LABEL_PROP.get(etype)
    return _Accepted(
        etype=etype,
        source=f"html:{element}" if etype == "Html" else "control",
        wake=wake,
        label=props.get(label_prop) if label_prop else None,
        value=kept_value,
        name=kept_name,
        data=kept_data,
        position=kept_position,
    )


class CanvasService:
    """The one door to the session canvas (see the module docstring).

    ``event_bus`` publishes the delta frame and the record ticks; ``workspace_registry`` reaches the session log for the records an
    operator's batch needs (without it only a turn's own batches are recorded). ``script_check`` judges the scripts of an Html
    element; it is called OFF the event loop (a worker thread), because V8 blocks until it answers. ``clock`` is for tests.
    """

    def __init__(
        self,
        storage_provider: Any,
        *,
        event_bus: Any = None,
        workspace_registry: Any = None,
        script_check: Callable[[str], list[str]] | None = check_html_scripts,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._sp = storage_provider
        self._bus = event_bus
        self._registry = workspace_registry
        self._script_check = script_check
        self._clock = clock or (lambda: datetime.now(UTC))

    def _store(self) -> Any:
        return self._sp.get_storage(SessionCanvas)

    def _now(self) -> datetime:
        moment = self._clock()
        return moment if moment.tzinfo else moment.replace(tzinfo=UTC)

    # ---- reading ---------------------------------------------------------------------------------------------------------------

    async def snapshot(self, session_id: str, *, workspace_id: str) -> SessionCanvas:
        """The stored row, else a new empty one that is NOT stored (reading a canvas never creates it)."""
        row = await self._store().get(session_id)
        return row if row is not None else SessionCanvas(id=session_id, workspace_id=workspace_id)

    @staticmethod
    def state_of(row: SessionCanvas) -> CanvasState:
        """The canvas of ``row`` as a pure ``CanvasState``: copies, so what a caller does to it never reaches the row."""
        return CanvasState(row.rev, copy.deepcopy(row.elements), copy.deepcopy(row.values), dict(row.value_set_by))

    async def current(self, session_id: str) -> SessionCanvas | None:
        """The stored row, or ``None`` when the session has none. For a caller that has no workspace id to give ``snapshot``."""
        return await self._store().get(session_id)

    async def unseen_events(self, session_id: str) -> list[CanvasEvent]:
        """The events the agent has not read yet: those after ``seen_event_seq``, oldest first."""
        row = await self._store().get(session_id)
        return row.unseen_events() if row is not None else []

    async def mark_seen(self, session_id: str, through_seq: int) -> None:
        """Raise ``seen_event_seq`` to ``through_seq`` (never lower it, never past the last event). One fenced patch of that field."""
        store = self._store()
        for _ in range(_ATTEMPTS):
            row = await store.get(session_id)
            target = min(through_seq, row.event_seq) if row is not None else 0
            if row is None or target <= row.seen_event_seq:
                return
            try:
                written = await patch_if_checked(
                    store, session_id, {"seen_event_seq": target}, where={"seen_event_seq": [row.seen_event_seq]},
                )
            except NotFoundError:
                return                       # the session's canvas was deleted: nothing is left to mark
            if written is not None:
                return
        logger.warning("canvas %s: seen_event_seq stayed behind %d after %d rejected fences", session_id, through_seq, _ATTEMPTS)

    # ---- batches ---------------------------------------------------------------------------------------------------------------

    async def apply(
        self,
        *,
        session_id: str,
        workspace_id: str,
        statements: Sequence[Statement],
        unit: Literal["line", "op"],
        base_rev: int | None,
        source: Literal["agent", "operator"],
        by: str,
        call_id: str | None = None,
    ) -> ApplyOutcome:
        """Apply ``statements`` as one atomic batch and write the result.

        Raises ``CanvasRevConflict`` when ``base_rev`` is given and is not the canvas's rev, ``CanvasApplyRefused`` when the batch
        has errors (nothing is written, recorded or announced), and ``ConflictError`` when other writers kept getting in first.
        Every accepted batch moves ``rev`` by one, whether or not it changed anything (the outcome's ``changed`` says).
        """
        statements = list(statements)
        store = self._store()
        for _ in range(_ATTEMPTS):
            row = await store.get(session_id)
            rev = row.rev if row is not None else 0
            if base_rev is not None and base_rev != rev:
                raise self._conflict(row, rev, base_rev)
            settings = await read_canvas_settings(self._sp)
            limits = Limits(settings.max_elements, settings.max_html_bytes, settings.max_canvas_bytes)
            before = self.state_of(row) if row is not None else CanvasState.empty()
            result = await asyncio.to_thread(
                apply_batch, before, statements, limits=limits, unit=unit, script_check=self._script_check,
            )
            if result.errors or result.state is None:
                raise CanvasApplyRefused(result.errors, rev)
            written = await self._write_batch(store, row, result.state, session_id=session_id, workspace_id=workspace_id)
            if written is None:
                continue                     # another writer got in first: read the fresh row and decide again
            await self._announce_batch(
                written, result.state, result.changed, statements, source=source, by=by, call_id=call_id,
                session_id=session_id, workspace_id=written.workspace_id,
            )
            return ApplyOutcome(written.rev, result.changed, result.warnings)
        raise ConflictError(_CONFLICT)

    async def clear(self, *, session_id: str, workspace_id: str, by: str) -> ApplyOutcome:
        """Empty the canvas: an operator's ``clear`` batch."""
        return await self.apply(
            session_id=session_id, workspace_id=workspace_id, statements=[Clear(1)], unit="line", base_rev=None,
            source="operator", by=by,
        )

    @staticmethod
    def _conflict(row: SessionCanvas | None, rev: int, base_rev: int) -> CanvasRevConflict:
        events = row.events if row is not None else []
        changes = [
            event_line(event.model_dump(mode="json"))
            for event in events
            if event.kind in _VALUE_CHANGING and event.rev > base_rev
        ]
        unnamed = (rev - base_rev) - len(changes)
        if unnamed > 0:
            changes.append(f"and {unnamed} other canvas updates")
        return CanvasRevConflict(rev, base_rev, changes)

    async def _write_batch(
        self, store: Any, row: SessionCanvas | None, after: CanvasState, *, session_id: str, workspace_id: str,
    ) -> SessionCanvas | None:
        """The one write of a batch: ``create`` for a session's first, else a patch fenced on ``rev`` and ``event_seq``.

        ``None`` means the fence rejected it (or another writer created the row first, or the row was deleted): read again.
        """
        now = self._now()
        if row is None:
            fresh = SessionCanvas(
                id=session_id, workspace_id=workspace_id, rev=1, elements=after.elements, values=after.values,
                value_set_by=after.value_set_by, updated_at=now,
            )
            try:
                return await store.create(fresh)
            except ConflictError:
                return None
        patch = {
            "rev": row.rev + 1,
            "elements": after.elements,
            "values": after.values,
            "value_set_by": after.value_set_by,
            "updated_at": _json_time(now),
        }
        try:
            return await patch_if_checked(store, session_id, patch, where={"rev": [row.rev], "event_seq": [row.event_seq]})
        except NotFoundError:
            return None

    async def _announce_batch(
        self,
        written: SessionCanvas,
        after: CanvasState,
        changed: Changed,
        statements: Sequence[Statement],
        *,
        source: str,
        by: str,
        call_id: str | None,
        session_id: str,
        workspace_id: str,
    ) -> None:
        """The record, the platform event and the tap frame of a batch that has been written. Each is best effort and alone."""
        summary = {"added": list(changed.added), "updated": list(changed.updated), "removed": list(changed.removed)}
        record: dict[str, Any] = {"rev": written.rev, "source": source, "by": by, "changed": summary, **self._ops_of(statements)}
        if call_id is not None:
            record["call_id"] = call_id
        await append_canvas_record(
            storage_provider=self._sp, workspace_registry=self._registry, event_bus=self._bus, session_id=session_id,
            workspace_id=workspace_id, kind=SessionMessageKind.CANVAS_OP, payload=record,
        )
        try:
            await recorder_for(self._sp, self._bus).emit(
                "canvas.updated",
                workspace_id=workspace_id,
                session_id=session_id,
                actor=by,
                payload={
                    "rev": written.rev, "source": source, "by": by, "changed": summary, "element_count": after.element_count(),
                },
            )
        except Exception:  # noqa: BLE001 - the canvas is written; its announcement is advisory
            logger.warning("canvas %s: canvas.updated could not be recorded", session_id, exc_info=True)
        await self._publish_frame(session_id, written)

    @staticmethod
    def _ops_of(statements: Sequence[Statement]) -> dict[str, Any]:
        """The ``ops`` of a ``canvas_op`` record, or ``ops_truncated`` when they would not fit (or cannot be named)."""
        try:
            ops = statements_to_ops(statements)
            size = len(json.dumps(ops, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
        except (ValueError, TypeError):
            return {"ops_truncated": True}
        return {"ops": ops} if size <= _MAX_OPS_RECORD_BYTES else {"ops_truncated": True}

    async def _publish_frame(self, session_id: str, written: SessionCanvas) -> None:
        if self._bus is None:
            return
        await publish_canvas_frame(self._bus.publish, session_id=session_id, rev=written.rev, event_seq=written.event_seq)

    # ---- interactions ----------------------------------------------------------------------------------------------------------

    async def record_event(
        self,
        *,
        session_id: str,
        workspace_id: str,
        element: str,
        kind: str,
        by: str,
        value: Any = None,
        name: str | None = None,
        data: Any = None,
        position: float | None = None,
        client_event_id: str | None = None,
    ) -> CanvasEvent:
        """Validate a user's interaction, write it to the canvas and announce it.

        The server decides everything that matters: the element must exist and be available, the kind must be one it sends, the
        value must fit, a waking event must be within the per-minute limit, and the label comes from the element, never from the
        caller. Raises ``CanvasEventRefused`` (nothing is written) or ``ConflictError`` (other writers kept getting in first). A
        ``client_event_id`` already in the ring returns that event unchanged: a retried POST records once.

        A ``change`` or ``set_value`` that sets a different value moves ``rev``; a click, an emit, an audio event or a link open
        moves only ``event_seq``. The fields a kind does not use (a ``value`` sent with a click) are dropped, not stored.
        """
        store = self._store()
        for _ in range(_ATTEMPTS):
            row = await store.get(session_id)
            if row is None:
                raise CanvasEventRefused("unknown_element", f"'{clip(str(element))}' is not on the canvas any more")
            if client_event_id is not None:
                repeat = next((event for event in row.events if event.client_event_id == client_event_id), None)
                if repeat is not None:
                    return repeat
            settings = await read_canvas_settings(self._sp)
            now = self._now()
            accepted = _check_event(
                row, settings, now, element=element, kind=kind, value=value, name=name, data=data, position=position,
            )
            values, set_by, bumped = self._values_after(row, element, kind, accepted)
            seq = row.event_seq + 1
            rev = row.rev + (1 if bumped else 0)
            event = CanvasEvent(
                id=f"e{seq}", seq=seq, rev=rev, element=element, type=accepted.etype, kind=kind, source=accepted.source, by=by,
                wake=accepted.wake, at=now, value=accepted.value, name=accepted.name, data=accepted.data,
                position=accepted.position, label=accepted.label, client_event_id=client_event_id,
            )
            patch: dict[str, Any] = {
                "events": to_jsonable_python([*row.events, event][-settings.max_events:]),
                "event_seq": seq,
                "updated_at": _json_time(now),
            }
            if values is not row.values:
                patch["values"], patch["value_set_by"] = values, set_by
            if bumped:
                patch["rev"] = rev
            if accepted.wake:
                patch["wake_times"] = to_jsonable_python([*row.wake_times, now][-settings.max_waking_events_per_minute:])
            try:
                written = await patch_if_checked(store, session_id, patch, where={"rev": [row.rev], "event_seq": [row.event_seq]})
            except NotFoundError:
                raise CanvasEventRefused("unknown_element", f"'{element}' is not on the canvas any more") from None
            if written is None:
                continue                     # another writer got in first: read the fresh row and judge the event again
            await self._announce_event(written, event, session_id=session_id, workspace_id=written.workspace_id)
            return event
        raise ConflictError(_CONFLICT)

    @staticmethod
    def _values_after(
        row: SessionCanvas, element: str, kind: str, accepted: _Accepted,
    ) -> tuple[dict[str, Any], dict[str, str], bool]:
        """The ``values`` and ``value_set_by`` the event leaves, and whether they changed what the canvas shows (a rev bump).

        Returns ``row.values`` itself (the same object) when the event sets nothing.
        """
        values, set_by = row.values, row.value_set_by
        if kind in _VALUE_CHANGING:
            current = row.values[element] if element in row.values else row.elements[element]["props"].get("value")
            if accepted.value is None:                       # set_value(null): the Html element lets go of its value
                if element not in row.values:
                    return values, set_by, False
                return (
                    {k: v for k, v in row.values.items() if k != element},
                    {k: v for k, v in row.value_set_by.items() if k != element},
                    True,
                )
            if json_equal(accepted.value, current):         # the same value again: nothing changes, not even who set it
                return values, set_by, False
            return (
                {**row.values, element: copy.deepcopy(accepted.value)},
                {**row.value_set_by, element: "user"},
                True,
            )
        if kind in _AUDIO_KINDS:
            state: dict[str, Any] = {"playing": kind == "play", "position": accepted.position or 0.0}
            if kind == "ended":
                state["ended"] = True
            return {**row.values, element: state}, {**row.value_set_by, element: "user"}, False
        return values, set_by, False

    async def _announce_event(self, written: SessionCanvas, event: CanvasEvent, *, session_id: str, workspace_id: str) -> None:
        """The platform event and the tap frame of an interaction that has been written. Best effort, each alone."""
        try:
            await recorder_for(self._sp, self._bus).emit(
                "canvas.event",
                workspace_id=workspace_id,
                session_id=session_id,
                actor=event.by,
                payload=event.public(),
            )
        except Exception:  # noqa: BLE001 - the event is written; its announcement is advisory
            logger.warning("canvas %s: canvas.event could not be recorded", session_id, exc_info=True)
        await self._publish_frame(session_id, written)


__all__ = [
    "ApplyOutcome",
    "CanvasApplyRefused",
    "CanvasEventRefused",
    "CanvasRevConflict",
    "CanvasService",
]

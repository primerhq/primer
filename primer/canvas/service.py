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
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic_core import to_jsonable_python

from primer.canvas.decompile import event_line
from primer.canvas.html_check import check_html_scripts
from primer.canvas.lang import Clear, LangError, Statement
from primer.canvas.ops import statements_to_ops
from primer.canvas.records import append_canvas_record
from primer.canvas.settings import read_canvas_settings
from primer.canvas.state import CanvasState, Changed, Limits, apply_batch
from primer.events.recorder import recorder_for
from primer.model.canvas import SessionCanvas
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


def _json_time(moment: datetime) -> str:
    """A moment as the stored document spells it, so a patch needs no canonicalizing rewrite."""
    return to_jsonable_python(moment)


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
                session_id=session_id, workspace_id=workspace_id,
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


__all__ = [
    "ApplyOutcome",
    "CanvasApplyRefused",
    "CanvasRevConflict",
    "CanvasService",
]

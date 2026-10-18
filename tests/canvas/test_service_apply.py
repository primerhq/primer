"""Applying a batch to the session canvas: the fenced write, the record, canvas.updated and the tap frame (pivot P5, Task 6)."""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import ANY

import pytest
from pydantic import TypeAdapter

import primer.observability.metrics as metrics
from primer.canvas.html_check import check_html_scripts
from primer.canvas.lang import parse_program
from primer.canvas.ops import CanvasOp, ops_to_statements
from primer.canvas.service import CanvasApplyRefused, CanvasRevConflict, CanvasService
from primer.canvas.settings import write_canvas_settings
from primer.canvas.state import CanvasState, Limits, apply_batch
from primer.model.canvas import CANVAS_SETTINGS_ID, ROOT_ELEMENT, CanvasEvent, CanvasSettings, SessionCanvas
from primer.model.except_ import ConflictError
from primer.session.delegation import reset_delegation_sink, set_delegation_sink
from tests.canvas._fakes import StoreSpy, make_world, seed_session
from tests.conftest import _FakeStorageProvider

GOLDEN = Path(__file__).parent / "golden"
NOW = datetime(2026, 10, 18, 12, 0, 0, tzinfo=UTC)


def G(name: str) -> str:
    return (GOLDEN / name).read_text(encoding="utf-8")


async def _apply(w, source: str, *, sid: str = "s1", base_rev: int | None = None, by: str = "alice", src: str = "operator",
                 call_id: str | None = None, unit: str = "line"):
    return await w.service.apply(
        session_id=sid, workspace_id="ws-1", statements=parse_program(source), unit=unit, base_rev=base_rev, source=src, by=by,
        call_id=call_id,
    )


def _playlist_row(**fields: Any) -> SessionCanvas:
    """The playlist canvas as a stored row, with whatever else the test needs on it (rev, values, events...)."""
    state = apply_batch(CanvasState.empty(), parse_program(G("playlist_create.cl")), limits=Limits()).state
    return SessionCanvas(id="s1", workspace_id="ws-1", elements=state.elements, **fields)


def _change_event(seq: int, rev: int, *, value: str = "Lo-fi") -> CanvasEvent:
    return CanvasEvent(
        id=f"e{seq}", seq=seq, rev=rev, element="genre", type="Select", kind="change", source="control", by="alice", wake=False,
        at=NOW, value=value, label="Genre",
    )


def _click_event(seq: int, rev: int) -> CanvasEvent:
    return CanvasEvent(
        id=f"e{seq}", seq=seq, rev=rev, element="play", type="Button", kind="click", source="control", by="alice", wake=True,
        at=NOW, label="Play",
    )


class _Turn:
    """The recorder a running turn publishes (``DelegationRecorder``), reduced to what the canvas asks of it."""

    def __init__(self, session_id: str = "s1") -> None:
        self.session_id = session_id
        self.appended: list[tuple[str, dict]] = []

    async def append_turn_record(self, kind: Any, payload: dict) -> int | None:
        self.appended.append((kind, payload))
        return 7


class _InTurn:
    """``with _InTurn(turn):`` publishes ``turn`` as the delegation sink, the way ``run_one_session_turn`` does."""

    def __init__(self, turn: _Turn) -> None:
        self.turn = turn

    def __enter__(self) -> _Turn:
        self.token = set_delegation_sink(self.turn)
        return self.turn

    def __exit__(self, *exc: object) -> None:
        reset_delegation_sink(self.token)


# ---- the first apply ---------------------------------------------------------------------------------------------------------------


async def test_the_first_apply_creates_rev_1_and_announces_it():
    w = make_world()
    await seed_session(w)
    out = await w.service.apply(
        session_id="s1", workspace_id="ws-1", statements=parse_program(G("playlist_create.cl")),
        unit="line", base_rev=None, source="operator", by="alice",
    )
    row = await w.canvas("s1")
    assert out.rev == 1 == row.rev and row.elements["play"] == {"type": "Button", "props": {"label": "Play", "style": "primary"}}
    assert ("session:s1:delta", {"class": "canvas", "session_id": "s1", "rev": 1, "event_seq": 0}) in w.bus.published
    [ev] = await w.events("canvas.updated")
    assert ev.session_id == "s1" and ev.payload["rev"] == 1 and ev.payload["source"] == "operator" and ev.payload["element_count"] == 8


async def test_the_outcome_says_what_changed_and_the_row_holds_who_wrote_it_when():
    w = make_world()
    await seed_session(w)
    out = await _apply(w, G("playlist_create.cl"))
    assert out.changed.added == ("title", "cover", "controls", "genre", "play", "player", "more", "viz") and out.changed.updated == ("root",)
    assert out.warnings == ()
    row = await w.canvas("s1")
    assert (row.workspace_id, row.event_seq, row.seen_event_seq, row.events, row.wake_times) == ("ws-1", 0, 0, [], [])
    assert row.updated_at is not None


async def test_canvas_updated_names_who_and_what_without_the_canvas_itself():
    w = make_world()
    await seed_session(w)
    await _apply(w, G("playlist_create.cl"), by="alice")
    await _apply(w, G("playlist_update.cl"), by="bob", src="agent")
    first, second = await w.events("canvas.updated")
    assert first.actor == "alice" and first.workspace_id == "ws-1"
    assert second.actor == "bob" and second.payload == {
        "rev": 2, "source": "agent", "by": "bob", "element_count": 9,
        "changed": {"added": ["skip"], "updated": ["cover", "controls", "play", "player"], "removed": []},
    }


async def test_a_second_apply_is_one_fenced_patch_and_never_a_whole_row_write():
    w = make_world()
    await seed_session(w)
    await _apply(w, G("playlist_create.cl"))
    writes = StoreSpy(w)
    out = await _apply(w, G("playlist_update.cl"))
    assert out.rev == 2
    [one] = writes.patches
    assert set(one["patch"]) == {"rev", "elements", "values", "value_set_by", "updated_at"}
    assert one["patch"]["rev"] == 2
    assert one["where"] == {"rev": [1], "event_seq": [0]}, "fenced on both counters, so a click and a batch never overwrite each other"


async def test_a_batch_that_changes_nothing_still_moves_rev():
    """Decision 1 counts every accepted batch; the outcome's ``changed`` is how a caller sees that nothing changed."""
    w = make_world()
    await seed_session(w)
    await _apply(w, G("playlist_create.cl"))
    out = await _apply(w, 'title = Text("Pick a playlist", "h2")')
    assert out.rev == 2 and (out.changed.added, out.changed.updated, out.changed.removed) == ((), (), ())


async def test_rev_counts_the_accepted_batches():
    w = make_world()
    await seed_session(w)
    revs = [(await _apply(w, f'n{n} = Text("{n}")\nroot += [n{n}]')).rev for n in range(5)]
    assert revs == [1, 2, 3, 4, 5] and (await w.canvas("s1")).rev == 5


# ---- a refused batch ---------------------------------------------------------------------------------------------------------------


async def test_a_refused_batch_writes_nothing():
    w = make_world()
    await seed_session(w)
    with pytest.raises(CanvasApplyRefused) as exc:
        await _apply(w, 'a = Text("x")\nb = Buton("y")\nroot += [a, b]')
    assert [e.render() for e in exc.value.errors] == [
        "line 2: unknown element type 'Buton' (Column, Row, Card, Divider, Text, Button, Select, Image, Audio, Link, Html)",
    ]
    assert exc.value.rev == 0
    assert await w.canvas("s1") is None
    assert await w.events("canvas.updated") == [] and w.bus.published == [] and w.records("s1") == []


async def test_a_refused_batch_leaves_an_existing_canvas_untouched():
    w = make_world()
    await seed_session(w)
    await _apply(w, G("playlist_create.cl"))
    before = (await w.canvas("s1")).model_dump()
    published, records = len(w.bus.published), len(w.records("s1"))
    writes = StoreSpy(w)
    with pytest.raises(CanvasApplyRefused) as exc:
        await _apply(w, 'skip = Button("Skip")\nplay = Select("Which")')
    assert exc.value.rev == 1 and exc.value.errors[0].render().startswith("line 2: Select needs options as a list")
    assert writes.patches == [] and (await w.canvas("s1")).model_dump() == before
    assert len(w.bus.published) == published and len(w.records("s1")) == records and len(await w.events("canvas.updated")) == 1


async def test_op_errors_say_op_n():
    w = make_world()
    await seed_session(w)
    ops = TypeAdapter(list[CanvasOp]).validate_python([
        {"op": "set", "id": "a", "type": "Text", "props": {"text": "x"}},
        {"op": "set", "id": "b", "type": "Select", "props": {"label": "Genre", "options": "Jazz"}},
    ])
    with pytest.raises(CanvasApplyRefused) as exc:
        await w.service.apply(
            session_id="s1", workspace_id="ws-1", statements=ops_to_statements(ops), unit="op", base_rev=None, source="agent", by="agent",
        )
    assert exc.value.errors[0].render().startswith("op 2: Select needs options as a list (Select(label, options, value?, multiple?, wake?))")


# ---- base_rev ----------------------------------------------------------------------------------------------------------------------


async def test_a_stale_base_rev_is_refused_with_the_user_changes():   # Review Focus 2
    w = make_world()
    await seed_session(w)
    await w.sp.get_storage(SessionCanvas).create(
        _playlist_row(rev=3, values={"genre": "Lo-fi"}, value_set_by={"genre": "user"}, events=[_change_event(1, 3)], event_seq=1),
    )
    writes = StoreSpy(w)
    with pytest.raises(CanvasRevConflict) as exc:
        await _apply(w, 'skip = Button("Skip")\ncontrols += [skip]', base_rev=2)
    assert (exc.value.rev, exc.value.base_rev, exc.value.changes) == (3, 2, ['e1 genre change value="Lo-fi" by="alice"'])
    assert str(exc.value) == "the canvas is at rev 3, not 2; nothing was applied"
    row = await w.canvas("s1")
    assert row.rev == 3 and "skip" not in row.elements and writes.patches == []
    assert await w.events("canvas.updated") == [] and w.bus.published == []


async def test_a_conflict_counts_the_updates_it_cannot_name():
    w = make_world()
    await seed_session(w)
    # rev 2: a user choice (e1); rev 3: a batch, then a click (e2, no new rev); rev 4: a batch; rev 5: a user choice (e3).
    ring = [_change_event(1, 2, value="Jazz"), _click_event(2, 3), _change_event(3, 5)]
    await w.sp.get_storage(SessionCanvas).create(_playlist_row(rev=5, events=ring, event_seq=3))
    with pytest.raises(CanvasRevConflict) as exc:
        await _apply(w, 'skip = Button("Skip")\ncontrols += [skip]', base_rev=1)
    # rev 5 is four updates past rev 1: the two value changes are named, a click is not an update, and the two batches are counted.
    assert exc.value.changes == [
        'e1 genre change value="Jazz" by="alice"', 'e3 genre change value="Lo-fi" by="alice"', "and 2 other canvas updates",
    ]


async def test_a_base_rev_in_front_of_the_canvas_is_refused_without_a_list():
    w = make_world()
    await seed_session(w)
    await w.sp.get_storage(SessionCanvas).create(_playlist_row(rev=3))
    with pytest.raises(CanvasRevConflict) as exc:
        await _apply(w, 'skip = Button("Skip")\ncontrols += [skip]', base_rev=9)
    assert (exc.value.rev, exc.value.base_rev, exc.value.changes) == (3, 9, [])


async def test_the_current_base_rev_applies_and_so_does_zero_on_a_new_canvas():
    w = make_world()
    await seed_session(w)
    assert (await _apply(w, 'a = Text("x")\nroot += [a]', base_rev=0)).rev == 1
    assert (await _apply(w, 'b = Text("y")\nroot += [b]', base_rev=1)).rev == 2
    with pytest.raises(CanvasRevConflict):
        await _apply(w, 'c = Text("z")\nroot += [c]', base_rev=1)


async def test_a_user_value_survives_a_batch_that_leaves_its_element_alone():   # Review Focus 2
    w = make_world()
    await seed_session(w)
    await w.sp.get_storage(SessionCanvas).create(_playlist_row(rev=2, values={"genre": "Lo-fi"}, value_set_by={"genre": "user"}))
    await _apply(w, 'skip = Button("Skip")\ncontrols += [skip]', base_rev=2)
    row = await w.canvas("s1")
    assert (row.values, row.value_set_by) == ({"genre": "Lo-fi"}, {"genre": "user"})


async def test_the_warning_for_a_reset_value_reaches_the_outcome_and_the_row_drops_it():
    w = make_world()
    await seed_session(w)
    await w.sp.get_storage(SessionCanvas).create(_playlist_row(rev=2, values={"genre": "Lo-fi"}, value_set_by={"genre": "user"}))
    out = await _apply(w, 'genre = Select("Genre", ["Jazz"])')
    assert out.warnings == ("genre: the user's value was reset because the element changed",)
    row = await w.canvas("s1")
    assert (row.values, row.value_set_by) == ({}, {})


# ---- a concurrent writer -----------------------------------------------------------------------------------------------------------


async def test_a_concurrent_write_is_retried_on_the_fresh_row(caplog):
    w = make_world()
    await seed_session(w)
    await _apply(w, G("playlist_create.cl"))
    writes, state = StoreSpy(w), {"injected": False}
    drift_before = metrics.storage_cas_drift_total.labels("SessionCanvas")._value.get()

    async def racing(id, patch=None, *, where, set_paths=None, conn=None):  # noqa: A002
        if not state["injected"]:
            state["injected"] = True
            await _apply(w, 'extra = Text("added by someone else")\nroot += [extra]', src="agent", by="agent")   # lands first
        return await writes.real_patch_if(id, patch, where=where, set_paths=set_paths, conn=conn)

    writes.store.patch_if = racing
    with caplog.at_level(logging.ERROR):
        out = await _apply(w, 'skip = Button("Skip")\ncontrols += [skip]')
    row = await w.canvas("s1")
    assert out.rev == 3 == row.rev, "ours landed after the other one, not instead of it"
    assert "skip" in row.elements and "extra" in row.elements and row.elements["controls"]["children"] == ["genre", "play", "skip"]
    assert [e.payload["rev"] for e in await w.events("canvas.updated")] == [1, 2, 3]
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR], "a lost race is not the CAS drift alarm"
    assert metrics.storage_cas_drift_total.labels("SessionCanvas")._value.get() == drift_before


async def test_a_retry_that_keeps_losing_gives_up_and_writes_nothing():
    w = make_world()
    await seed_session(w)
    await _apply(w, G("playlist_create.cl"))
    store, calls = w.sp.get_storage(SessionCanvas), {"n": 0}
    real = store.patch_if

    async def contended(id, patch=None, *, where, set_paths=None, conn=None):  # noqa: A002
        calls["n"] += 1
        fresh = await store.get(id)
        await real(id, {"event_seq": fresh.event_seq + 1}, where={"event_seq": [fresh.event_seq]})   # somebody else always wins first
        return await real(id, patch, where=where, set_paths=set_paths, conn=conn)

    store.patch_if = contended
    with pytest.raises(ConflictError, match="the canvas changed too often; try again"):
        await _apply(w, G("playlist_update.cl"))
    assert calls["n"] == 5
    row = await w.canvas("s1")
    assert row.rev == 1 and "skip" not in row.elements
    assert len(await w.events("canvas.updated")) == 1


async def test_two_first_applies_both_land():
    w = make_world()
    await seed_session(w)
    store, state = w.sp.get_storage(SessionCanvas), {"injected": False}
    real_create = store.create

    async def racing_create(entity, *, conn=None):
        if not state["injected"]:
            state["injected"] = True
            other = apply_batch(CanvasState.empty(), parse_program('other = Text("the other batch")\nroot += [other]'), limits=Limits()).state
            await real_create(SessionCanvas(id="s1", workspace_id="ws-1", rev=1, elements=other.elements))
        return await real_create(entity, conn=conn)

    store.create = racing_create
    out = await _apply(w, 'mine = Text("my batch")\nroot += [mine]')
    row = await w.canvas("s1")
    assert out.rev == 2 == row.rev
    assert row.elements["root"]["children"] == ["other", "mine"]


async def test_the_other_side_of_the_race_is_a_deleted_session_canvas():
    """The row vanishes between the read and the patch (its session was deleted): the batch starts the canvas again."""
    w = make_world()
    await seed_session(w)
    await _apply(w, G("playlist_create.cl"))
    store = w.sp.get_storage(SessionCanvas)
    real, state = store.patch_if, {"deleted": False}

    async def vanishing(id, patch=None, *, where, set_paths=None, conn=None):  # noqa: A002
        if not state["deleted"]:
            state["deleted"] = True
            await store.delete(id)
        return await real(id, patch, where=where, set_paths=set_paths, conn=conn)

    store.patch_if = vanishing
    out = await _apply(w, 'a = Text("x")\nroot += [a]')
    assert out.rev == 1 and set((await w.canvas("s1")).elements) == {"root", "a"}


# ---- the record --------------------------------------------------------------------------------------------------------------------


async def test_inside_a_turn_the_record_goes_through_the_turns_writer():
    w = make_world()
    await seed_session(w, turn_status="running", last_seq=5)
    with _InTurn(_Turn()) as fake:
        await _apply(w, G("playlist_create.cl"), src="agent", by="agent", call_id="c1")
    assert fake.appended == [("canvas_op", {"rev": 1, "source": "agent", "by": "agent", "changed": ANY, "ops": ANY, "call_id": "c1"})]
    [(_, payload)] = fake.appended
    assert payload["changed"] == {
        "added": ["title", "cover", "controls", "genre", "play", "player", "more", "viz"], "updated": ["root"], "removed": [],
    }
    assert len(payload["ops"]) == 9 and payload["ops"][0] == {
        "op": "set", "id": "root", "type": "Column", "props": {},
        "children": ["title", "cover", "controls", "player", "more", "viz"],
    }
    assert w.records("s1") == [], "the turn's writer owns the seqs; nothing else writes the log"
    assert (await w.sessions.get("s1")).last_seq == 5, "and nothing reserved one"


async def test_a_turn_of_another_session_is_not_this_sessions_writer():
    w = make_world()
    await seed_session(w, turn_status="idle", last_seq=5)
    with _InTurn(_Turn(session_id="other")) as fake:
        await _apply(w, G("playlist_create.cl"))
    assert fake.appended == []
    assert [r["seq"] for r in w.records("s1", "canvas_op")] == [6]


async def test_outside_a_turn_the_record_takes_the_next_seq():
    w = make_world()
    await seed_session(w, turn_status="idle", last_seq=5)
    await _apply(w, G("playlist_create.cl"))
    [record] = w.records("s1", "canvas_op")
    assert record["seq"] == 6 and record["payload"]["rev"] == 1 and record["payload"]["source"] == "operator"
    assert record["payload"]["by"] == "alice" and "call_id" not in record["payload"]
    assert (await w.sessions.get("s1")).last_seq == 6
    assert ("session:s1:tick", {"seq": 6}) in w.bus.published
    await _apply(w, G("playlist_update.cl"))
    assert [r["seq"] for r in w.records("s1", "canvas_op")] == [6, 7] and (await w.sessions.get("s1")).last_seq == 7


async def test_a_claimable_session_takes_the_record_too():
    w = make_world()
    await seed_session(w, turn_status="claimable", last_seq=2)
    await _apply(w, G("playlist_create.cl"))
    assert [r["seq"] for r in w.records("s1", "canvas_op")] == [3]


async def test_while_a_turn_runs_an_operator_batch_writes_no_record(caplog):
    w = make_world()
    await seed_session(w, turn_status="running", last_seq=5)
    with caplog.at_level(logging.DEBUG, logger="primer.canvas.records"):
        out = await _apply(w, G("playlist_create.cl"))
    assert out.rev == 1
    assert w.records("s1") == [] and (await w.sessions.get("s1")).last_seq == 5
    assert len(await w.events("canvas.updated")) == 1 and any(f["class"] == "canvas" for k, f in w.bus.published if k == "session:s1:delta")
    assert "a turn is running; the canvas record is skipped" in caplog.text


async def test_a_session_that_is_gone_takes_no_record_and_the_batch_still_lands():
    w = make_world()
    out = await _apply(w, G("playlist_create.cl"), sid="nobody")
    assert out.rev == 1 and w.records("nobody") == []


async def test_without_a_registry_there_is_no_operator_record():
    w = make_world()
    service = CanvasService(w.sp, event_bus=w.bus)
    await seed_session(w)
    await service.apply(
        session_id="s1", workspace_id="ws-1", statements=parse_program(G("playlist_create.cl")), unit="line", base_rev=None,
        source="operator", by="alice",
    )
    assert w.records("s1") == [] and (await w.canvas("s1")).rev == 1


async def test_a_record_that_cannot_be_written_never_fails_the_batch(caplog):
    w = make_world()
    await seed_session(w)
    w.registry.workspace("ws-1").fail_writes = True
    with caplog.at_level(logging.WARNING):
        out = await _apply(w, G("playlist_create.cl"))
    assert out.rev == 1 and (await w.canvas("s1")).rev == 1
    assert len(await w.events("canvas.updated")) == 1 and any(k == "session:s1:delta" for k, _ in w.bus.published)
    assert "canvas_op" in caplog.text


async def test_a_bus_that_is_down_never_fails_the_batch():
    w = make_world()
    await seed_session(w)
    w.bus.fail = True
    out = await _apply(w, G("playlist_create.cl"))
    assert out.rev == 1 and (await w.canvas("s1")).rev == 1 and len(await w.events("canvas.updated")) == 1


async def test_an_event_log_that_is_down_never_fails_the_batch():
    w = make_world()
    await seed_session(w)

    async def down(**kwargs):
        raise RuntimeError("event log down")

    w.sp.get_event_store().append = down
    out = await _apply(w, G("playlist_create.cl"))
    assert out.rev == 1 and len(w.records("s1", "canvas_op")) == 1 and any(k == "session:s1:delta" for k, _ in w.bus.published)


async def test_a_turns_writer_that_fails_never_fails_the_batch(caplog):
    class _Broken(_Turn):
        async def append_turn_record(self, kind: Any, payload: dict) -> int | None:
            raise OSError("the workspace did not take the write")

    w = make_world()
    await seed_session(w, turn_status="running")
    with _InTurn(_Broken()), caplog.at_level(logging.WARNING):
        out = await _apply(w, G("playlist_create.cl"), src="agent", by="agent", call_id="c1")
    assert out.rev == 1 and len(await w.events("canvas.updated")) == 1


async def test_the_ops_of_a_big_batch_are_left_out_of_the_record_and_the_record_says_so():
    w = make_world()
    await seed_session(w)
    body = "x" * 9000
    await _apply(w, f'root = Column([a, b, c])\na = Html("{body}")\nb = Html("{body}")\nc = Html("{body}")')
    [record] = w.records("s1", "canvas_op")
    assert record["payload"]["ops_truncated"] is True and "ops" not in record["payload"]
    assert record["payload"]["changed"]["added"] == ["a", "b", "c"] and record["payload"]["rev"] == 1


async def test_the_ops_of_a_small_batch_are_kept():
    w = make_world()
    await seed_session(w)
    body = "x" * 6000
    await _apply(w, f'root = Column([a, b])\na = Html("{body}")\nb = Html("{body}")')
    [record] = w.records("s1", "canvas_op")
    assert "ops_truncated" not in record["payload"] and [op["id"] for op in record["payload"]["ops"]] == ["root", "a", "b"]


async def test_clear_is_an_operator_batch_that_empties_the_canvas():
    w = make_world()
    await seed_session(w)
    await _apply(w, G("playlist_create.cl"))
    out = await w.service.clear(session_id="s1", workspace_id="ws-1", by="alice")
    assert out.rev == 2 and sorted(out.changed.removed) == sorted(["title", "cover", "controls", "genre", "play", "player", "more", "viz"])
    row = await w.canvas("s1")
    assert row.elements == {"root": ROOT_ELEMENT} and row.values == {} and row.value_set_by == {}
    assert w.records("s1", "canvas_op")[-1]["payload"]["ops"] == [{"op": "clear"}]
    assert (await w.events("canvas.updated"))[-1].payload["source"] == "operator"


# ---- the settings and the checks ---------------------------------------------------------------------------------------------------


async def test_limits_come_from_the_settings():
    w = make_world()
    await seed_session(w)
    await write_canvas_settings(w.sp, CanvasSettings(id=CANVAS_SETTINGS_ID, max_elements=2))
    with pytest.raises(CanvasApplyRefused) as exc:
        await _apply(w, 'root = Column([a, b, c])\na = Text("1")\nb = Text("2")\nc = Text("3")')
    assert [e.message for e in exc.value.errors] == ["the batch would make 3 elements; the limit is 2"]
    assert await w.canvas("s1") is None


async def test_the_html_and_canvas_limits_come_from_the_settings_and_are_read_for_every_batch():
    w = make_world()
    await seed_session(w)
    big = "x" * 2000
    assert (await _apply(w, f'root = Column([v])\nv = Html("{big}")')).rev == 1
    await write_canvas_settings(w.sp, CanvasSettings(id=CANVAS_SETTINGS_ID, max_html_bytes=1024))
    with pytest.raises(CanvasApplyRefused) as exc:
        await _apply(w, f'w = Html("{big}")\nroot += [w]')
    assert [e.message for e in exc.value.errors] == ["w: Html is 2000 bytes; the limit is 1024"]
    await write_canvas_settings(w.sp, CanvasSettings(id=CANVAS_SETTINGS_ID, max_canvas_bytes=16384))
    with pytest.raises(CanvasApplyRefused) as exc:
        await _apply(w, "\n".join(f'h{n} = Html("{"y" * 5000}")' for n in range(4)) + "\nroot += [h0, h1, h2, h3]")
    assert exc.value.errors[0].message.startswith("the canvas would be ") and exc.value.errors[0].message.endswith("bytes; the limit is 16384")


async def test_the_script_check_is_the_real_one_by_default():
    assert CanvasService(_FakeStorageProvider())._script_check is check_html_scripts
    w = make_world()
    await seed_session(w)
    with pytest.raises(CanvasApplyRefused) as exc:
        await _apply(w, 'root = Column([v])\nv = Html("<script>var b = ;</script>")')
    assert exc.value.errors[0].render().startswith("line 2: v: script 1 does not parse")
    assert (await _apply(w, 'root = Column([v])\nv = Html("<script>var b = 1;</script>")')).rev == 1


async def test_a_script_problem_refuses_the_batch():
    w = make_world(script_check=lambda html: ["script 1 does not parse: Unexpected token ';'"])
    await seed_session(w)
    with pytest.raises(CanvasApplyRefused) as exc:
        await _apply(w, 'root = Column([v])\nv = Html("<script>x</script>")')
    assert [e.render() for e in exc.value.errors] == ["line 2: v: script 1 does not parse: Unexpected token ';'"]
    assert await w.canvas("s1") is None


async def test_the_script_check_runs_off_the_event_loop():
    seen: dict[str, Any] = {}

    def check(html: str) -> list[str]:
        try:
            asyncio.get_running_loop()
            seen["loop"] = True
        except RuntimeError:
            seen["loop"] = False
        seen["thread"] = threading.current_thread()
        return []

    w = make_world(script_check=check)
    await seed_session(w)
    await _apply(w, 'root = Column([v])\nv = Html("<b>hi</b>")')
    assert seen["loop"] is False and seen["thread"] is not threading.current_thread(), "V8 blocks; a loop thread must not wait for it"


async def test_no_script_check_is_a_service_without_one():
    w = make_world(script_check=None)
    await seed_session(w)
    assert (await _apply(w, 'root = Column([v])\nv = Html("<script>var b = ;</script>")')).rev == 1


# ---- reading -----------------------------------------------------------------------------------------------------------------------


async def test_a_session_without_a_canvas_has_an_empty_unsaved_snapshot():
    w = make_world()
    snap = await w.service.snapshot("s1", workspace_id="ws-1")
    assert (snap.id, snap.workspace_id, snap.rev, set(snap.elements)) == ("s1", "ws-1", 0, {"root"})
    assert await w.canvas("s1") is None, "reading a canvas never creates one"


async def test_the_snapshot_is_the_stored_row_and_state_of_copies_it():
    w = make_world()
    await seed_session(w)
    await _apply(w, G("playlist_create.cl"))
    snap = await w.service.snapshot("s1", workspace_id="ws-1")
    assert snap.rev == 1 and "genre" in snap.elements
    state = w.service.state_of(snap)
    assert (state.rev, state.elements, state.values, state.value_set_by) == (1, snap.elements, {}, {})
    state.elements["genre"]["props"]["label"] = "changed"
    state.values["x"] = 1
    assert snap.elements["genre"]["props"]["label"] == "Genre" and snap.values == {}, "state_of hands out copies"


# ---- the record rule, pinned in the data ----------------------------------------------------------------------------------------------


async def test_a_turn_that_starts_between_the_read_and_the_reservation_gets_no_record(caplog):
    """The guard is in the reservation itself, not only in the read before it: a worker that claims the session in between makes the
    reservation fail, and the row it finds next is running."""
    w = make_world()
    await seed_session(w, turn_status="idle", last_seq=5)
    sessions = w.sessions
    real, state = sessions.patch_if, {"claimed": False}

    async def claimed_first(id, patch=None, *, where, set_paths=None, conn=None):  # noqa: A002
        if not state["claimed"]:
            state["claimed"] = True
            await real(id, {"turn_status": "running"}, where={"turn_status": ["idle"]})          # a worker claims the session
        return await real(id, patch, where=where, set_paths=set_paths, conn=conn)

    sessions.patch_if = claimed_first
    with caplog.at_level(logging.DEBUG, logger="primer.canvas.records"):
        out = await _apply(w, G("playlist_create.cl"))
    row = await w.sessions.get("s1")
    assert out.rev == 1 and (row.turn_status, row.last_seq) == ("running", 5), "no seq was reserved under a running turn"
    assert w.records("s1") == [] and "a turn is running; the canvas record is skipped" in caplog.text


async def test_a_seq_a_steer_took_in_between_is_not_reused():
    w = make_world()
    await seed_session(w, turn_status="idle", last_seq=5)
    sessions = w.sessions
    real, state = sessions.patch_if, {"stolen": False}

    async def steer_first(id, patch=None, *, where, set_paths=None, conn=None):  # noqa: A002
        if not state["stolen"]:
            state["stolen"] = True
            await real(id, {"last_seq": 6}, where={"last_seq": [5]})                         # wake_session wrote its USER_INPUT at 6
        return await real(id, patch, where=where, set_paths=set_paths, conn=conn)

    sessions.patch_if = steer_first
    await _apply(w, G("playlist_create.cl"))
    assert [r["seq"] for r in w.records("s1", "canvas_op")] == [7] and (await w.sessions.get("s1")).last_seq == 7


async def test_a_session_that_is_deleted_under_the_reservation_takes_no_record():
    w = make_world()
    await seed_session(w, turn_status="idle", last_seq=5)
    sessions = w.sessions
    real, state = sessions.patch_if, {"deleted": False}

    async def deleted_first(id, patch=None, *, where, set_paths=None, conn=None):  # noqa: A002
        if not state["deleted"]:
            state["deleted"] = True
            await sessions.delete(id)
        return await real(id, patch, where=where, set_paths=set_paths, conn=conn)

    sessions.patch_if = deleted_first
    out = await _apply(w, G("playlist_create.cl"))
    assert out.rev == 1 and w.records("s1") == []


async def test_two_batches_at_once_both_land():
    w = make_world()
    await seed_session(w)
    await _apply(w, G("playlist_create.cl"))
    store = w.sp.get_storage(SessionCanvas)
    real_get = store.get

    async def slow_get(id, *, conn=None):  # noqa: A002
        row = await real_get(id)
        await asyncio.sleep(0)               # both writers read the row before either writes it
        return row

    store.get = slow_get
    one, two = await asyncio.gather(
        _apply(w, 'one = Text("1")\nroot += [one]'), _apply(w, 'two = Text("2")\nroot += [two]', src="agent", by="agent"),
    )
    row = await w.canvas("s1")
    assert row.rev == 3 and {"one", "two"} <= set(row.elements) and sorted([one.rev, two.rev]) == [2, 3]
    assert [e.payload["rev"] for e in await w.events("canvas.updated")] == [1, *sorted([one.rev, two.rev])]

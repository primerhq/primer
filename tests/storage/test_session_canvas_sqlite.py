"""The canvas service over the real SQLite backend, the zero-config default (pivot P5, Tasks 6 and 7).

The unit tests of the service run over the in-memory fake, whose ``patch_if`` is the pure-Python statement of the real one. This file
runs the same service over a file database, so what is written (the nested JSON of the elements, the ring of events with their
timestamps, the values the audio and Html events leave) is read back exactly as the service wrote it, and the fence is the database's.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import pytest_asyncio

from primer.canvas.lang import parse_program
from primer.canvas.service import CanvasEventRefused, CanvasRevConflict, CanvasService
from primer.model.canvas import SessionCanvas
from primer.storage.sqlite import SqliteStorageProvider

_PLAYLIST = Path(__file__).resolve().parents[1] / "canvas" / "golden" / "playlist_create.cl"
_BOUND_S = 30.0


async def _apply(service: CanvasService, program: str, **kwargs):
    return await service.apply(
        session_id="s1", workspace_id="ws-1", statements=parse_program(program), unit="line", base_rev=kwargs.pop("base_rev", None),
        source=kwargs.pop("source", "operator"), by=kwargs.pop("by", "alice"),
    )


async def _event(service: CanvasService, element: str, kind: str, **kwargs):
    return await service.record_event(session_id="s1", workspace_id="ws-1", element=element, kind=kind, by="alice", **kwargs)


@pytest_asyncio.fixture
async def service(sqlite_provider: SqliteStorageProvider) -> CanvasService:
    await sqlite_provider.get_event_store().ensure_schema()
    return CanvasService(sqlite_provider)


async def test_a_canvas_round_trips_through_a_file_database(service: CanvasService, sqlite_provider: SqliteStorageProvider):
    async with asyncio.timeout(_BOUND_S):
        await _apply(service, _PLAYLIST.read_text(encoding="utf-8"))
        await _event(service, "genre", "change", value="Lo-fi")
        await _event(service, "play", "click", client_event_id="c-1")
        await _event(service, "player", "play", position=42.5)
        await _event(service, "viz", "set_value", value={"count": 3, "tags": ["a", "b"]})
        await _event(service, "viz", "emit", name="go", data={"n": 1})
        await service.mark_seen("s1", 3)
        row = await sqlite_provider.get_storage(SessionCanvas).get("s1")
    assert (row.rev, row.event_seq, row.seen_event_seq) == (3, 5, 3)
    assert row.values == {"genre": "Lo-fi", "player": {"playing": True, "position": 42.5}, "viz": {"count": 3, "tags": ["a", "b"]}}
    assert row.value_set_by == {"genre": "user", "player": "user", "viz": "user"}
    assert [e.id for e in row.events] == ["e1", "e2", "e3", "e4", "e5"]
    assert [(e.kind, e.wake) for e in row.events] == [("change", False), ("click", True), ("play", False), ("set_value", False), ("emit", True)]
    assert row.events[1].client_event_id == "c-1" and row.events[4].data == {"n": 1} and row.events[2].position == 42.5
    assert len(row.wake_times) == 2 and all(t.tzinfo is not None for t in row.wake_times) and row.updated_at is not None
    assert row.elements["controls"] == {"type": "Row", "props": {}, "children": ["genre", "play"]}
    assert [e.id for e in await service.unseen_events("s1")] == ["e4", "e5"]


async def test_a_retried_click_and_a_stale_base_rev_read_the_stored_ring(service: CanvasService):
    await _apply(service, _PLAYLIST.read_text(encoding="utf-8"))
    first = await _event(service, "genre", "change", value="Lo-fi", client_event_id="c-1")
    assert await _event(service, "genre", "change", value="Lo-fi", client_event_id="c-1") == first
    with pytest.raises(CanvasRevConflict) as exc:
        await _apply(service, 'skip = Button("Skip")\ncontrols += [skip]', base_rev=1)
    assert exc.value.changes == ['e1 genre change value="Lo-fi" by="alice"']


async def test_a_refused_event_writes_nothing_to_the_file(service: CanvasService, sqlite_provider: SqliteStorageProvider):
    await _apply(service, _PLAYLIST.read_text(encoding="utf-8"))
    before = (await sqlite_provider.get_storage(SessionCanvas).get("s1")).model_dump()
    with pytest.raises(CanvasEventRefused) as exc:
        await _event(service, "genre", "change", value="Rock")
    assert exc.value.code == "invalid_value"
    assert (await sqlite_provider.get_storage(SessionCanvas).get("s1")).model_dump() == before


async def test_a_click_and_a_batch_that_meet_at_the_database_both_land(service: CanvasService, sqlite_provider: SqliteStorageProvider):
    """Both read the row before either writes (a barrier), so the database refuses one fenced patch; that writer reads again and lands."""
    await _apply(service, _PLAYLIST.read_text(encoding="utf-8"))
    store = sqlite_provider.get_storage(SessionCanvas)
    barrier, real, landed = asyncio.Barrier(2), store.patch_if, []
    turns = iter(range(2))

    async def patch_if(id, patch=None, *, where, set_paths=None, conn=None):  # noqa: A002
        mine = next(turns, None)                         # the first two calls are the two writers' first attempts
        if mine is not None:
            await barrier.wait()
        written = await real(id, patch, where=where, set_paths=set_paths, conn=conn)
        landed.append(written is not None)
        return written

    store.patch_if = patch_if
    async with asyncio.timeout(_BOUND_S):
        applied, clicked = await asyncio.gather(
            _apply(service, 'skip = Button("Skip")\ncontrols += [skip]', source="agent", by="agent"),
            _event(service, "genre", "change", value="Lo-fi"),
        )
    row = await store.get("s1")
    assert sorted(landed) == [False, True, True]
    assert "skip" in row.elements and row.values == {"genre": "Lo-fi"} and (row.rev, row.event_seq) == (3, 1)
    assert sorted([applied.rev, clicked.rev]) == [2, 3]

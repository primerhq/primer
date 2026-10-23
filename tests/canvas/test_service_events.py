"""Recording a user's interaction with the canvas: the checks, the fenced write, canvas.event and the tap frame (pivot P5, Task 7)."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from primer.canvas.decompile import decompile
from primer.canvas.lang import parse_program
from primer.canvas.service import CanvasEventRefused, CanvasRevConflict
from primer.canvas.settings import write_canvas_settings
from primer.model.canvas import CANVAS_SETTINGS_ID, CanvasSettings, SessionCanvas
from primer.model.except_ import ConflictError
from tests.canvas._fakes import StoreSpy, World, make_world, seed_session

GOLDEN = Path(__file__).parent / "golden"
NOW = datetime(2026, 10, 18, 12, 0, 0, tzinfo=UTC)


def G(name: str) -> str:
    return (GOLDEN / name).read_text(encoding="utf-8")


async def _apply(w: World, source: str, **kwargs: Any):
    return await w.service.apply(
        session_id="s1", workspace_id="ws-1", statements=parse_program(source), unit="line", base_rev=kwargs.pop("base_rev", None),
        source=kwargs.pop("src", "operator"), by=kwargs.pop("by", "alice"), **kwargs,
    )


async def _event(w: World, element: str, kind: str, **kwargs: Any):
    return await w.service.record_event(session_id="s1", workspace_id="ws-1", element=element, kind=kind, by=kwargs.pop("by", "alice"), **kwargs)


async def _world(**service_kwargs: Any) -> World:
    w = make_world(**service_kwargs)
    await seed_session(w)
    await _apply(w, G("playlist_create.cl"))
    return w


@pytest_asyncio.fixture
async def world_with_playlist() -> World:
    return await _world()


# ---- what an event does to the canvas ------------------------------------------------------------------------------------------------


async def test_a_select_change_records_the_value_and_bumps_rev(world_with_playlist):
    w = world_with_playlist
    ev = await w.service.record_event(session_id="s1", workspace_id="ws-1", element="genre", kind="change", value="Lo-fi", by="alice")
    row = await w.canvas("s1")
    assert (ev.id, ev.wake, ev.label, ev.rev) == ("e1", False, "Genre", 2)
    assert (row.rev, row.values, row.value_set_by) == (2, {"genre": "Lo-fi"}, {"genre": "user"})
    [logged] = await w.events("canvas.event")
    assert logged.payload["element"] == "genre" and logged.payload["value"] == "Lo-fi" and "client_event_id" not in logged.payload


async def test_the_event_is_what_the_server_made_of_it(world_with_playlist):
    w = world_with_playlist
    ev = await _event(w, "genre", "change", value="Lo-fi")
    assert (ev.seq, ev.type, ev.kind, ev.source, ev.by, ev.element) == (1, "Select", "change", "control", "alice", "genre")
    assert (ev.name, ev.data, ev.position, ev.client_event_id) == (None, None, None, None)
    assert ev.at.tzinfo is not None
    row = await w.canvas("s1")
    assert row.events == [ev] and row.event_seq == 1 and row.seen_event_seq == 0


async def test_the_platform_event_carries_the_event_and_names_who_did_it(world_with_playlist):
    w = world_with_playlist
    ev = await _event(w, "genre", "change", value="Lo-fi", by="bob")
    [logged] = await w.events("canvas.event")
    assert (logged.actor, logged.workspace_id, logged.session_id) == ("bob", "ws-1", "s1")
    assert logged.payload == ev.public() and logged.payload["id"] == "e1" and logged.payload["rev"] == 2


async def test_the_frame_after_an_event_carries_both_counters(world_with_playlist):
    w = world_with_playlist
    await _event(w, "genre", "change", value="Lo-fi")
    await _event(w, "play", "click")
    frames = [f for k, f in w.bus.published if k == "session:s1:delta"]
    assert frames[-2:] == [
        {"class": "canvas", "session_id": "s1", "rev": 2, "event_seq": 1},
        {"class": "canvas", "session_id": "s1", "rev": 2, "event_seq": 2},
    ]


async def test_a_button_click_wakes_and_keeps_rev(world_with_playlist):
    w = world_with_playlist
    ev = await _event(w, "play", "click")
    row = await w.canvas("s1")
    assert (ev.wake, ev.label, ev.rev, ev.value) == (True, "Play", 1, None)
    assert (row.rev, row.event_seq, row.values, len(row.wake_times)) == (1, 1, {}, 1)


async def test_wake_overrides_per_element():
    w = make_world()
    await seed_session(w)
    await _apply(w, 'root = Column([pick, quiet, loud])\npick = Select("Pick", ["a", "b"], wake=true)\n'
                    'quiet = Button("Quiet", wake=false)\nloud = Button("Loud")')
    assert (await _event(w, "pick", "change", value="b")).wake is True
    assert (await _event(w, "quiet", "click")).wake is False
    assert (await _event(w, "loud", "click")).wake is True
    assert len((await w.canvas("s1")).wake_times) == 2, "only the waking events are counted"


async def test_choosing_what_is_already_chosen_changes_nothing_but_the_event(world_with_playlist):
    w = world_with_playlist
    ev = await _event(w, "genre", "change", value="Jazz")                  # the Select's own value
    row = await w.canvas("s1")
    assert ev.rev == 1 and (row.rev, row.values, row.value_set_by, row.event_seq) == (1, {}, {}, 1)
    await _event(w, "genre", "change", value="Lo-fi")
    again = await _event(w, "genre", "change", value="Lo-fi")
    row = await w.canvas("s1")
    assert (again.rev, row.rev, row.event_seq, row.value_set_by) == (2, 2, 3, {"genre": "user"})


async def test_a_multiple_select_keeps_its_choices_in_the_order_of_its_options():
    w = make_world()
    await seed_session(w)
    await _apply(w, 'root = Column([tags])\ntags = Select("Tags", ["a", "b", "c"], multiple=true)')
    ev = await _event(w, "tags", "change", value=["c", "a"])
    assert ev.value == ["a", "c"] and (await w.canvas("s1")).values == {"tags": ["a", "c"]}
    same = await _event(w, "tags", "change", value=["a", "c"])
    assert same.rev == ev.rev, "the same choices in another order are the same value"


async def test_link_and_audio_events_are_silent_and_take_their_label_from_the_element(world_with_playlist):
    w = world_with_playlist
    opened = await _event(w, "more", "open")
    played = await _event(w, "player", "play", position=3)
    assert (opened.wake, opened.label, opened.type) == (False, "Open on Bandcamp", "Link")
    assert (played.wake, played.label, played.type) == (False, "So What - Miles Davis", "Audio")
    assert (await w.canvas("s1")).wake_times == []


# ---- audio: the state contract of the decompiler ------------------------------------------------------------------------------------


async def test_audio_events_store_the_players_state_without_moving_rev(world_with_playlist):
    w = world_with_playlist
    await _event(w, "player", "play", position=42.5)
    row = await w.canvas("s1")
    assert (row.rev, row.values["player"], row.value_set_by["player"]) == (1, {"playing": True, "position": 42.5}, "user")
    await _event(w, "player", "pause", position=50)
    assert (await w.canvas("s1")).values["player"] == {"playing": False, "position": 50.0}
    ended = await _event(w, "player", "ended", position=180)
    row = await w.canvas("s1")
    assert row.values["player"] == {"playing": False, "position": 180.0, "ended": True} and ended.position == 180.0
    assert (row.rev, row.event_seq) == (1, 3)


async def test_an_audio_event_without_a_position_starts_at_zero(world_with_playlist):
    w = world_with_playlist
    ev = await _event(w, "player", "play")
    assert ev.position is None and (await w.canvas("s1")).values["player"] == {"playing": True, "position": 0.0}


async def test_the_decompiler_reads_the_audio_state_the_service_stores(world_with_playlist):
    w = world_with_playlist
    await _event(w, "player", "play", position=42)
    assert "# user: playing at 0:42" in decompile(w.service.state_of(await w.canvas("s1")))
    await _event(w, "player", "pause", position=65)
    assert "# user: paused at 1:05" in decompile(w.service.state_of(await w.canvas("s1")))
    await _event(w, "player", "ended", position=90)
    assert "# user: ended" in decompile(w.service.state_of(await w.canvas("s1")))


# ---- an Html element's events --------------------------------------------------------------------------------------------------------


async def test_an_html_emit_wakes_and_marks_its_source(world_with_playlist):
    w = world_with_playlist
    ev = await _event(w, "viz", "emit", name="go", data={"n": 1, "tags": ["a"]})
    assert (ev.source, ev.wake, ev.type, ev.label, ev.name, ev.data, ev.rev) == ("html:viz", True, "Html", None, "go", {"n": 1, "tags": ["a"]}, 1)
    [logged] = await w.events("canvas.event")
    assert logged.payload["name"] == "go" and logged.payload["data"] == {"n": 1, "tags": ["a"]}


async def test_an_html_set_value_is_silent_and_moves_rev_only_when_it_changes_the_value(world_with_playlist):
    w = world_with_playlist
    first = await _event(w, "viz", "set_value", value={"count": 3})
    assert (first.source, first.wake, first.rev) == ("html:viz", False, 2)
    row = await w.canvas("s1")
    assert (row.values["viz"], row.value_set_by["viz"]) == ({"count": 3}, "user")
    same = await _event(w, "viz", "set_value", value={"count": 3})
    assert same.rev == 2 and (await w.canvas("s1")).rev == 2
    cleared = await _event(w, "viz", "set_value", value=None)
    row = await w.canvas("s1")
    assert (cleared.rev, row.rev, "viz" in row.values, "viz" in row.value_set_by) == (3, 3, False, False)
    nothing = await _event(w, "viz", "set_value", value=None)
    assert nothing.rev == 3, "clearing what is not set changes nothing"


async def test_fields_a_kind_does_not_use_are_not_stored(world_with_playlist):
    w = world_with_playlist
    ev = await _event(w, "play", "click", value="junk", name="junk", data={"x": 1}, position=5)
    assert (ev.value, ev.name, ev.data, ev.position) == (None, None, None, None)


# ---- refusals ------------------------------------------------------------------------------------------------------------------------

_NAME_RULE = "an emit name is 1 to 64 letters, digits, _ . : or -, and does not start with /"


@pytest.mark.parametrize("element, kind, kwargs, code, message", [
    ("nope", "click", {}, "unknown_element", "'nope' is not on the canvas any more"),
    ("title", "click", {}, "kind_not_allowed", "Text elements do not send click events"),
    ("play", "change", {"value": "x"}, "kind_not_allowed", "Button elements do not send change events"),
    ("genre", "hover", {}, "kind_not_allowed", "Select elements do not send hover events"),
    ("genre", "change", {"value": "Rock"}, "invalid_value", "'Rock' is not one of the options of 'genre'"),
    ("genre", "change", {"value": ["Jazz"]}, "invalid_value", "'genre' takes a single option, not a list"),
    ("genre", "change", {}, "invalid_value", "'None' is not one of the options of 'genre'"),
    ("viz", "emit", {"name": "/cmd"}, "invalid_value", _NAME_RULE),
    ("viz", "emit", {"name": ""}, "invalid_value", _NAME_RULE),
    ("viz", "emit", {"name": "x" * 65}, "invalid_value", _NAME_RULE),
    ("viz", "emit", {"name": "has space"}, "invalid_value", _NAME_RULE),
    ("viz", "emit", {"name": None}, "invalid_value", _NAME_RULE),
    ("viz", "emit", {"name": "go", "data": "x" * 5000}, "invalid_value", "emit data is at most 4096 bytes of JSON"),
    ("viz", "emit", {"name": "go", "data": float("nan")}, "invalid_value", "value must be JSON: no NaN or infinity"),
    ("viz", "emit", {"name": "go", "data": "bad \x00 nul"}, "invalid_value", "value contains a character that cannot be stored (a NUL or a lone surrogate)"),
    ("viz", "set_value", {"value": "x" * 1001}, "invalid_value", "a value is at most 1000 characters"),
    ("viz", "set_value", {"value": float("inf")}, "invalid_value", "value must be JSON: no NaN or infinity"),
    ("viz", "set_value", {"value": 10**30}, "invalid_value", "number is out of range"),
    # a float of 1e15 or more: Postgres JSONB reads 1e16 and up back as an int, so it is refused like an int of 16 digits (#790 review)
    ("viz", "set_value", {"value": 1e16}, "invalid_value", "number is out of range"),
    ("viz", "emit", {"name": "go", "data": {"n": [1e15]}}, "invalid_value", "number is out of range"),
    ("player", "play", {"position": -1}, "invalid_value", "a position is 0 to 86400 seconds"),
    ("player", "pause", {"position": 86401}, "invalid_value", "a position is 0 to 86400 seconds"),
    ("player", "ended", {"position": float("nan")}, "invalid_value", "a position is 0 to 86400 seconds"),
    ("player", "play", {"position": True}, "invalid_value", "a position is 0 to 86400 seconds"),
    ("player", "play", {"position": "5"}, "invalid_value", "a position is 0 to 86400 seconds"),
    ("player", "play", {"position": 10**400}, "invalid_value", "a position is 0 to 86400 seconds"),
])
async def test_refusals(world_with_playlist, element, kind, kwargs, code, message):
    w = world_with_playlist
    before = (await w.canvas("s1")).model_dump()
    published, logged = len(w.bus.published), len(await w.events("canvas.event"))
    spy = StoreSpy(w)
    with pytest.raises(CanvasEventRefused) as exc:
        await _event(w, element, kind, **kwargs)
    assert (exc.value.code, exc.value.message) == (code, message) and str(exc.value) == message
    assert spy.patches == [] and (await w.canvas("s1")).model_dump() == before, "a refused event writes nothing"
    assert len(w.bus.published) == published and len(await w.events("canvas.event")) == logged


def _options_of(lengths: list[int]) -> list[str]:
    """Distinct options of exactly these lengths (each starts with its index)."""
    return [str(i) + "x" * (n - 1) for i, n in enumerate(lengths)]


async def test_a_select_choice_is_capped_at_a_thousand_characters_of_json():
    """A choice goes into the ring and out in the platform event, so its JSON is held to the limit of every event value (#790 review:
    the cap on a Select ``change`` was not pinned). A multiple Select of 200-character options can reach it."""
    fits, over = _options_of([197, 197, 197, 197, 196]), _options_of([197, 197, 197, 197, 197])
    compact = {"separators": (",", ":"), "ensure_ascii": False}
    assert len(json.dumps(fits, **compact)) == 1000 and len(json.dumps(over, **compact)) == 1001
    w = make_world()
    await seed_session(w)
    await _apply(w, f'root = Column([a, b])\na = Select("A", {json.dumps(fits)}, multiple=true)\nb = Select("B", {json.dumps(over)}, multiple=true)')
    spy = StoreSpy(w)
    with pytest.raises(CanvasEventRefused) as exc:
        await _event(w, "b", "change", value=over)
    assert (exc.value.code, exc.value.message) == ("invalid_value", "a value is at most 1000 characters")
    assert spy.patches == [] and (await w.canvas("s1")).event_seq == 0, "the refused choice wrote nothing"
    ev = await _event(w, "a", "change", value=fits)
    assert ev.value == fits and (await w.canvas("s1")).values == {"a": fits}, "exactly 1000 characters is the last that fits"


async def test_a_list_longer_than_the_options_is_refused_without_being_read():
    """A multiple Select's value is checked against every option, so a body full of choices must not be walked at all."""
    class Walked(list):
        def __iter__(self):
            raise AssertionError("the list was read")

    w = make_world()
    await seed_session(w)
    await _apply(w, 'root = Column([tags])\ntags = Select("Tags", ["a", "b", "c", "d"], multiple=true)')
    with pytest.raises(CanvasEventRefused) as exc:
        await _event(w, "tags", "change", value=Walked(["a"] * 5))
    assert (exc.value.code, exc.value.message) == ("invalid_value", "'tags' has 4 options; the value lists 5")
    with pytest.raises(CanvasEventRefused) as exc:
        await _event(w, "tags", "change", value=["a", "a"])
    assert exc.value.message == "'tags' lists an option twice", "a list that is not too long is still judged by the options"
    assert (await w.canvas("s1")).events == []


async def test_an_event_on_a_session_without_a_canvas_is_unknown():
    w = make_world()
    await seed_session(w)
    with pytest.raises(CanvasEventRefused) as exc:
        await _event(w, "play", "click")
    assert exc.value.code == "unknown_element"


async def test_a_disabled_or_hidden_element_does_not_take_events():
    w = make_world()
    await seed_session(w)
    await _apply(w, 'root = Column([off, gone, card, ok])\noff = Button("Off", disabled=true)\ngone = Button("Gone")\ngone.hidden = true\n'
                    'card = Card([inner])\ncard.hidden = true\ninner = Button("Inner")\nok = Button("Fine")')
    for element in ("off", "gone", "inner"):
        with pytest.raises(CanvasEventRefused) as exc:
            await _event(w, element, "click")
        assert (exc.value.code, exc.value.message) == ("element_disabled", f"'{element}' is disabled or hidden")
    assert (await _event(w, "ok", "click")).label == "Fine"
    assert (await w.canvas("s1")).event_seq == 1


async def test_a_disabled_container_does_not_block_its_descendants():
    """Only a HIDDEN container takes what is in it out of the user's reach. ``disabled`` is a flag on the element it is set on: the
    controls inside a disabled Card or Row keep their own state (#790 review: a change that read ``disabled`` up the tree survived)."""
    w = make_world()
    await seed_session(w)
    await _apply(w, 'root = Column([card, outer])\ncard = Card([inner])\ninner = Button("Inner")\ncard.disabled = true\n'
                    'outer = Column([mid])\nmid = Row([deep])\ndeep = Button("Deep")\nouter.disabled = true\nmid.disabled = true')
    assert (await _event(w, "inner", "click")).label == "Inner"
    assert (await _event(w, "deep", "click")).label == "Deep"
    assert (await w.canvas("s1")).event_seq == 2


async def test_a_hidden_container_further_up_blocks_its_descendants():
    w = make_world()
    await seed_session(w)
    await _apply(w, 'root = Column([outer])\nouter = Column([mid])\nmid = Row([deep])\ndeep = Button("Deep")\nouter.hidden = true')
    with pytest.raises(CanvasEventRefused) as exc:
        await _event(w, "deep", "click")
    assert exc.value.code == "element_disabled"


async def test_the_eleventh_waking_event_in_a_minute_is_refused(world_with_playlist):
    w = world_with_playlist
    for _ in range(10):
        await _event(w, "play", "click")
    before = (await w.canvas("s1")).model_dump()
    with pytest.raises(CanvasEventRefused) as exc:
        await _event(w, "play", "click")
    assert (exc.value.code, exc.value.message) == (
        "rate_limited", "The agent takes at most 10 canvas wakes a minute. Wait a moment, then try again.",
    )
    assert (await w.canvas("s1")).model_dump() == before, "a refused event is not counted against the budget"
    assert (await _event(w, "genre", "change", value="Lo-fi")).wake is False, "a silent event still passes"


async def test_the_limit_comes_from_the_settings(world_with_playlist):
    w = world_with_playlist
    await write_canvas_settings(w.sp, CanvasSettings(id=CANVAS_SETTINGS_ID, max_waking_events_per_minute=2))
    await _event(w, "play", "click")
    await _event(w, "play", "click")
    with pytest.raises(CanvasEventRefused) as exc:
        await _event(w, "play", "click")
    assert exc.value.message == "The agent takes at most 2 canvas wakes a minute. Wait a moment, then try again."


async def test_the_budget_comes_back_after_a_minute():
    now = [NOW]
    w = await _world(clock=lambda: now[0])
    for _ in range(10):
        await _event(w, "play", "click")
    with pytest.raises(CanvasEventRefused):
        await _event(w, "play", "click")
    now[0] = NOW + timedelta(seconds=59)
    with pytest.raises(CanvasEventRefused):
        await _event(w, "play", "click")
    now[0] = NOW + timedelta(seconds=61)
    await _event(w, "play", "click")
    row = await w.canvas("s1")
    assert len(row.wake_times) == 10 and row.wake_times[-1] == NOW + timedelta(seconds=61), "the ring keeps the last ten"


# ---- retries and the ring ------------------------------------------------------------------------------------------------------------


async def test_a_retried_post_returns_the_same_event(world_with_playlist):
    w = world_with_playlist
    first = await _event(w, "genre", "change", value="Lo-fi", client_event_id="c-1")
    frames, logged = len(w.bus.published), len(await w.events("canvas.event"))
    again = await _event(w, "genre", "change", value="Lo-fi", client_event_id="c-1")
    assert again == first and again.id == "e1"
    row = await w.canvas("s1")
    assert (row.event_seq, len(row.events), row.rev) == (1, 1, 2)
    assert len(w.bus.published) == frames and len(await w.events("canvas.event")) == logged, "no write, no emit, no frame"


async def test_a_retry_is_answered_even_when_the_element_is_gone_by_then(world_with_playlist):
    w = world_with_playlist
    first = await _event(w, "play", "click", client_event_id="c-9")
    await _apply(w, "del play")
    assert await _event(w, "play", "click", client_event_id="c-9") == first
    with pytest.raises(CanvasEventRefused):
        await _event(w, "play", "click", client_event_id="c-10")


async def test_the_client_event_id_stays_out_of_what_others_see(world_with_playlist):
    w = world_with_playlist
    await _event(w, "play", "click", client_event_id="c-2")
    [logged] = await w.events("canvas.event")
    assert "client_event_id" not in logged.payload
    assert (await w.canvas("s1")).events[0].client_event_id == "c-2"


async def test_the_ring_keeps_the_newest(world_with_playlist):
    w = world_with_playlist
    await write_canvas_settings(w.sp, CanvasSettings(id=CANVAS_SETTINGS_ID, max_events=10))
    for n in range(12):
        await _event(w, "genre", "change", value="Lo-fi" if n % 2 == 0 else "Jazz")
    row = await w.canvas("s1")
    assert [e.id for e in row.events] == [f"e{n}" for n in range(3, 13)] and row.event_seq == 12


async def test_the_wake_ring_keeps_the_last_ones(world_with_playlist):
    w = world_with_playlist
    await write_canvas_settings(w.sp, CanvasSettings(id=CANVAS_SETTINGS_ID, max_waking_events_per_minute=3))
    for _ in range(3):
        await _event(w, "play", "click")
    assert len((await w.canvas("s1")).wake_times) == 3


# ---- the writes ----------------------------------------------------------------------------------------------------------------------


async def test_an_event_is_one_fenced_patch_of_only_what_it_changed(world_with_playlist):
    w = world_with_playlist
    spy = StoreSpy(w)
    await _event(w, "genre", "change", value="Lo-fi")
    await _event(w, "play", "click")
    await _event(w, "player", "play", position=1)
    change, click, audio = spy.patches
    assert set(change["patch"]) == {"events", "event_seq", "updated_at", "values", "value_set_by", "rev"}
    assert set(click["patch"]) == {"events", "event_seq", "updated_at", "wake_times"}
    assert set(audio["patch"]) == {"events", "event_seq", "updated_at", "values", "value_set_by"}
    assert change["where"] == {"rev": [1], "event_seq": [0]} and click["where"] == {"rev": [2], "event_seq": [1]}
    assert audio["where"] == {"rev": [2], "event_seq": [2]}


async def test_a_click_racing_an_apply_loses_neither(world_with_playlist):          # Review Focus 5
    w = world_with_playlist
    spy = StoreSpy(w)
    state = {"injected": False}

    async def racing(id, patch=None, *, where, set_paths=None, conn=None):  # noqa: A002
        if not state["injected"]:
            state["injected"] = True
            await w.service.record_event(session_id="s1", workspace_id="ws-1", element="genre", kind="change", value="Lo-fi", by="alice")
        return await spy.real_patch_if(id, patch, where=where, set_paths=set_paths, conn=conn)

    spy.store.patch_if = racing
    out = await _apply(w, 'skip = Button("Skip")\ncontrols += [skip]')
    row = await w.canvas("s1")
    assert (row.rev, row.values["genre"], "skip" in row.elements) == (3, "Lo-fi", True)
    assert out.rev == 3 and (row.event_seq, row.value_set_by) == (1, {"genre": "user"})


async def test_an_apply_racing_a_click_loses_neither(world_with_playlist):
    w = world_with_playlist
    spy = StoreSpy(w)
    state = {"injected": False}

    async def racing(id, patch=None, *, where, set_paths=None, conn=None):  # noqa: A002
        if not state["injected"]:
            state["injected"] = True
            await _apply(w, 'skip = Button("Skip")\ncontrols += [skip]')
        return await spy.real_patch_if(id, patch, where=where, set_paths=set_paths, conn=conn)

    spy.store.patch_if = racing
    ev = await _event(w, "play", "click")
    row = await w.canvas("s1")
    assert (ev.id, ev.rev, row.rev, row.event_seq, "skip" in row.elements) == ("e1", 2, 2, 1, True)


async def test_an_event_is_judged_again_on_the_row_that_beat_it(world_with_playlist):
    """The batch that landed first removed the element: the retried event must be refused, not recorded against a ghost."""
    w = world_with_playlist
    spy = StoreSpy(w)
    state = {"injected": False}

    async def racing(id, patch=None, *, where, set_paths=None, conn=None):  # noqa: A002
        if not state["injected"]:
            state["injected"] = True
            await _apply(w, "del play\ncontrols = Row([genre])")
        return await spy.real_patch_if(id, patch, where=where, set_paths=set_paths, conn=conn)

    spy.store.patch_if = racing
    with pytest.raises(CanvasEventRefused) as exc:
        await _event(w, "play", "click")
    assert exc.value.code == "unknown_element"
    row = await w.canvas("s1")
    assert (row.event_seq, row.events, row.wake_times) == (0, [], [])


async def test_two_clicks_at_once_both_get_their_own_seq(world_with_playlist):
    w = world_with_playlist
    store = w.sp.get_storage(SessionCanvas)
    real_get = store.get

    async def slow_get(id, *, conn=None):  # noqa: A002
        row = await real_get(id)
        await asyncio.sleep(0)               # both writers read the row before either writes it
        return row

    store.get = slow_get
    first, second = await asyncio.gather(_event(w, "play", "click", by="alice"), _event(w, "play", "click", by="bob"))
    row = await w.canvas("s1")
    assert sorted([first.seq, second.seq]) == [1, 2] and row.event_seq == 2
    assert sorted(e.by for e in row.events) == ["alice", "bob"] and len(row.wake_times) == 2
    assert len(await w.events("canvas.event")) == 2


async def test_events_that_keep_losing_give_up_with_a_conflict(world_with_playlist):
    w = world_with_playlist
    store = w.sp.get_storage(SessionCanvas)
    real, calls = store.patch_if, {"n": 0}

    async def contended(id, patch=None, *, where, set_paths=None, conn=None):  # noqa: A002
        calls["n"] += 1
        fresh = await store.get(id)
        await real(id, {"rev": fresh.rev + 1}, where={"rev": [fresh.rev]})          # somebody else always wins first
        return await real(id, patch, where=where, set_paths=set_paths, conn=conn)

    store.patch_if = contended
    with pytest.raises(ConflictError, match="the canvas changed too often; try again"):
        await _event(w, "play", "click")
    assert calls["n"] == 5
    assert (await w.canvas("s1")).events == [] and await w.events("canvas.event") == []


async def test_the_announcements_are_best_effort(world_with_playlist):
    w = world_with_playlist
    w.bus.fail = True

    async def down(**kwargs):
        raise RuntimeError("event log down")

    w.sp.get_event_store().append = down
    ev = await _event(w, "genre", "change", value="Lo-fi")
    assert ev.id == "e1" and (await w.canvas("s1")).values == {"genre": "Lo-fi"}


# ---- what the agent reads ------------------------------------------------------------------------------------------------------------


async def test_a_user_change_is_listed_when_the_agent_edits_from_an_old_read(world_with_playlist):   # Review Focus 2
    w = world_with_playlist
    await _event(w, "genre", "change", value="Lo-fi")
    with pytest.raises(CanvasRevConflict) as exc:
        await _apply(w, 'skip = Button("Skip")\ncontrols += [skip]', base_rev=1)
    assert (exc.value.rev, exc.value.base_rev, exc.value.changes) == (2, 1, ['e1 genre change value="Lo-fi" by="alice"'])


async def test_a_user_value_survives_the_agents_next_batch(world_with_playlist):                     # Review Focus 2
    w = world_with_playlist
    await _event(w, "genre", "change", value="Lo-fi")
    await _apply(w, 'skip = Button("Skip")\ncontrols += [skip]')
    row = await w.canvas("s1")
    assert (row.values, row.value_set_by) == ({"genre": "Lo-fi"}, {"genre": "user"})


async def test_unseen_and_mark_seen(world_with_playlist):
    w = world_with_playlist
    assert await w.service.unseen_events("s1") == []
    for value in ("Lo-fi", "Jazz", "Classical"):
        await _event(w, "genre", "change", value=value)
    assert [e.id for e in await w.service.unseen_events("s1")] == ["e1", "e2", "e3"]
    await w.service.mark_seen("s1", 2)
    assert [e.id for e in await w.service.unseen_events("s1")] == ["e3"] and (await w.canvas("s1")).seen_event_seq == 2
    await w.service.mark_seen("s1", 1)
    assert (await w.canvas("s1")).seen_event_seq == 2, "never lowered"
    await w.service.mark_seen("s1", 99)
    assert (await w.canvas("s1")).seen_event_seq == 3, "never past the last event"
    assert await w.service.unseen_events("s1") == []
    await _event(w, "play", "click")
    assert [e.id for e in await w.service.unseen_events("s1")] == ["e4"], "an event that comes later is still unseen"


async def test_mark_seen_is_one_fenced_patch_of_its_own_fields(world_with_playlist):
    """The read marks are two fields since G8-07 (the pointer and the events read ahead of it), so the patch writes both and moves the
    seen_version it is fenced on; nothing else on the row is written."""
    w = world_with_playlist
    await _event(w, "play", "click")
    spy = StoreSpy(w)
    await w.service.mark_seen("s1", 1)
    [one] = spy.patches
    assert one == {"patch": {"seen_event_seq": 1, "seen_ahead": [], "seen_version": 1}, "where": {"seen_version": [0]}}


async def test_mark_events_seen_marks_exactly_those_events(world_with_playlist):
    """G8-07: events read out of order wait in seen_ahead; the pointer moves over them once the events before them are read."""
    w = world_with_playlist
    for _ in range(4):
        await _event(w, "play", "click")
    await w.service.mark_events_seen("s1", [2, 4])
    row = await w.canvas("s1")
    assert [e.id for e in row.unseen_events()] == ["e1", "e3"] and (row.seen_event_seq, row.seen_ahead) == (0, [2, 4])
    await w.service.mark_events_seen("s1", [1])
    row = await w.canvas("s1")
    assert [e.id for e in row.unseen_events()] == ["e3"] and (row.seen_event_seq, row.seen_ahead) == (2, [4])
    await w.service.mark_seen("s1", 3)
    row = await w.canvas("s1")
    assert row.unseen_events() == [] and row.seen_event_seq == 4, "the high-water mark also moves over what was read ahead"
    await w.service.mark_events_seen("s1", [4, 99, 0])
    assert (await w.canvas("s1")).seen_ahead == [], "a seq already read, or that names no event, is not kept"


async def test_two_marks_from_stale_reads_both_land(world_with_playlist):
    """Two waits mark different events from the same read: the seen_version fence rejects the second, which reads again."""
    w = world_with_playlist
    for _ in range(4):
        await _event(w, "play", "click")
    store = w.sp.get_storage(SessionCanvas)
    real, state = store.patch_if, {"raced": False}

    async def racing(id, patch=None, *, where, set_paths=None, conn=None):  # noqa: A002
        if not state["raced"]:
            state["raced"] = True
            store.patch_if = real
            await w.service.mark_events_seen("s1", [3])
            store.patch_if = racing
        return await real(id, patch, where=where, set_paths=set_paths, conn=conn)

    store.patch_if = racing
    await w.service.mark_events_seen("s1", [2])
    row = await w.canvas("s1")
    assert [e.id for e in row.unseen_events()] == ["e1", "e4"] and row.seen_ahead == [2, 3]


async def test_a_mark_from_a_stale_read_keeps_what_another_wait_marked(world_with_playlist):
    """Both writers patch the read marks fenced on seen_version: the stale mark_seen is rejected, reads again and keeps the
    seen_ahead another wait wrote under it."""
    w = world_with_playlist
    for _ in range(3):
        await _event(w, "play", "click")
    store = w.sp.get_storage(SessionCanvas)
    real, state = store.patch_if, {"raced": False}

    async def racing(id, patch=None, *, where, set_paths=None, conn=None):  # noqa: A002
        if not state["raced"]:
            state["raced"] = True
            store.patch_if = real
            await w.service.mark_events_seen("s1", [3])
            store.patch_if = racing
        return await real(id, patch, where=where, set_paths=set_paths, conn=conn)

    store.patch_if = racing
    await w.service.mark_seen("s1", 1)
    row = await w.canvas("s1")
    assert [e.id for e in row.unseen_events()] == ["e2"] and (row.seen_event_seq, row.seen_ahead) == (1, [3])


async def test_an_event_read_ahead_that_leaves_the_ring_is_dropped_from_seen_ahead(world_with_playlist):
    w = world_with_playlist
    await write_canvas_settings(w.sp, CanvasSettings(id=CANVAS_SETTINGS_ID, max_events=10, max_waking_events_per_minute=120))
    for _ in range(3):
        await _event(w, "play", "click")
    await w.service.mark_events_seen("s1", [3])
    for _ in range(10):
        await _event(w, "play", "click")                              # e3 leaves the ring of 10
    await w.service.mark_events_seen("s1", [13])
    row = await w.canvas("s1")
    assert [e.seq for e in row.events][0] == 4 and row.seen_ahead == [13]
    assert [e.seq for e in row.unseen_events()] == list(range(4, 13))


async def test_marking_and_reading_a_session_without_a_canvas_is_harmless():
    w = make_world()
    assert await w.service.unseen_events("nobody") == []
    await w.service.mark_seen("nobody", 5)
    assert await w.canvas("nobody") is None


async def test_marking_while_a_click_lands_loses_neither(world_with_playlist):
    w = world_with_playlist
    await _event(w, "play", "click")
    store = w.sp.get_storage(SessionCanvas)
    real = store.patch_if
    state = {"injected": False}

    async def racing(id, patch=None, *, where, set_paths=None, conn=None):  # noqa: A002
        if not state["injected"]:
            state["injected"] = True
            await _event(w, "play", "click")
        return await real(id, patch, where=where, set_paths=set_paths, conn=conn)

    store.patch_if = racing
    await w.service.mark_seen("s1", 1)
    row = await w.canvas("s1")
    assert (row.seen_event_seq, row.event_seq) == (1, 2), "the click wrote other fields; the mark is not fenced on them"


async def test_a_canvas_deleted_under_an_event_refuses_it(world_with_playlist):
    """The row vanishes between the read and the patch (its session was deleted): the click has nothing to land on."""
    w = world_with_playlist
    spy = StoreSpy(w)

    async def vanishing(id, patch=None, *, where, set_paths=None, conn=None):  # noqa: A002
        await spy.store.delete(id)
        return await spy.real_patch_if(id, patch, where=where, set_paths=set_paths, conn=conn)

    spy.store.patch_if = vanishing
    with pytest.raises(CanvasEventRefused) as exc:
        await _event(w, "play", "click")
    assert exc.value.code == "unknown_element" and await w.events("canvas.event") == []


# ---- the canvas row names its workspace ----------------------------------------------------------------------------------------------


async def test_an_event_is_announced_in_the_workspace_the_canvas_names_never_the_callers(world_with_playlist):
    """The non-admin event feed is read per workspace, and ``canvas.event`` carries what a user typed: a caller's wrong workspace id
    must not put it on another workspace's feed (#790 review)."""
    w = world_with_playlist                                    # the canvas of s1 lives in ws-1
    ev = await w.service.record_event(session_id="s1", workspace_id="ws-2", element="genre", kind="change", value="Lo-fi", by="alice")
    [logged] = await w.events("canvas.event")
    assert (logged.workspace_id, logged.payload["id"]) == ("ws-1", ev.id)
    assert (await w.canvas("s1")).workspace_id == "ws-1"


# ---- a stored row from before the read marks had seen_ahead and seen_version (review N6 on #856) -------------------------------------


@pytest_asyncio.fixture
async def sqlite_sp(tmp_path):
    from primer.model.provider import SqliteConfig, StorageProviderConfig, StorageProviderType
    from primer.storage.factory import StorageProviderFactory

    provider = StorageProviderFactory.create(
        StorageProviderConfig(provider=StorageProviderType.SQLITE, config=SqliteConfig(path=tmp_path / "t.sqlite")),
    )
    await provider.initialize()
    yield provider
    await provider.aclose()


async def test_marks_on_a_real_backend_row_that_lacks_the_new_keys(sqlite_sp):
    """A canvas stored before G8-07 has no ``seen_ahead`` or ``seen_version`` key. The model reads their defaults, and the
    seen_version guard that names the default 0 must match the document that lacks the key, or no mark would ever land."""
    from primer.canvas.service import CanvasService
    from primer.model.canvas import CanvasEvent

    def click(seq: int) -> dict:
        return CanvasEvent(
            id=f"e{seq}", seq=seq, rev=1, element="play", type="Button", kind="click", source="control", by="alice", wake=True,
            at=datetime(2026, 10, 18, 12, 0, tzinfo=UTC), label="Play",
        ).model_dump(mode="json")

    doc = SessionCanvas(id="s1", workspace_id="ws-1", rev=1).model_dump(mode="json")
    doc.update(events=[click(1), click(2), click(3)], event_seq=3, seen_event_seq=0)
    del doc["seen_ahead"], doc["seen_version"]
    store = sqlite_sp.get_storage(SessionCanvas)
    await store._ensure_table()
    await sqlite_sp.connection.execute(f'INSERT INTO "{store._table}" (id, data) VALUES (?, ?)', ("s1", json.dumps(doc)))
    await sqlite_sp.connection.commit()
    old = await store.get("s1")
    assert (old.seen_ahead, old.seen_version) == ([], 0)

    service = CanvasService(sqlite_sp)
    await service.mark_events_seen("s1", [2])
    row = await store.get("s1")
    assert (row.seen_event_seq, row.seen_ahead, row.seen_version) == (0, [2], 1), "the first mark landed on the old document"
    assert [e.id for e in row.unseen_events()] == ["e1", "e3"]
    await service.mark_events_seen("s1", [1])
    await service.mark_seen("s1", 3)
    row = await store.get("s1")
    assert (row.seen_event_seq, row.seen_ahead, row.seen_version) == (3, [], 3) and row.unseen_events() == []


"""The ``canvas`` toolset: canvas_get and canvas_apply, and the registrations that make it a built-in (pivot P5, Tasks 9 and 10; design 8.3).

``canvas_wait`` has its own file (``test_canvas_wait.py``); the registrations are checked here for all three tools."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from pydantic import ValidationError

from primer.api.registries import ProviderRegistry
from primer.api.registries.provider_registry import RESERVED_TOOLSET_IDS
from primer.canvas.catalog import CATALOG, signature
from primer.canvas.lang import IDENT_RE
from primer.canvas.ops import MAX_OPS_CHARS, MAX_SOURCE_CHARS
from primer.canvas.settings import write_canvas_settings
from primer.model.canvas import CANVAS_SETTINGS_ID, CanvasSettings
from primer.model.except_ import ConflictError
from primer.model.yield_ import ToolContext
from primer.toolset.canvas import CANVAS_TOOLSET_ID, ID_PATTERN, CanvasToolsetProvider, build_canvas_toolset
from primer.toolset.tool_catalog import collect_tool_entries
from tests.api.conftest import app, client, fake_provider_registry  # noqa: F401  (fixtures)
from tests.canvas._fakes import World, make_world, seed_session
from tests.docs._live_toolsets import built_in_providers, built_in_tools

GOLDEN = Path(__file__).resolve().parents[1] / "canvas" / "golden"


def G(name: str) -> str:
    return (GOLDEN / name).read_text(encoding="utf-8")


@pytest_asyncio.fixture
async def world() -> World:
    w = make_world()
    await seed_session(w)
    return w


@pytest.fixture
def provider(world: World) -> CanvasToolsetProvider:
    return build_canvas_toolset(storage_provider=world.sp, event_bus=world.bus, workspace_registry=world.registry)


@pytest.fixture
def ctx() -> ToolContext:
    return ToolContext(tool_call_id="call-1", session_id="s1", workspace_id="ws-1")


async def call(provider: CanvasToolsetProvider, name: str, arguments: dict[str, Any], ctx: ToolContext | None) -> tuple[bool, Any]:
    result = await provider.call(tool_name=name, arguments=arguments, ctx=ctx)
    return result.is_error, json.loads(result.output)


async def _click(world: World, element: str = "play", kind: str = "click", **kwargs: Any):
    return await world.service.record_event(session_id="s1", workspace_id="ws-1", element=element, kind=kind, by="alice", **kwargs)


# ---- the round trip ------------------------------------------------------------------------------------------------------------------


async def test_apply_then_get_round_trips(provider, ctx):
    r = await provider.call(tool_name="canvas_apply", arguments={"source": G("playlist_create.cl")}, ctx=ctx)
    assert not r.is_error and json.loads(r.output)["rev"] == 1
    got = json.loads((await provider.call(tool_name="canvas_get", arguments={}, ctx=ctx)).output)
    assert got["rev"] == 1 and got["format"] == "lang"
    assert got["source"].startswith("# canvas rev 1;")
    assert 'genre = Select("Genre", ["Jazz", "Lo-fi", "Classical"], "Jazz")' in got["source"]


async def test_the_result_of_an_apply_says_what_changed(provider, ctx):
    is_error, body = await call(provider, "canvas_apply", {"source": G("playlist_create.cl")}, ctx)
    assert not is_error
    assert body["changed"] == {
        "added": ["title", "cover", "controls", "genre", "play", "player", "more", "viz"], "updated": ["root"], "removed": [],
    }
    assert body["warnings"] == [] and set(body) == {"rev", "changed", "warnings"}


async def test_a_warning_reaches_the_agent(provider, ctx, world):
    await call(provider, "canvas_apply", {"source": G("playlist_create.cl")}, ctx)
    await _click(world, "genre", "change", value="Lo-fi")
    is_error, body = await call(provider, "canvas_apply", {"source": 'genre = Select("Genre", ["Jazz", "Classical"])'}, ctx)
    assert not is_error and body["warnings"] == ["genre: the user's value was reset because the element changed"]
    assert body["changed"] == {"added": [], "updated": ["genre"], "removed": []}


async def test_an_apply_is_a_record_of_the_agent_in_the_session(provider, ctx, world):
    await call(provider, "canvas_apply", {"source": G("playlist_create.cl")}, ctx)
    [record] = world.records("s1", "canvas_op")
    assert (record["payload"]["source"], record["payload"]["by"], record["payload"]["call_id"]) == ("agent", "agent", "call-1")
    [event] = await world.events("canvas.updated")
    assert (event.workspace_id, event.session_id, event.payload["source"]) == ("ws-1", "s1", "agent")


async def test_apply_errors_name_the_line_and_signature(provider, ctx):          # Review Focus 3
    r = await provider.call(tool_name="canvas_apply", arguments={"source": 'a = Select("x", "y")'}, ctx=ctx)
    body = json.loads(r.output)
    assert r.is_error and body["type"] == "validation-error" and body["message"] == "1 problem(s); nothing was applied"
    assert body["errors"] == [{"line": 1, "message": "Select needs options as a list", "expected": "Select(label, options, value?, multiple?, wake?)"}]
    assert body["rev"] == 0


async def test_every_bad_line_is_reported_and_nothing_is_applied(provider, ctx, world):
    source = 'root = Column([a, b, c])\na = Text("ok")\nb = Buton("x")\nc = Select("Genre", "Jazz")'
    is_error, body = await call(provider, "canvas_apply", {"source": source}, ctx)
    assert is_error and body["message"] == "2 problem(s); nothing was applied"
    assert [(e["line"], e["message"].split(" (")[0]) for e in body["errors"]] == [
        (3, "unknown element type 'Buton'"), (4, "Select needs options as a list"),
    ]
    assert await world.canvas("s1") is None, "a refused batch creates no canvas"
    assert world.records("s1") == [] and await world.events("canvas.updated") == []


async def test_a_syntax_error_has_the_same_shape_and_no_signature(provider, ctx):
    is_error, body = await call(provider, "canvas_apply", {"source": 'a = Text("x)\nb = Text("fine")'}, ctx)
    assert is_error and body["type"] == "validation-error"
    assert body["errors"] == [{"line": 1, "message": "unterminated string"}]
    assert body["rev"] == 0


async def test_a_refusal_reports_the_rev_that_is_current(provider, ctx):
    await call(provider, "canvas_apply", {"source": G("playlist_create.cl")}, ctx)
    is_error, body = await call(provider, "canvas_apply", {"source": "nope = Buton()"}, ctx)
    assert is_error and body["rev"] == 1
    is_error, body = await call(provider, "canvas_apply", {"source": 'a = Text("x'}, ctx)
    assert is_error and body["rev"] == 1


async def test_text_in_any_language_is_not_escaped(provider, ctx):
    await call(provider, "canvas_apply", {"source": 'root = Column([t])\nt = Text("café 日本語")'}, ctx)
    raw = (await provider.call(tool_name="canvas_get", arguments={}, ctx=ctx)).output
    assert "café 日本語" in raw and "\\u00e9" not in raw


# ---- JSON ops ------------------------------------------------------------------------------------------------------------------------


OPS = [
    {"op": "set", "id": "t", "type": "Text", "props": {"text": "hi", "style": "h2"}},
    {"op": "set", "id": "go", "type": "Button", "props": {"label": "Go"}},
    {"op": "append", "id": "root", "children": ["t", "go"]},
]


async def test_json_ops_are_accepted(provider, ctx, world):
    is_error, body = await call(provider, "canvas_apply", {"ops": OPS}, ctx)
    assert not is_error and body["rev"] == 1 and body["changed"]["added"] == ["t", "go"]
    row = await world.canvas("s1")
    assert row.elements["t"] == {"type": "Text", "props": {"text": "hi", "style": "h2"}}
    assert row.elements["root"]["children"] == ["t", "go"]
    [record] = world.records("s1", "canvas_op")
    assert [op["op"] for op in record["payload"]["ops"]] == ["set", "set", "append"]


async def test_json_ops_give_the_same_canvas_as_the_program(provider, ctx, world):
    ops_world = make_world()
    await seed_session(ops_world)
    ops_provider = build_canvas_toolset(storage_provider=ops_world.sp)
    await call(provider, "canvas_apply", {"source": 'root = Column([t, go])\nt = Text("hi", "h2")\ngo = Button("Go")'}, ctx)
    await call(ops_provider, "canvas_apply", {"ops": [*OPS[:2], {"op": "set", "id": "root", "type": "Column", "children": ["t", "go"]}]}, ctx)
    assert (await world.canvas("s1")).elements == (await ops_world.canvas("s1")).elements


async def test_op_errors_say_op_n_with_the_signature(provider, ctx):
    ops = [{"op": "set", "id": "t", "type": "Text", "props": {"text": "ok"}}, {"op": "set", "id": "pick", "type": "Select", "props": {"label": "x"}}]
    is_error, body = await call(provider, "canvas_apply", {"ops": ops}, ctx)
    assert is_error and body["type"] == "validation-error" and body["message"] == "1 problem(s); nothing was applied"
    assert body["errors"] == [{"op": 2, "message": "Select needs options as a list", "expected": "Select(label, options, value?, multiple?, wake?)"}]


@pytest.mark.parametrize("bad, message", [
    ({"op": "set", "id": "t"}, "type: Field required"),
    ({"op": "sett", "id": "t"}, "op must be one of set, prop, append, delete or clear"),
    ({"id": "t"}, "op must be one of set, prop, append, delete or clear"),
    ("clear", "Input should be a valid dictionary or object to extract fields from"),
    ({"op": "delete", "ids": "a"}, "ids: Input should be a valid list"),
    ({"op": "set", "id": "t", "type": "Text", "colour": 1}, "colour: Extra inputs are not permitted"),
])
async def test_a_malformed_op_is_reported_by_number_with_the_shapes(provider, ctx, world, bad, message):
    is_error, body = await call(provider, "canvas_apply", {"ops": [{"op": "clear"}, bad]}, ctx)
    assert is_error and body["type"] == "validation-error" and body["message"] == "1 problem(s); nothing was applied"
    [error] = body["errors"]
    assert error["op"] == 2 and error["message"] == message
    assert error["expected"].startswith('{"op":"set","id","type"') and '{"op":"clear"}' in error["expected"]
    assert await world.canvas("s1") is None


async def test_a_malformed_op_does_not_echo_what_was_sent(provider, ctx):
    long_name = "x" * 5000
    is_error, body = await call(provider, "canvas_apply", {"ops": [{"op": long_name}, {"op": "set", "id": "t", "type": "Text", long_name: 1}]}, ctx)
    assert is_error and len(json.dumps(body)) < 2000
    assert long_name not in json.dumps(body)


async def test_only_fifty_malformed_ops_are_listed(provider, ctx):
    is_error, body = await call(provider, "canvas_apply", {"ops": [{"op": "nope"}] * 60}, ctx)
    assert is_error and len(body["errors"]) == 51
    assert body["errors"][-1] == {"op": 51, "message": "10 more malformed ops are not shown"}


async def test_source_and_ops_are_exclusive(provider, ctx):
    for arguments in ({}, {"source": "clear", "ops": [{"op": "clear"}]}):
        is_error, body = await call(provider, "canvas_apply", arguments, ctx)
        assert is_error and body == {"type": "validation-error", "message": "give source or ops, not both and not neither"}


async def test_an_empty_batch_is_a_batch(provider, ctx):
    is_error, body = await call(provider, "canvas_apply", {"source": ""}, ctx)
    assert not is_error and body["rev"] == 1 and body["changed"] == {"added": [], "updated": [], "removed": []}
    is_error, body = await call(provider, "canvas_apply", {"ops": []}, ctx)
    assert not is_error and body["rev"] == 2


# ---- the size of a batch -------------------------------------------------------------------------------------------------------------


async def test_a_source_past_the_cap_is_refused_without_echoing_it(provider, ctx, world):
    huge = "# " + "x" * MAX_SOURCE_CHARS
    r = await provider.call(tool_name="canvas_apply", arguments={"source": huge}, ctx=ctx)
    body = json.loads(r.output)
    assert r.is_error and body["type"] == "validation-error" and "at most 600000 characters" in body["message"]
    assert len(r.output) < 2000, "the argument is not echoed back"
    assert await world.canvas("s1") is None


async def test_the_source_cap_is_inclusive(provider, ctx):
    program = "# " + "x" * (MAX_SOURCE_CHARS - 2)
    assert len(program) == MAX_SOURCE_CHARS
    is_error, body = await call(provider, "canvas_apply", {"source": program}, ctx)
    assert not is_error and body["rev"] == 1


async def test_ops_past_the_cap_are_refused_before_they_are_read(provider, ctx, world):
    """``source`` is capped at 600000 characters; the JSON of ``ops`` is capped at the same (#790 review)."""
    ops = [{"op": "set", "id": "t", "type": "Text", "props": {"text": "x" * 10000}}] * 70
    assert len(json.dumps(ops, separators=(",", ":"))) > MAX_OPS_CHARS
    r = await provider.call(tool_name="canvas_apply", arguments={"ops": ops}, ctx=ctx)
    body = json.loads(r.output)
    assert r.is_error and body["type"] == "validation-error"
    assert "ops hold" in body["message"] and "the limit is 600000" in body["message"], body
    assert len(r.output) < 2000
    assert await world.canvas("s1") is None


def _compact_size(ops: list[dict]) -> int:
    return len(json.dumps(ops, separators=(",", ":"), ensure_ascii=False))


def _applying_ops_of_size(n: int) -> list[dict]:
    """A batch that APPLIES and is EXACTLY ``n`` characters of compact JSON: a Text is set, then the canvas is cleared many times.

    Each ``{"op":"clear"}`` adds 15 characters (14 and a comma) and the Text's ``text`` takes the remainder, so every ``n`` from 57 up is
    reachable. The Text is cleared away again by the batch itself, so the canvas it ends with is a valid one at any size (a padded
    string alone could not be: a Text holds 10000 characters at most and a canvas 512 KiB).
    """
    head = {"op": "set", "id": "t", "type": "Text", "props": {"text": ""}}
    clears, pad = divmod(n - _compact_size([head]), 15)
    ops = [{**head, "props": {"text": "x" * pad}}, *[{"op": "clear"}] * clears]
    assert _compact_size(ops) == n
    return ops


async def test_ops_of_exactly_the_cap_are_applied_and_one_character_more_is_refused(provider, ctx, world):
    """600000 characters of compact JSON is the most ``ops`` may hold: that batch is read and applied; 600001 is refused before it is read."""
    is_error, body = await call(provider, "canvas_apply", {"ops": _applying_ops_of_size(MAX_OPS_CHARS)}, ctx)
    assert not is_error and body["rev"] == 1
    is_error, body = await call(provider, "canvas_apply", {"ops": _applying_ops_of_size(MAX_OPS_CHARS + 1)}, ctx)
    assert is_error and body["type"] == "validation-error"
    assert "ops hold 600001 characters of JSON; the limit is 600000" in body["message"], body
    assert (await world.canvas("s1")).rev == 1, "the refused batch applied nothing"


# ---- base_rev ------------------------------------------------------------------------------------------------------------------------


async def test_a_stale_base_rev_is_a_conflict(provider, ctx, world):
    await call(provider, "canvas_apply", {"source": G("playlist_create.cl")}, ctx)
    await _click(world, "genre", "change", value="Lo-fi")                    # the user moves the canvas to rev 2
    is_error, body = await call(provider, "canvas_apply", {"source": 'x = Text("x")\nroot += [x]', "base_rev": 1}, ctx)
    assert is_error and body["type"] == "conflict"
    assert body["message"] == "the canvas is at rev 2, not 1; nothing was applied" and body["rev"] == 2
    assert [line.split(" by=")[0] for line in body["changes"]] == ['e1 genre change value="Lo-fi"']
    assert (await world.canvas("s1")).rev == 2, "nothing was applied"


async def test_the_current_base_rev_applies(provider, ctx):
    await call(provider, "canvas_apply", {"source": G("playlist_create.cl")}, ctx)
    is_error, body = await call(provider, "canvas_apply", {"source": 'x = Text("x")\nroot += [x]', "base_rev": 1}, ctx)
    assert not is_error and body["rev"] == 2


async def test_a_negative_base_rev_is_a_validation_error(provider, ctx):
    is_error, body = await call(provider, "canvas_apply", {"source": "clear", "base_rev": -1}, ctx)
    assert is_error and body["type"] == "validation-error" and "base_rev" in body["message"]


async def test_a_canvas_that_keeps_changing_is_a_conflict_error(provider, ctx, monkeypatch):
    async def always_busy(**kwargs):
        raise ConflictError("the canvas changed too often; try again")

    monkeypatch.setattr(provider.service, "apply", always_busy)
    is_error, body = await call(provider, "canvas_apply", {"source": "clear"}, ctx)
    assert is_error and body == {"type": "conflict", "message": "the canvas changed too often; try again"}


# ---- canvas_get ----------------------------------------------------------------------------------------------------------------------


async def test_get_of_a_session_without_a_canvas_is_an_empty_one_and_creates_none(provider, ctx, world):
    is_error, body = await call(provider, "canvas_get", {}, ctx)
    assert not is_error and body == {"rev": 0, "format": "lang", "source": "# canvas rev 0: empty\n"}
    assert await world.canvas("s1") is None, "reading a canvas never creates it"


async def test_get_reports_events_once(provider, ctx, world):
    await call(provider, "canvas_apply", {"source": G("playlist_create.cl")}, ctx)
    await _click(world, "play", "click")
    first = (await call(provider, "canvas_get", {}, ctx))[1]["source"]
    assert "# events since your last read (1):" in first and '#   e1 play click by="alice"' in first
    second = (await call(provider, "canvas_get", {}, ctx))[1]["source"]
    assert "events since your last read" not in second
    assert (await world.canvas("s1")).seen_event_seq == 1


async def test_get_shows_what_the_user_chose_marked_as_theirs(provider, ctx, world):
    await call(provider, "canvas_apply", {"source": G("playlist_create.cl")}, ctx)
    await _click(world, "genre", "change", value="Lo-fi")
    source = (await call(provider, "canvas_get", {}, ctx))[1]["source"]
    assert 'genre.value = "Lo-fi"  # user' in source and source.startswith("# canvas rev 2;")


async def test_get_hides_an_html_body_unless_asked(provider, ctx):
    await call(provider, "canvas_apply", {"source": G("playlist_create.cl")}, ctx)
    short = (await call(provider, "canvas_get", {}, ctx))[1]["source"]
    full = (await call(provider, "canvas_get", {"full": True}, ctx))[1]["source"]
    assert "viz = Html(..., 60)" in short and "canvas_get full=true shows the body" in short
    assert "<script>b.textContent='3 tracks queued'</script>" in full


async def test_get_of_some_ids_is_their_subtree(provider, ctx):
    await call(provider, "canvas_apply", {"source": G("playlist_create.cl")}, ctx)
    source = (await call(provider, "canvas_get", {"ids": ["controls"]}, ctx))[1]["source"]
    lines = source.splitlines()
    assert lines[1] == "# part of the canvas: controls"
    assert [line.split(" = ")[0] for line in lines[2:]] == ["controls", "genre", "play"]


async def test_get_of_an_unknown_id_is_refused(provider, ctx):
    await call(provider, "canvas_apply", {"source": G("playlist_create.cl")}, ctx)
    is_error, body = await call(provider, "canvas_get", {"ids": ["controls", "nope"]}, ctx)
    assert is_error and body == {"type": "validation-error", "message": "no element named 'nope'"}


@pytest.mark.parametrize("ids", [["Title"], ["a b"], ["x" * 33], [""], ["ok"] * 51, "controls"])
async def test_get_refuses_ids_that_cannot_be_element_ids(provider, ctx, ids):
    is_error, body = await call(provider, "canvas_get", {"ids": ids}, ctx)
    assert is_error and body["type"] == "validation-error"


async def test_get_as_json_is_the_stored_form(provider, ctx, world):
    await call(provider, "canvas_apply", {"source": G("playlist_create.cl")}, ctx)
    await _click(world, "genre", "change", value="Lo-fi")
    await _click(world, "play", "click")
    is_error, body = await call(provider, "canvas_get", {"format": "json"}, ctx)
    row = await world.canvas("s1")
    assert not is_error and body["format"] == "json" and body["rev"] == 2
    assert body["elements"] == row.elements and body["values"] == {"genre": "Lo-fi"} and body["value_set_by"] == {"genre": "user"}
    assert [(e["id"], e["kind"], e["element"]) for e in body["events"]] == [("e1", "change", "genre"), ("e2", "click", "play")]
    assert "client_event_id" not in body["events"][0] and "more_events" not in body
    again = (await call(provider, "canvas_get", {"format": "json"}, ctx))[1]
    assert again["events"] == [], "the events were marked seen"


async def test_get_as_json_of_some_ids_holds_only_their_subtree(provider, ctx, world):
    await call(provider, "canvas_apply", {"source": G("playlist_create.cl")}, ctx)
    await _click(world, "genre", "change", value="Lo-fi")
    body = (await call(provider, "canvas_get", {"format": "json", "ids": ["controls"]}, ctx))[1]
    assert list(body["elements"]) == ["controls", "genre", "play"] and body["values"] == {"genre": "Lo-fi"}
    other = (await call(provider, "canvas_get", {"format": "json", "ids": ["title"]}, ctx))[1]
    assert list(other["elements"]) == ["title"] and other["values"] == {} and other["value_set_by"] == {}


async def test_more_than_twenty_events_are_shown_twenty_at_a_time_and_none_are_lost(provider, ctx, world):
    await call(provider, "canvas_apply", {"source": G("playlist_create.cl")}, ctx)
    await write_canvas_settings(world.sp, CanvasSettings(id=CANVAS_SETTINGS_ID, max_waking_events_per_minute=120))
    for _ in range(25):
        await _click(world, "play", "click")
    first = (await call(provider, "canvas_get", {}, ctx))[1]["source"]
    assert "# events since your last read (25):" in first and first.count("#   e") == 20 and "#   ... and 5 more" in first
    assert (await world.canvas("s1")).seen_event_seq == 20
    second = (await call(provider, "canvas_get", {}, ctx))[1]["source"]
    assert "# events since your last read (5):" in second and "#   e25 play click" in second and "and" not in second.split("(5):")[1]
    assert (await world.canvas("s1")).seen_event_seq == 25
    body = (await call(provider, "canvas_get", {"format": "json"}, ctx))[1]
    assert body["events"] == []


async def test_json_get_counts_the_events_it_did_not_show(provider, ctx, world):
    await call(provider, "canvas_apply", {"source": G("playlist_create.cl")}, ctx)
    await write_canvas_settings(world.sp, CanvasSettings(id=CANVAS_SETTINGS_ID, max_waking_events_per_minute=120))
    for _ in range(22):
        await _click(world, "play", "click")
    body = (await call(provider, "canvas_get", {"format": "json"}, ctx))[1]
    assert len(body["events"]) == 20 and body["more_events"] == 2 and body["events"][-1]["id"] == "e20"
    rest = (await call(provider, "canvas_get", {"format": "json"}, ctx))[1]
    assert [e["id"] for e in rest["events"]] == ["e21", "e22"] and "more_events" not in rest


# ---- a call that has no canvas to act on ---------------------------------------------------------------------------------------------


UNSUPPORTED = {"type": "unsupported-surface", "message": "the canvas belongs to a workspace session; this call has none"}


@pytest.mark.parametrize("tool, arguments", [("canvas_get", {}), ("canvas_apply", {"source": "clear"})])
async def test_without_a_session_the_tools_refuse(provider, tool, arguments):
    assert await call(provider, tool, arguments, None) == (True, UNSUPPORTED)
    chat = ToolContext(tool_call_id="c", session_id=None, workspace_id=None, chat_id="chat-1")
    assert await call(provider, tool, arguments, chat) == (True, UNSUPPORTED)
    half = ToolContext(tool_call_id="c", session_id="s1", workspace_id=None)
    assert await call(provider, tool, arguments, half) == (True, UNSUPPORTED)


async def test_a_tool_acts_on_the_canvas_of_the_calling_session_only(provider, world):
    await seed_session(world, "s2", "ws-1")
    mine = ToolContext(tool_call_id="c1", session_id="s1", workspace_id="ws-1")
    other = ToolContext(tool_call_id="c2", session_id="s2", workspace_id="ws-1")
    await call(provider, "canvas_apply", {"source": 'root = Column([t])\nt = Text("mine")'}, mine)
    assert (await call(provider, "canvas_get", {}, other))[1]["source"] == "# canvas rev 0: empty\n"
    assert (await world.canvas("s2")) is None and (await world.canvas("s1")).rev == 1
    schema = (await _tools(provider))["canvas_apply"].args_schema
    assert not {"session_id", "workspace_id", "session"} & set(schema["properties"]), "no argument names another session"


# ---- the tools as the model reads them -----------------------------------------------------------------------------------------------


async def _tools(provider) -> dict[str, Any]:
    return {t.id: t async for t in provider.list_tools()}


async def test_the_tool_flags(provider):
    tools = await _tools(provider)
    assert list(tools) == ["canvas_get", "canvas_apply", "canvas_wait"]
    assert {t.required_role for t in tools.values()} == {"user"} and all(t.requires_workspace for t in tools.values())
    assert (tools["canvas_apply"].interruptible, tools["canvas_get"].interruptible, tools["canvas_wait"].interruptible) == (False, True, True)
    assert (tools["canvas_get"].yields, tools["canvas_apply"].yields, tools["canvas_wait"].yields) == (False, False, True)
    assert all(t.toolset_id == "canvas" for t in tools.values())
    assert all(provider.requires_workspace(name) and provider.required_role(name) == "user" for name in tools)


async def test_the_tools_are_not_exposable_over_mcp(provider):
    """They act on the calling session's canvas (Decision 16), and canvas_wait parks: neither survives the stateless MCP surface."""
    from primer.mcp.safety import is_exposable

    reasons = {"canvas_get": "needs_workspace", "canvas_apply": "needs_workspace", "canvas_wait": "yielding_unsupported"}
    tools = await _tools(provider)
    assert {tid: is_exposable(tool, provider=provider) for tid, tool in tools.items()} == {tid: (False, why) for tid, why in reasons.items()}


async def test_the_apply_description_teaches_the_whole_language(provider):
    tool = (await _tools(provider))["canvas_apply"]
    assert tool.description.startswith("Change this session's canvas, the panel above the chat, with one atomic batch of Canvas Lang statements")
    for name in CATALOG:
        assert signature(name) in tool.description, f"the description does not teach {name}"
    for phrase in ("`id = Type(args)` creates or replaces an element", "`id += [a, b]` appends children", "`del a, b` removes elements",
                   '"""triple quotes"""', "A Button click wakes you with a <canvas_event> message", "a Select change does not unless wake=true",
                   "every error names its line and the expected signature: fix those lines and resend."):
        assert phrase in tool.description, phrase
    assert "Example: " in tool.description and "base_rev refuses the batch if the user changed the canvas since rev 1" in tool.description


async def test_the_get_description_is_the_plans(provider):
    tool = (await _tools(provider))["canvas_get"]
    assert tool.description.startswith(
        "Read this session's canvas, the panel above the chat, as a Canvas Lang program you can edit and resend, with the values the user set "
        "and the canvas events since your last read.\n\nUse when you need the canvas's current state before changing it"
    )
    assert "format=json returns the stored form." in tool.description


async def test_the_apply_schema_shows_the_five_ops_and_the_limits(provider):
    schema = (await _tools(provider))["canvas_apply"].args_schema
    assert schema["properties"]["source"]["anyOf"][0]["maxLength"] == MAX_SOURCE_CHARS
    assert schema["properties"]["base_rev"]["anyOf"][0]["minimum"] == 0
    ops = schema["properties"]["ops"]["anyOf"][0]["items"]
    assert {ref["$ref"].rsplit("/", 1)[1] for ref in ops["oneOf"]} == {"SetOp", "PropOp", "AppendOp", "DeleteOp", "ClearOp"}


def test_the_id_pattern_is_the_languages_id_rule():
    """The JSON schema states the id rule as ``ID_PATTERN`` and pydantic enforces it; ``IDENT_RE`` is the rule the language itself uses."""
    from primer.toolset.canvas import _CanvasGetArgs

    words = ["a", "z9", "play_btn", "a" * 32, "a" * 33, "A", "1a", "_a", "a-b", "a b", "", "a\n", "\na", "root", "\u00e9", "ok_2"]
    taken = []
    for word in words:
        try:
            _CanvasGetArgs.model_validate({"ids": [word]})
            taken.append(True)
        except ValidationError:
            taken.append(False)
    assert taken == [bool(IDENT_RE.match(word)) for word in words]
    schema = _CanvasGetArgs.model_json_schema()
    assert schema["properties"]["ids"]["anyOf"][0]["items"]["pattern"] == ID_PATTERN
    assert [bool(re.search(ID_PATTERN, word)) for word in words if "\n" not in word] == [bool(IDENT_RE.match(word)) for word in words if "\n" not in word]


# ---- the registrations ---------------------------------------------------------------------------------------------------------------


async def test_canvas_is_a_reserved_builtin_resolved_without_storage(fake_storage_provider):
    assert "canvas" in RESERVED_TOOLSET_IDS and CANVAS_TOOLSET_ID == "canvas"
    p = build_canvas_toolset(storage_provider=fake_storage_provider)
    reg = ProviderRegistry(
        fake_storage_provider,
        llm_factory=lambda p: object(), embedder_factory=lambda p: object(),
        cross_encoder_factory=lambda p: object(), toolset_factory=lambda t: object(),
        canvas_toolset_provider=p,
    )
    assert await reg.get_toolset("canvas") is p


@pytest.mark.asyncio
async def test_the_catalogue_lists_canvas_last_and_opens_it(client):
    ids = [it["id"] for it in (await client.get("/v1/toolsets/builtin")).json()["items"]]
    assert ids[-1] == "canvas" and len(ids) == 10
    detail = (await client.get("/v1/toolsets/canvas")).json()
    assert detail["builtin"] is True and detail["description"] == "Show controls, media and HTML above the chat, and read the user's clicks"
    grouped = {it["id"]: it for it in (await client.get("/v1/tools")).json()["items"]}
    assert grouped["canvas"]["available"] is True
    assert [t["id"] for t in grouped["canvas"]["tools"]] == ["canvas_get", "canvas_apply", "canvas_wait"]
    assert {t["scoped_id"] for t in grouped["canvas"]["tools"]} == {"canvas__canvas_get", "canvas__canvas_apply", "canvas__canvas_wait"}
    badges = {t["id"]: (t["yields"], t["requires_workspace"], t["required_role"], t["interruptible"]) for t in grouped["canvas"]["tools"]}
    assert badges == {
        "canvas_get": (False, True, "user", True), "canvas_apply": (False, True, "user", False), "canvas_wait": (True, True, "user", True),
    }


@pytest.mark.asyncio
async def test_the_flat_tool_catalogue_lists_the_canvas_tools(client):
    flat = {item["id"] for item in (await client.get("/v1/tools/catalogue")).json()["items"]}
    assert {"canvas__canvas_get", "canvas__canvas_apply", "canvas__canvas_wait"} <= flat


@pytest.mark.asyncio
async def test_the_app_builds_the_toolset_the_registry_serves(app):
    assert isinstance(app.state.canvas_toolset, CanvasToolsetProvider)
    assert await app.state.provider_registry.get_toolset("canvas") is app.state.canvas_toolset


def test_the_catalogue_reserved_ids_and_the_docs_registry_agree():      # pivot P4's requirement
    from primer.api.routers import providers

    assert "canvas" in RESERVED_TOOLSET_IDS
    assert {spec["id"] for spec in providers._BUILTIN_TOOLSETS} == RESERVED_TOOLSET_IDS == set(built_in_providers())
    # The docs registry keys a toolset's tools by their bare ids (docs write ``canvas::canvas_get`` or ``canvas__canvas_get``: the scan splits
    # the toolset off); the scoped id is the catalog entry's (the next test).
    assert {"canvas_get", "canvas_apply", "canvas_wait"} <= set(built_in_tools()["canvas"])


async def test_the_tool_catalog_lists_the_canvas_tools():               # the wiki's reference pages are built from it
    entries = await collect_tool_entries(built_in_providers())
    canvas = {e.scoped_id: e for e in entries if e.toolset_id == "canvas"}
    assert set(canvas) == {"canvas__canvas_get", "canvas__canvas_apply", "canvas__canvas_wait"}
    assert all(e.required_role == "user" and e.needs_workspace and e.via_call_tool for e in canvas.values())
    assert all(e.default_gate is None for e in canvas.values())
    assert {name: e.pauses_turn for name, e in canvas.items()} == {
        "canvas__canvas_get": False, "canvas__canvas_apply": False, "canvas__canvas_wait": True,
    }


def test_the_console_names_the_canvas_toolset_last():
    source = (Path(__file__).resolve().parents[2] / "ui" / "components" / "toolsets.jsx").read_text(encoding="utf-8")
    assert re.search(r'const TS_BUILTIN_RESERVED_IDS = \[[^\]]*, "crud", "canvas"\];', source)

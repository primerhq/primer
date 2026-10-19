from __future__ import annotations

import json
from pathlib import Path

import pytest
from py_mini_racer import MiniRacer

SOURCE = (Path(__file__).resolve().parents[2] / "ui" / "components" / "toolsets.jsx").read_text(encoding="utf-8")
HELPERS = SOURCE[SOURCE.index("const TS_BUILTIN_RESERVED_IDS"):SOURCE.index("const TS_TABS")]

_OPEN: list = []


@pytest.fixture(autouse=True)
def _close_contexts():
    yield
    while _OPEN:
        _OPEN.pop().close()


def _ctx(answer: bool = True) -> MiniRacer:
    ctx = MiniRacer()
    _OPEN.append(ctx)
    ctx.eval("var window = globalThis; var __dialogs = []; var __overlays = [];")
    ctx.eval("window.confirmDialog = function (o) { __dialogs.push(o); return Promise.resolve(" + ("true" if answer else "false") + "); };")
    ctx.eval("var admin = { role: 'admin', openOverlay: function (n, s, i) { __overlays.push([n, s, i]); } };")
    ctx.eval("var plain = { role: 'user', openOverlay: function (n, s, i) { __overlays.push([n, s, i]); } };")
    ctx.eval(HELPERS)
    return ctx


def _status(ctx: MiniRacer, row: dict, con: str) -> dict:
    """con is "admin", "plain" or "undefined"."""
    return json.loads(ctx.eval(f"JSON.stringify(TS_toolsetCardStatus({json.dumps(row)}, {con}))"))


OFF = {"id": "collections", "builtin": True, "available": True, "internal_search": False, "tools": []}
DIALOG = {
    "title": "Internal search is off",
    "message": ("Internal search indexes the system collection, the map of this install's agents, graphs, collections and tools, "
                "so agents can search it by meaning with the Collections toolset's search tool. While it is off, that search "
                "matches keywords only."),
    "confirmLabel": "Open Internal collections",
    "cancelLabel": "Close",
}


def test_collections_with_internal_search_off_shows_the_chip() -> None:
    ctx = _ctx()
    assert _status(ctx, OFF, "admin") == {"label": "Internal search: off", "tone": "warn", "testid": "ts-internal-search-chip"}
    assert ctx.eval(f"typeof TS_toolsetCardStatus({json.dumps(OFF)}, admin).onClick") == "function"


@pytest.mark.parametrize("state", [True, None, "missing"])
def test_collections_with_internal_search_on_or_unknown_shows_available(state) -> None:
    row = {k: v for k, v in OFF.items() if k != "internal_search"} if state == "missing" else {**OFF, "internal_search": state}
    assert _status(_ctx(), row, "admin") == {"label": "available", "tone": "ok"}


def test_an_unavailable_collections_toolset_says_unavailable() -> None:
    assert _status(_ctx(), {**OFF, "available": False}, "admin") == {"label": "unavailable", "tone": "error"}


def test_only_the_collections_toolset_gets_the_chip() -> None:
    assert _status(_ctx(), {**OFF, "id": "system"}, "admin") == {"label": "available", "tone": "ok"}


def test_a_non_admin_sees_the_chip_but_cannot_open_it() -> None:
    ctx = _ctx()
    assert _status(ctx, OFF, "plain")["label"] == "Internal search: off"
    assert ctx.eval(f"typeof TS_toolsetCardStatus({json.dumps(OFF)}, plain).onClick") == "undefined"


def test_without_a_console_handle_the_chip_cannot_be_opened() -> None:
    ctx = _ctx()
    assert ctx.eval(f"typeof TS_toolsetCardStatus({json.dumps(OFF)}).onClick") == "undefined"


def test_the_chip_opens_the_dialog_with_the_exact_copy() -> None:
    ctx = _ctx()
    ctx.eval(f"TS_toolsetCardStatus({json.dumps(OFF)}, admin).onClick();")
    assert json.loads(ctx.eval("JSON.stringify(__dialogs)")) == [DIALOG]


def test_open_internal_collections_opens_its_overlay() -> None:
    ctx = _ctx(answer=True)
    ctx.eval(f"TS_toolsetCardStatus({json.dumps(OFF)}, admin).onClick();")
    assert json.loads(ctx.eval("JSON.stringify(__overlays)")) == [["internal-collections", None, None]]


def test_close_opens_nothing() -> None:
    ctx = _ctx(answer=False)
    ctx.eval(f"TS_toolsetCardStatus({json.dumps(OFF)}, admin).onClick();")
    assert json.loads(ctx.eval("JSON.stringify(__overlays)")) == []


def test_the_dialog_copy_is_the_constant_and_has_no_em_dash() -> None:
    copy = json.loads(_ctx().eval("JSON.stringify(TS_INTERNAL_SEARCH_DIALOG)"))
    assert copy == DIALOG
    assert "\u2014" not in json.dumps(copy, ensure_ascii=False)

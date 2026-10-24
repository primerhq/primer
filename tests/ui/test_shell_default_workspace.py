"""The console's first-load workspace is the default one (pivot P2 Task 14).

``GET /v1/workspaces`` flags the default workspace (prime) with a response-only ``is_default``. When the address names no workspace the shell
picks that row, else the first listed (an install with more workspaces than the console lists, or an older server that sends no flag), else none.
``NV_defaultWid`` is the pick; it is sliced out of ``nv-shell.jsx`` and run in V8.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests._support.js_source import blank, close_of

ROOT = Path(__file__).resolve().parents[2]
SHELL = (ROOT / "ui" / "components" / "console" / "nv-shell.jsx").read_text(encoding="utf-8")


def _function(name: str) -> str:
    """The source of one top-level ``function <name>(...) {...}`` of nv-shell.jsx."""
    start = SHELL.index(f"function {name}(")
    code = blank(SHELL, strings=True, templates=True)
    body = code.index("{", code.index(")", start))
    end = close_of(code, body)
    assert end > 0, name
    return SHELL[start:end]


@pytest.fixture
def ev():
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval(_function("NV_defaultWid"))
    try:
        yield lambda expr: json.loads(ctx.eval(f"JSON.stringify({{v: {expr}}})")).get("v")
    finally:
        ctx.close()


def test_the_flagged_workspace_wins(ev) -> None:
    assert ev('NV_defaultWid([{id: "a"}, {id: "prime", is_default: true}])') == "prime"


def test_without_a_flag_the_first_listed_is_picked(ev) -> None:
    assert ev('NV_defaultWid([{id: "a"}, {id: "b"}])') == "a"
    assert ev('NV_defaultWid([{id: "a", is_default: false}, {id: "b", is_default: "yes"}])') == "a", "only true is the flag"


def test_no_workspace_is_no_pick(ev) -> None:
    assert ev("NV_defaultWid([])") is None
    assert ev("NV_defaultWid(null)") is None


def test_the_pick_is_exported_and_used_by_the_default_effect() -> None:
    assert "window.NV_defaultWid = NV_defaultWid;" in SHELL
    effect = SHELL[SHELL.index("// Default workspace: the URL's, else the flagged one"):]
    effect = effect[:effect.index("}, [wid, wsItems.length]);")]
    assert "setWid(function (current) { return current || NV_defaultWid(wsItems); })" in effect
    assert "wsItems[0].id" not in effect, "the first listed is only the fallback inside NV_defaultWid"

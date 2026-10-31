"""The console reads the CALLER's default workspace (access-control P2 Task 8, spec 6.7).

``GET /v1/workspaces`` flags ``is_default`` per person: each user's own ``ws-<id>``, never one install-wide row. The shell, the Agent
"Chat" button and the graph builder find the default by that flag alone, so none of them may name ``prime`` or ``primer``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests._support.js_source import blank, close_of

ROOT = Path(__file__).resolve().parents[2]
SHELL = (ROOT / "ui" / "components" / "console" / "nv-shell.jsx").read_text(encoding="utf-8")


def _function(name: str) -> str:
    start = SHELL.index(f"function {name}(")
    code = blank(SHELL, strings=True, templates=True)
    end = close_of(code, code.index("{", code.index(")", start)))
    assert end > 0, name
    return SHELL[start:end]


@pytest.fixture
def ev():
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    ctx.eval(_function("NV_defaultWid") + _function("NV_flaggedWid"))
    try:
        yield lambda expr: json.loads(ctx.eval(f"JSON.stringify({{v: {expr}}})")).get("v")
    finally:
        ctx.close()


def test_the_shell_picks_the_callers_flagged_workspace(ev):
    # Ada's list flags her own workspace; a shared one and prime are listed but not hers.
    ada = [{"id": "prime"}, {"id": "ws-user-bob"}, {"id": "ws-user-ada", "is_default": True}]
    bob = [{"id": "prime"}, {"id": "ws-user-bob", "is_default": True}]
    assert ev(f"NV_defaultWid({json.dumps(ada)})") == "ws-user-ada"
    assert ev(f"NV_flaggedWid({json.dumps(ada)})") == "ws-user-ada"
    assert ev(f"NV_flaggedWid({json.dumps(bob)})") == "ws-user-bob"


def test_a_user_with_no_default_is_never_given_another_one_to_open_a_session_in(ev):
    rows = [{"id": "prime"}, {"id": "ws-user-bob"}]
    assert ev(f"NV_flaggedWid({json.dumps(rows)})") is None


@pytest.mark.parametrize("path", ["ui/components/agents.jsx", "ui/components/graph-builder/gb-api.jsx", "ui/components/console/nv-shell.jsx"])
def test_no_console_reader_hardcodes_the_default_id(path):
    src = (ROOT / path).read_text(encoding="utf-8")
    assert "is_default === true" in src
    assert '"prime"' not in src and '"primer"' not in src

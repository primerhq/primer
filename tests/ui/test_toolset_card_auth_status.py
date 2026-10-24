"""Toolset cards prefer the shared OAuth sign-in state when one exists."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.ui._mini_react import mini_react_context, transpile

ROOT = Path(__file__).resolve().parents[2]
AUTH = ROOT / "ui" / "components" / "toolsets" / "mcp-auth.jsx"
PLATFORM = ROOT / "ui" / "components" / "console" / "nv-platform.jsx"
KIT = ROOT / "ui" / "components" / "shared" / "platform-kit.jsx"
TOOLSETS = ROOT / "ui" / "components" / "toolsets.jsx"

PRELUDE = r"""
function PK_Section() { return null; } function PK_Segmented() { return null; }
function PK_Grid() { return null; } function PK_Field() { return null; }
function PK_Fold() { return null; } function TS_KvEditor() { return null; }
"""


@pytest.fixture
def auth():
    ctx = mini_react_context(transpile(AUTH), PRELUDE)
    try:
        yield ctx
    finally:
        ctx.close()


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("connected", {"label": "Connected", "tone": "ok"}),
        ("needs_sign_in", {"label": "Needs sign-in", "tone": "warn"}),
        ("error", {"label": "Error", "tone": "error"}),
        (None, None),
    ],
)
def test_card_auth_status(auth, state: str | None, expected: dict | None) -> None:
    row = {"auth": {"state": state}} if state else {}
    got = json.loads(auth.eval(f"JSON.stringify(TS_cardAuthStatus({json.dumps(row)}))"))
    assert got == expected


def _toolsets_entry() -> str:
    source = PLATFORM.read_text(encoding="utf-8")
    start = source.index("  toolsets: {")
    end = source.index("\n  tools: {", start)
    return source[start:end]


def test_the_platform_card_prefers_the_sign_in_state() -> None:
    # Since revamp 06 Task 6 the fallback is toolsets.jsx's TS_toolsetCardStatus (available, unavailable or the Collections card's internal-search chip),
    # not an inline available/unavailable pair; the sign-in state still comes first. tests/ui/test_platform_toolsets_cards.py runs the composition.
    block = _toolsets_entry()
    assert "window.TS_cardAuthStatus" in block
    assert "window.TS_toolsetCardStatus(row, con)" in block
    assert block.index("window.TS_cardAuthStatus") < block.index("window.TS_toolsetCardStatus(row, con)")


def test_a_toolset_without_oauth_keeps_its_available_status() -> None:
    # The available/unavailable pair moved from the Platform entry into TS_toolsetCardStatus (revamp 06 Task 5), which the entry calls.
    source = TOOLSETS.read_text(encoding="utf-8")
    helper = source[source.index("function TS_toolsetCardStatus("):source.index("const TS_TABS")]
    assert '{ label: "available", tone: "ok" }' in helper
    assert '{ label: "unavailable", tone: "error" }' in helper
    assert "window.TS_toolsetCardStatus(row, con)" in _toolsets_entry()


# ---- G4-12: a toolset whose server does not answer never looks healthy ---------------------------------------------------------------------------------------

REASON = "MCP server unreachable: connection refused"


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("connected", {"label": "Connected, unreachable", "tone": "error", "title": REASON}),
        ("needs_sign_in", {"label": "Needs sign-in", "tone": "warn", "title": REASON}),
        ("error", {"label": "Error", "tone": "error", "title": REASON}),
    ],
)
def test_an_unavailable_toolset_says_so_with_the_reason(auth, state: str, expected: dict) -> None:
    row = {"auth": {"state": state}, "available": False, "unavailable_reason": REASON}
    got = json.loads(auth.eval(f"JSON.stringify(TS_cardAuthStatus({json.dumps(row)}))"))
    assert got == expected


def test_an_available_connected_toolset_is_connected(auth) -> None:
    row = {"auth": {"state": "connected"}, "available": True}
    assert json.loads(auth.eval(f"JSON.stringify(TS_cardAuthStatus({json.dumps(row)}))")) == {"label": "Connected", "tone": "ok"}


def test_the_platform_card_shows_the_status_reason_as_its_tooltip() -> None:
    """The Platform card (nv-platform.jsx toolsets entry) hands TS_cardAuthStatus's status to PK_Card, which draws its `title`."""
    ctx = mini_react_context(transpile(AUTH) + "\n" + transpile(KIT), PRELUDE)
    try:
        row = {"id": "kb", "auth": {"state": "connected"}, "available": False, "unavailable_reason": REASON}
        ctx.eval("MR.mount(PK_Card, { id: 'kb', title: 'kb', status: TS_cardAuthStatus(" + json.dumps(row) + "), onOpen: function () {} })")
        status = ctx.eval("JSON.stringify({ text: MR.find('pk-card-status:kb').props.children, title: MR.find('pk-card-status:kb').props.title })")
        assert json.loads(status) == {"text": "Connected, unreachable", "title": REASON}
        ctx.eval("MR.mount(PK_Card, { id: 'kb', title: 'kb', status: { label: 'available', tone: 'ok' }, onOpen: function () {} })")
        assert ctx.eval("MR.find('pk-card-status:kb').props.title === undefined")
    finally:
        ctx.close()


PAGE_PRELUDE = PRELUDE + r"""
function Icon() { return null; } function Btn(p) { return React.createElement("button", p, p.children); } function Banner() { return null; }
function Fab() { return null; }
function Card(p) { return React.createElement("div", { "data-testid": "card:" + p.title }, p.pill); }
function CardList(p) { return React.createElement("div", null, p.items.map(function (t) { return React.createElement("div", { key: t.id }, p.renderCard(t)); })); }
var ITEMS = []; var MOBILE = false;
window.primerApi = {
  useRouter: function () { return { navigate: function () {} }; },
  useViewport: function () { return { isMobile: MOBILE }; },
  useResource: function () { return { data: { items: ITEMS }, loading: false, error: null, refetch: function () {} }; },
  apiFetch: function () { return Promise.resolve({}); },
};
"""


@pytest.mark.parametrize("mobile", [False, True])
def test_the_toolsets_list_shows_a_connected_but_unreachable_toolset(mobile: bool) -> None:
    ctx = mini_react_context(transpile(AUTH) + "\n" + transpile(TOOLSETS), PAGE_PRELUDE)
    try:
        ctx.eval("MOBILE = " + json.dumps(mobile))
        ctx.eval("ITEMS = " + json.dumps([{"id": "kb", "builtin": False, "tools": [], "auth": {"state": "connected"},
                                           "available": False, "unavailable_reason": REASON}]))
        ctx.eval("MR.mount(ToolsetsPage, { pushToast: function () {} })")
        pill = ctx.eval("JSON.stringify((function (el) { return el && { cls: el.props.className, text: el.props.children[1] }; })(MR.findProp('title', "
                        + json.dumps(REASON) + ")))")
        assert json.loads(pill) == {"cls": "pill pill-cancelled", "text": "Connected, unreachable"}
    finally:
        ctx.close()

"""The Platform Toolsets page shows the Collections card's internal-search chip, an unavailable toolset's reason and the compact action (revamp 06 Task 6).

The ``toolsets`` entry of ``NV_PLAT_PAGES`` (``ui/components/console/nv-platform.jsx``) builds each card's status with ``TS_toolsetCardStatus(row, con)``
(``ui/components/toolsets.jsx``, revamp 06 Task 5) after the MCP sign-in status (``TS_cardAuthStatus``), and adds two facts: the reason of an unavailable
toolset, and "internal search: off" on the Collections card (the phone's fact sheet draws facts, so a phone shows the state too). The chip opens the
internal-search dialog for an admin; only its "Open Internal collections" opens an overlay, and never the toolset's own (Review Focus 5).

The first half runs the plain-JS helper block of ``toolsets.jsx`` and the ``NV_PLAT_PAGES`` slice of ``nv-platform.jsx`` in MiniRacer; the second
mounts the real page (``tests/ui/_platform_page_harness.py``, which loads the same helper block, as ``ui/index.html`` loads ``toolsets.jsx``
before the page).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from py_mini_racer import MiniRacer

from tests.ui._platform_page_harness import platform_page_context

ROOT = Path(__file__).resolve().parents[2]
TOOLSETS = (ROOT / "ui" / "components" / "toolsets.jsx").read_text(encoding="utf-8")
HELPERS = TOOLSETS[TOOLSETS.index("const TS_BUILTIN_RESERVED_IDS"):TOOLSETS.index("const TS_TABS")]
PLAT = (ROOT / "ui" / "components" / "console" / "nv-platform.jsx").read_text(encoding="utf-8")
PAGES = PLAT[PLAT.index("function NV_fact("):PLAT.index("var NV_PLAT_PAGE_SIZE")]
AUTH = (ROOT / "ui" / "components" / "toolsets" / "mcp-auth.jsx").read_text(encoding="utf-8")
_AUTH_START = AUTH.index("function TS_cardAuthStatus(")
CARD_AUTH_STATUS = AUTH[_AUTH_START:AUTH.index("\n}\n", _AUTH_START) + 3]

OFF = {"id": "collections", "builtin": True, "available": True, "internal_search": False, "tools": [{"id": "search"}]}
DOWN = {"id": "my-mcp", "builtin": False, "available": False, "unavailable_reason": "did not respond within 5s", "tools": []}
DIALOG_TITLE = "Internal search is off"

_OPEN: list = []


@pytest.fixture(autouse=True)
def _close_contexts():
    yield
    while _OPEN:
        _OPEN.pop().close()


def _ctx(answer: bool = True, with_auth: bool = False) -> MiniRacer:
    ctx = MiniRacer()
    _OPEN.append(ctx)
    ctx.eval("var window = globalThis; var __dialogs = []; var __overlays = [];")
    ctx.eval("window.confirmDialog = function (o) { __dialogs.push(o); return Promise.resolve(" + ("true" if answer else "false") + "); };")
    if with_auth:
        ctx.eval(CARD_AUTH_STATUS + "\nwindow.TS_cardAuthStatus = TS_cardAuthStatus;")
    ctx.eval(HELPERS)
    ctx.eval("window.TS_toolsetCardStatus = TS_toolsetCardStatus;")
    ctx.eval(PAGES)
    ctx.eval("var admin = { role: 'admin', openOverlay: function (n, s, i) { __overlays.push([n, s, i]); } };")
    ctx.eval("var plain = { role: 'user', openOverlay: function (n, s, i) { __overlays.push([n, s, i]); } };")
    return ctx


def _js(ctx: MiniRacer, expr: str):
    return json.loads(ctx.eval(f"JSON.stringify({expr})"))


def _card(ctx: MiniRacer, row: dict, con: str | None = "admin") -> dict:
    arg = f"{json.dumps(row)}, {con}" if con else json.dumps(row)
    return _js(ctx, f"NV_PLAT_PAGES.toolsets.card({arg})")


def test_the_toolsets_page_action_is_compact() -> None:
    assert _js(_ctx(), "NV_PLAT_PAGES.toolsets.createLabel") == "+ Toolset"


def test_the_collections_card_with_internal_search_off_carries_the_chip() -> None:
    ctx = _ctx()
    card = _card(ctx, OFF)
    assert card["status"] == {"label": "Internal search: off", "tone": "warn", "testid": "ts-internal-search-chip"}
    assert ctx.eval(f"typeof NV_PLAT_PAGES.toolsets.card({json.dumps(OFF)}, admin).status.onClick") == "function"
    assert ["internal search", "off"] in card["facts"]
    assert card["badge"] == "built in" and ["tools", "1"] in card["facts"]


def test_the_chip_opens_the_dialog_and_only_its_confirm_opens_an_overlay() -> None:
    ctx = _ctx(answer=True)
    ctx.eval(f"NV_PLAT_PAGES.toolsets.card({json.dumps(OFF)}, admin).status.onClick();")
    assert [d["title"] for d in _js(ctx, "__dialogs")] == [DIALOG_TITLE]
    assert _js(ctx, "__overlays") == [["internal-collections", None, None]], "Open Internal collections opens that overlay, never the toolset's"


def test_closing_the_dialog_opens_nothing() -> None:
    ctx = _ctx(answer=False)
    ctx.eval(f"NV_PLAT_PAGES.toolsets.card({json.dumps(OFF)}, admin).status.onClick();")
    assert [d["title"] for d in _js(ctx, "__dialogs")] == [DIALOG_TITLE]
    assert _js(ctx, "__overlays") == []


def test_a_non_admin_sees_the_chip_and_the_fact_but_cannot_open_the_dialog() -> None:
    ctx = _ctx()
    card = _card(ctx, OFF, con="plain")
    assert card["status"]["label"] == "Internal search: off"
    assert ["internal search", "off"] in card["facts"]
    assert ctx.eval(f"typeof NV_PLAT_PAGES.toolsets.card({json.dumps(OFF)}, plain).status.onClick") == "undefined"


def test_a_card_built_without_a_console_handle_keeps_the_fact_and_cannot_open_the_dialog() -> None:
    ctx = _ctx()
    card = _card(ctx, OFF, con=None)
    assert ["internal search", "off"] in card["facts"]
    assert ctx.eval(f"typeof NV_PLAT_PAGES.toolsets.card({json.dumps(OFF)}).status.onClick") == "undefined"


def test_an_unavailable_card_states_its_reason() -> None:
    card = _card(_ctx(), DOWN)
    assert card["status"] == {"label": "unavailable", "tone": "error"}
    assert ["reason", "did not respond within 5s"] in card["facts"]


@pytest.mark.parametrize("state", [True, None])
def test_collections_with_internal_search_on_or_unknown_has_no_chip_or_fact(state) -> None:
    card = _card(_ctx(), {**OFF, "internal_search": state})
    assert card["status"] == {"label": "available", "tone": "ok"}
    assert "internal search" not in [f[0] for f in card["facts"] if f]


def test_an_available_card_has_no_reason_or_search_fact() -> None:
    card = _card(_ctx(), {"id": "system", "builtin": True, "available": True, "tools": [{"id": "ask_user"}]})
    keys = [f[0] for f in card["facts"] if f]
    assert "reason" not in keys and "internal search" not in keys
    assert card["status"] == {"label": "available", "tone": "ok"}


def test_the_facts_come_in_their_order() -> None:
    row = {**DOWN, "id": "collections", "internal_search": False, "source": "git", "tools": [{"id": "a"}, {"id": "b"}]}
    assert [f[0] for f in _card(_ctx(), row)["facts"] if f] == ["tools", "source", "reason", "internal search"]


def test_an_mcp_sign_in_status_still_wins_over_the_available_status() -> None:
    """The MCP OAuth state (revamp 07) is read first; a toolset without one falls through to TS_toolsetCardStatus."""
    ctx = _ctx(with_auth=True)
    signed_out = {"id": "kb", "builtin": False, "available": True, "auth": {"state": "needs_sign_in"}, "tools": []}
    assert _card(ctx, signed_out)["status"] == {"label": "Needs sign-in", "tone": "warn"}
    assert _card(ctx, OFF)["status"]["label"] == "Internal search: off"
    assert _card(ctx, DOWN)["status"] == {"label": "unavailable", "tone": "error"}


# ---- the real page --------------------------------------------------------------------------------------------------------------------------------


@pytest.fixture
def page_ctx():
    ctx = platform_page_context()
    try:
        yield ctx
    finally:
        ctx.close()


def _mount(ctx, rows: list[dict]) -> None:
    ctx.eval(f"ITEMS = {json.dumps(rows)}; MR.mount(NV_PlatListPage, {{ nav: 'toolsets' }});")


def test_on_the_page_the_chip_is_a_button_that_opens_the_dialog_and_never_the_toolset(page_ctx) -> None:
    _mount(page_ctx, [OFF, {"id": "system", "builtin": True, "available": True, "tools": []}])
    assert page_ctx.eval('MR.find("ts-internal-search-chip").type') == "button"
    assert page_ctx.eval('MR.find("ts-internal-search-chip").props.children') == "Internal search: off"
    page_ctx.eval('MR.click("ts-internal-search-chip")')
    assert [d["title"] for d in _js(page_ctx, "DIALOGS")] == [DIALOG_TITLE]
    overlays = [c for c in _js(page_ctx, "CALLS") if c[0] == "openOverlay"]
    assert overlays == [["openOverlay", "internal-collections", None, None]], "only the dialog's confirm opens an overlay, never the Collections toolset"


def test_on_the_page_a_non_admin_reads_the_chip_as_text(page_ctx) -> None:
    page_ctx.eval("CON.role = 'user';")
    _mount(page_ctx, [OFF])
    assert page_ctx.eval('MR.find("ts-internal-search-chip").type') == "span"
    assert page_ctx.eval('MR.find("ts-internal-search-chip").props.children') == "Internal search: off"

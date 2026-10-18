"""One card view model for every Platform page, ``card(row, con)``, and a card status that can act (Platform revamp Plan 01, Task 3; seam I-8).

Every entry of ``NV_PLAT_PAGES`` answers ``card(row, con)`` with ONE shape: ``{id, name, sub, badge?, icon?, status?, facts}``. ``id`` is the record id
(the scoped id for a tool); ``badge`` is a short neutral word ("built in", the toolset of a tool, a backend kind); ``status`` is ``{label, tone}``
with ``tone`` one of ok, warn, error and muted, and may carry ``onClick`` and ``testid``, which makes it a button (an "Internal search: off" chip
that opens a dialog, Plan 06); ``facts`` holds strings and ``[key, value]`` pairs (``NV_fact`` answers null for a missing value, and every renderer
drops it). ``con`` is the console (``NV_useConsole()``), so a status can open a dialog or an overlay. The old ``chip``, ``glyph``, ``color`` and
``_key`` are gone. The desktop cards draw the shape and the phone's fact sheet draws its badge and status (``test_mobile_more_reachability.py``).

The page runs here for real (``tests/ui/_platform_page_harness.py``: the kit and ``nv-platform.jsx``, transpiled, with the console stubbed).
"""

from __future__ import annotations

import json

import pytest

from tests.ui._platform_page_harness import platform_navs, platform_page_context
from tests.ui.test_platform_kit_page import css_body, declarations

ALLOWED = {"id", "name", "sub", "badge", "icon", "status", "facts"}
TONES = {"ok", "warn", "error", "muted"}
NAVS = platform_navs()


def js(ctx, expr: str):
    return json.loads(ctx.eval("JSON.stringify(" + expr + ")"))


@pytest.fixture
def page_ctx():
    ctx = platform_page_context()
    try:
        yield ctx
    finally:
        ctx.close()


def card(ctx, nav: str, row: str):
    return js(ctx, f"NV_PLAT_PAGES[{json.dumps(nav)}].card({row}, CON)")


def test_the_table_is_read_from_the_source() -> None:
    assert len(NAVS) >= 13 and {"profiles", "agents", "tools", "approvals"} <= set(NAVS)


@pytest.mark.parametrize("nav", NAVS)          # every key of NV_PLAT_PAGES, read from the source
def test_every_entry_returns_the_card_shape(page_ctx, nav) -> None:
    vm = card(page_ctx, nav, "SAMPLE_ROW")
    assert set(vm) <= ALLOWED and {"id", "name", "facts"} <= set(vm), sorted(vm)
    assert isinstance(vm["id"], str) and vm["id"] and isinstance(vm["name"], str)
    assert isinstance(vm.get("sub", ""), str)
    assert all(f is None or isinstance(f, str) or (isinstance(f, list) and len(f) == 2) for f in vm["facts"])
    assert vm.get("status") is None or (vm["status"]["tone"] in TONES and isinstance(vm["status"]["label"], str))
    assert vm.get("badge") is None or (isinstance(vm["badge"], str) and vm["badge"])
    assert vm.get("icon") is None or isinstance(vm["icon"]["path"], str)


@pytest.mark.parametrize("nav", NAVS)
def test_an_entry_keeps_its_row_id_as_the_card_id(page_ctx, nav) -> None:
    assert card(page_ctx, nav, '{ id: "x-1", toolset_id: "a", tool_name: "b" }')["id"] == "x-1"


def test_old_chips_become_badges_or_status(page_ctx) -> None:
    ts = card(page_ctx, "toolsets", '{ id: "web", builtin: true, available: false }')
    assert ts["badge"] == "built in" and ts["status"] == {"label": "unavailable", "tone": "error"}
    assert card(page_ctx, "toolsets", '{ id: "gh", kind: "mcp" }')["badge"] == "mcp"
    assert card(page_ctx, "toolsets", '{ id: "gh" }') == {"id": "gh", "name": "gh", "sub": "", "badge": "registered",
                                                          "status": {"label": "available", "tone": "ok"}, "facts": [None, None]}
    tool = card(page_ctx, "tools", '{ id: "web__search", description: "Find" }')
    assert tool["id"] == "web__search" and tool["name"] == "search" and tool["badge"] == "web"
    assert "badge" not in card(page_ctx, "tools", '{ id: "loose" }'), "a tool without a toolset has no badge"
    tr = card(page_ctx, "triggers", '{ id: "t1", enabled: false }')
    assert tr["status"] == {"label": "disabled", "tone": "muted"}
    assert card(page_ctx, "triggers", '{ id: "t1" }')["status"] == {"label": "enabled", "tone": "ok"}


@pytest.mark.parametrize(("nav", "row", "expected"), [
    ("agents", '{ id: "a", status: "ok" }', {"label": "ok", "tone": "ok"}),
    ("agents", '{ id: "a", status: "degraded" }', {"label": "degraded", "tone": "warn"}),
    ("agents", '{ id: "a" }', None),
    ("workspaces", '{ id: "w", status: "running" }', {"label": "running", "tone": "muted"}),
    ("workspaces", '{ id: "w" }', None),
    ("channels", '{ id: "c", status: "ok" }', {"label": "ok", "tone": "ok"}),
    ("channels", '{ id: "c", status: "error" }', {"label": "error", "tone": "warn"}),
    ("services", '{ id: "s", state: "running" }', {"label": "running", "tone": "ok"}),
    ("services", '{ id: "s", state: "stopped" }', {"label": "stopped", "tone": "muted"}),
    ("services", '{ id: "s" }', None),
    ("approvals", '{ id: "p", enabled: true }', {"label": "active", "tone": "ok"}),
    ("approvals", '{ id: "p", enabled: false }', {"label": "disabled", "tone": "muted"}),
    ("profiles", '{ id: "p" }', None),
    ("graphs", '{ id: "g" }', None),
])
def test_each_entry_maps_its_state_to_one_status(page_ctx, nav, row, expected) -> None:
    assert card(page_ctx, nav, row).get("status") == expected


@pytest.mark.parametrize(("nav", "row", "expected"), [
    ("templates", '{ id: "t", backend: { kind: "kubernetes" } }', "kubernetes"),
    ("templates", '{ id: "t" }', "local"),
    ("collections", '{ id: "c", system: true }', "system"),
    ("collections", '{ id: "c" }', None),
    ("harnesses", '{ id: "h", kind: "outbound" }', "outbound"),
    ("harnesses", '{ id: "h" }', None),
    ("profiles", '{ id: "p" }', None),
    ("agents", '{ id: "a" }', None),
])
def test_each_entry_names_its_badge(page_ctx, nav, row, expected) -> None:
    assert card(page_ctx, nav, row).get("badge") == expected


def test_agents_and_graphs_carry_their_type_icon_in_their_own_tone(page_ctx) -> None:
    agent = card(page_ctx, "agents", '{ id: "operator" }')
    assert agent["icon"] == {"path": page_ctx.eval("NV_PLAT_ICONS.agents"), "tone": "var(--teal)"}, "the agent's identity colour"
    graph = card(page_ctx, "graphs", '{ id: "g" }')
    assert graph["icon"] == {"path": page_ctx.eval("NV_PLAT_ICONS.graphs"), "tone": "var(--text-3)"}
    assert "icon" not in card(page_ctx, "profiles", '{ id: "p" }'), "the page's own icon is the default (the kit's card props)"


def test_the_list_page_hands_the_console_to_card(page_ctx) -> None:
    page_ctx.eval('var SEEN = []; NV_PLAT_PAGES.probe = { title: "Probes", noun: ["probe", "probes"], list: function () {},'
                  ' card: function (r, con) { SEEN.push(con); return { id: r.id, name: r.id, sub: "", facts: [] }; },'
                  ' open: rec("open") };'
                  ' ITEMS = [{ id: "p1" }]; MR.mount(NV_PlatListPage, { nav: "probe" });')
    assert page_ctx.eval("SEEN.length > 0 && SEEN.every(function (c) { return c === CON; })"), "card(row, con) gets the console"
    assert page_ctx.eval('!!MR.find("nv-pcard:p1")')


def test_an_actionable_status_on_a_platform_card_runs_its_action_and_does_not_open_it(page_ctx) -> None:
    page_ctx.eval('NV_PLAT_PAGES.probe = { title: "Probes", noun: ["probe", "probes"], list: function () {},'
                  ' card: function (r) { return { id: r.id, name: r.id, sub: "", facts: [],'
                  '   status: { label: "Internal search: off", tone: "warn", onClick: rec("chip"), testid: "probe-chip" } }; },'
                  ' open: rec("open") };'
                  ' ITEMS = [{ id: "p1" }]; MR.mount(NV_PlatListPage, { nav: "probe" });')
    chip = 'MR.find("probe-chip")'
    assert page_ctx.eval(f"{chip}.type") == "button" and page_ctx.eval(f"{chip}.props.type") == "button"
    assert page_ctx.eval(f"{chip}.props.children") == "Internal search: off", "named by its label"
    assert page_ctx.eval(f"{chip}.props.className").split() == ["nv-pcard-status", "nv-pcard-status-btn"]
    page_ctx.eval('MR.click("probe-chip")')
    assert [c[0] for c in js(page_ctx, "CALLS")] == ["chip"], "the status ran once and the card did not open"
    page_ctx.eval('MR.click("nv-pcard-open:p1")')
    assert [c[0] for c in js(page_ctx, "CALLS")] == ["chip", "open"]


def test_a_desktop_card_draws_the_badge_the_status_and_string_or_pair_facts(page_ctx) -> None:
    page_ctx.eval('NV_PLAT_PAGES.probe = { title: "Probes", noun: ["probe", "probes"], list: function () {},'
                  ' card: function (r) { return { id: r.id, name: "Probe one", sub: "a probe", badge: "built in",'
                  '   status: { label: "available", tone: "ok" }, facts: [null, "vLLM · 1 model", ["context", "8192"], "x", "y"] }; },'
                  ' open: rec("open") };'
                  ' ITEMS = [{ id: "p1" }]; MR.mount(NV_PlatListPage, { nav: "probe" });')
    texts = js(page_ctx, "MR.texts()")
    assert {"Probe one", "a probe", "built in", "available", "vLLM · 1 model", "context", "8192", "x"} <= set(texts)
    assert "y" not in texts, "at most three facts"
    tree = js(page_ctx, 'MR.subtree("nv-pcard:Probe one")')
    assert [e["className"] for e in tree if e["className"] in ("nv-pcard-badge", "nv-pcard-status")] == ["nv-pcard-badge", "nv-pcard-status"]


def test_the_status_button_sits_above_the_stretched_open() -> None:
    d = declarations(css_body(".nv-pcard-status-btn"))
    assert d.get("position") == "relative" and d.get("z-index") == "1"

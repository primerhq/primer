"""One card view model for every Platform page, ``card(row, con)``, and a card status that can act (Platform revamp Plan 01, Task 3; seam I-8).

Every entry of ``NV_PLAT_PAGES`` answers ``card(row, con)`` with ONE shape: ``{id, name, sub, label?, badge?, icon?, status?, facts}``. ``id`` is the record id
(the scoped id for a tool); ``label``, when set, names the card's controls where ``name`` alone is ambiguous (a tool's scoped id, audit G1-21); ``badge`` is a short neutral word ("built in", the toolset of a tool, a backend kind); ``status`` is ``{label, tone}``
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

ALLOWED = {"id", "name", "sub", "label", "badge", "icon", "status", "facts"}
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
    # 11 generic pages: Workspaces and Workspace templates left the table (Platform revamp Plan 01 Task 7, design 14.1 and 14.2); templates is a custom page now.
    assert len(NAVS) >= 11 and {"profiles", "agents", "tools", "approvals"} <= set(NAVS)
    assert not {"workspaces", "templates"} & set(NAVS)


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
    # agents: the card drops its status chip (open point 2 of Plan 05) -
    # the list rows carry no status, so even a row arriving with one
    # renders no chip (a per-card status fetch is out of scope).
    ("agents", '{ id: "a", status: "ok" }', None),
    ("agents", '{ id: "a", status: "degraded" }', None),
    ("agents", '{ id: "a" }', None),
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
    ("collections", '{ id: "c", system: true }', "system"),
    ("collections", '{ id: "c" }', None),
    ("harnesses", '{ id: "h", kind: "outbound" }', "outbound"),
    ("harnesses", '{ id: "h" }', None),
    # Platform revamp Plan 03, Task 4 (design 7.1): a profile's card names its kind, "imported" for a single profile (it comes from the Register
    # provider overlay's import) and "aggregated" for a chain of them (tests/ui/test_llm_profiles_page.py).
    ("profiles", '{ id: "p" }', "imported"),
    ("profiles", '{ id: "p", kind: "aggregated" }', "aggregated"),
    ("agents", '{ id: "a" }', None),
])
def test_each_entry_names_its_badge(page_ctx, nav, row, expected) -> None:
    assert card(page_ctx, nav, row).get("badge") == expected


def test_agents_and_graphs_carry_their_type_icon_in_their_own_tone(page_ctx) -> None:
    agent = card(page_ctx, "agents", '{ id: "operator" }')
    assert agent["icon"] == {"path": page_ctx.eval("NV_PLAT_ICONS.agents"), "tone": "var(--teal)"}, "the agent's identity colour"
    graph = card(page_ctx, "graphs", '{ id: "g" }')
    assert graph["icon"] == {"path": page_ctx.eval("NV_PLAT_ICONS.graphs"), "tone": "var(--text-3)"}
    assert "icon" not in card(page_ctx, "collections", '{ id: "c" }'), "the page's own icon is the default (the kit's card props)"
    # Platform revamp Plan 03, Task 4: a profile's card draws its kind's icon (a single profile's chip, an aggregate's link), so the profiles page is no
    # longer the example of the default (tests/ui/test_llm_profiles_page.py pins the two paths).
    assert card(page_ctx, "profiles", '{ id: "p" }')["icon"]["tone"] == "var(--text-2)"
    assert card(page_ctx, "profiles", '{ id: "p", kind: "aggregated" }')["icon"]["tone"] == "var(--accent)"


def test_the_list_page_hands_the_console_to_card(page_ctx) -> None:
    page_ctx.eval('var SEEN = []; NV_PLAT_PAGES.probe = { title: "Probes", noun: ["probe", "probes"], list: function () {},'
                  ' card: function (r, con) { SEEN.push(con); return { id: r.id, name: r.id, sub: "", facts: [] }; },'
                  ' open: rec("open") };'
                  ' ITEMS = [{ id: "p1" }]; MR.mount(NV_PlatListPage, { nav: "probe" });')
    assert page_ctx.eval("SEEN.length > 0 && SEEN.every(function (c) { return c === CON; })"), "card(row, con) gets the console"
    assert page_ctx.eval('!!MR.find("pk-card:p1")')


def test_an_actionable_status_on_a_platform_card_runs_its_action_and_does_not_open_it(page_ctx) -> None:
    page_ctx.eval('NV_PLAT_PAGES.probe = { title: "Probes", noun: ["probe", "probes"], list: function () {},'
                  ' card: function (r) { return { id: r.id, name: r.id, sub: "", facts: [],'
                  '   status: { label: "Internal search: off", tone: "warn", onClick: rec("chip"), testid: "probe-chip" } }; },'
                  ' open: rec("open") };'
                  ' ITEMS = [{ id: "p1" }]; MR.mount(NV_PlatListPage, { nav: "probe" });')
    chip = 'MR.find("probe-chip")'
    assert page_ctx.eval(f"{chip}.type") == "button" and page_ctx.eval(f"{chip}.props.type") == "button"
    assert page_ctx.eval(f"{chip}.props.children") == "Internal search: off", "named by its label"
    assert page_ctx.eval(f"{chip}.props.className").split() == ["pk-card-status", "pk-card-status-btn"]
    page_ctx.eval('MR.click("probe-chip")')
    assert [c[0] for c in js(page_ctx, "CALLS")] == ["chip"], "the status ran once and the card did not open"
    page_ctx.eval('MR.click("pk-card-open:p1")')
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
    tree = js(page_ctx, 'MR.subtree("pk-card:p1")')
    assert [e["className"] for e in tree if e["className"] in ("pk-card-badge", "pk-card-status")] == ["pk-card-badge", "pk-card-status"]


def test_the_status_button_sits_above_the_stretched_open() -> None:
    d = declarations(css_body(".pk-card-status-btn"))
    assert d.get("position") == "relative" and d.get("z-index") == "1"


def test_the_agents_card_reads_the_model_profile_and_drops_the_status_chip(page_ctx) -> None:
    """The Agent row's profile lives at model.profile_id (the card's old
    row.model_profile_id never matched) and the list rows carry no
    status, so the chip is dropped instead of fetching one per card
    (lead ruling, open point 2 of Plan 05)."""
    vm = card(page_ctx, "agents", '{ id: "a", name: "builder", model: { profile_id: "p1" } }')
    assert ["profile", "p1"] in [f for f in vm["facts"] if f is not None], "the profile fact reads model.profile_id"
    assert vm.get("status") is None, "no status chip: the list rows carry no status"


def test_a_tool_card_is_labelled_by_its_scoped_id(page_ctx) -> None:
    """Audit G1-21: the tool card is named by its bare tool name, which two toolsets may share; its controls are named by the scoped id."""
    vm = card(page_ctx, "tools", '{ id: "web__search", description: "Search the web" }')
    assert vm["name"] == "search" and vm["label"] == "web__search"
    assert js(page_ctx, 'NV_platCardProps("tools", NV_PLAT_PAGES.tools.card({ id: "web__search" }, CON)).label') == "web__search"
    assert page_ctx.eval('NV_platCardProps("agents", { id: "a1", name: "a1", facts: [] }).label === undefined')

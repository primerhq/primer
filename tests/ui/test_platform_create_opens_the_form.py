"""Platform pages: the page's "New X" opens X's own create form, not a second list of X (ADM-06 of the 2026-10-08 admin review).

Pressing "New toolset" (or "New trigger") on a Platform page used to open the LEGACY list overlay for the entity, a full table with its
own filter, Refresh and "+ New toolset" over the card grid, and only a second press opened the form. Three behaviours existed for the same
button: a form directly (model profiles, workspaces, templates), a form stacked on the legacy table (agents, graphs, approval policies),
and the legacy table first (the rest).

Now toolsets, triggers and services host the entity's EXISTING create dialog (``TS_NewToolsetModal``, ``TR_CreateTriggerDialog``,
``SV_ServiceModal``) on the Platform page itself, the way model profiles already do, and a created row lands on its detail overlay with the card grid refreshed behind it.

The page table (``NV_PLAT_PAGES``) and the hand-off (``NV_createdRow``) are plain JS with no JSX, so they run here in MiniRacer against
the real source. ``EXPECTED_CREATE`` is the whole IA in one place: each later surface moves from the "legacy list" group into
"form on the page" in its own change, and this table is what says so.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
PLAT = (UI / "components" / "console" / "nv-platform.jsx").read_text(encoding="utf-8")
TOOLSETS = (UI / "components" / "toolsets.jsx").read_text(encoding="utf-8")
TRIGGERS = (UI / "components" / "triggers.jsx").read_text(encoding="utf-8")
SERVICES = (UI / "components" / "services.jsx").read_text(encoding="utf-8")

# What each page's create does: ("modal", kind) hosts a form on the page; ("overlay", name, section) opens a management overlay.
EXPECTED_CREATE = {
    # a form hosted on the Platform page
    "profiles": ("modal", "profile"),
    "templates": ("modal", "template"),
    "toolsets": ("modal", "toolset"),
    "triggers": ("modal", "trigger"),
    "services": ("modal", "service"),
    # the entity's own create overlay (workspaces) or its list with the form stacked on top (section "new")
    "workspaces": ("overlay", "new-workspace", None),
    "agents": ("overlay", "agents", "new"),
    "graphs": ("overlay", "graphs", "new"),
    "approvals": ("overlay", "approvals", "new"),
    # still the legacy list first (their own change each)
    "collections": ("overlay", "collections", None),
    "channels": ("overlay", "channels", None),
    "harnesses": ("overlay", "harnesses", None),
}

_OPEN_CONTEXTS: list = []


@pytest.fixture(autouse=True)
def _close_isolates():
    yield
    while _OPEN_CONTEXTS:
        _OPEN_CONTEXTS.pop().close()


def _ctx():
    from py_mini_racer import MiniRacer

    ctx = MiniRacer()
    _OPEN_CONTEXTS.append(ctx)
    ctx.eval("var window = globalThis;")
    ctx.eval(PLAT[PLAT.index("function NV_fact("):PLAT.index("var NV_PLAT_PAGE_SIZE")])
    ctx.eval(
        "var modals = [], overlays = [], refetched = 0;"
        "var con = { openOverlay: function (name, section, id) { overlays.push([name, section === undefined ? null : section, id === undefined ? null : id]); } };"
        "function setModal(m) { modals.push(m); }"
    )
    return ctx


def _js(ctx, expr: str):
    return json.loads(ctx.eval(f"JSON.stringify({expr})"))


def _press_create(ctx, nav: str) -> dict:
    ctx.eval(f"NV_PLAT_PAGES[{json.dumps(nav)}].create(con, setModal);")
    return {"modals": _js(ctx, "modals"), "overlays": _js(ctx, "overlays")}


@pytest.mark.parametrize("nav", sorted(EXPECTED_CREATE))
def test_each_pages_create_does_the_one_thing_the_table_says(nav: str) -> None:
    expected = EXPECTED_CREATE[nav]
    ctx = _ctx()

    pressed = _press_create(ctx, nav)

    if expected[0] == "modal":
        assert [m["kind"] for m in pressed["modals"]] == [expected[1]], pressed
        assert pressed["overlays"] == [], "a hosted form must not also open an overlay"
    else:
        assert pressed["modals"] == [], pressed
        name, section = expected[1], expected[2]
        assert pressed["overlays"] == [[name, section, None]], pressed       # openOverlay(name, section, id): a create has no id


def test_the_table_covers_every_page_that_has_a_new_button() -> None:
    ctx = _ctx()

    with_button = {nav for nav in _js(ctx, "Object.keys(NV_PLAT_PAGES)") if _js(ctx, f"!!NV_PLAT_PAGES[{json.dumps(nav)}].createLabel")}

    assert with_button == set(EXPECTED_CREATE), "a page gained or lost a New button without saying what it opens here"


@pytest.mark.parametrize("nav", ["toolsets", "triggers", "services"])
def test_a_created_row_refreshes_the_cards_and_opens_its_detail(nav: str) -> None:
    ctx = _ctx()
    ctx.eval("function refetch() { refetched++; }")

    ctx.eval(f'NV_createdRow(con, refetch, {json.dumps(nav)}, {{ id: "new-one" }});')

    assert _js(ctx, "refetched") == 1
    assert _js(ctx, "overlays") == [[nav, None, "new-one"]]


def test_a_created_row_without_an_id_only_refreshes_the_cards() -> None:
    ctx = _ctx()
    ctx.eval("function refetch() { refetched++; }")

    ctx.eval('NV_createdRow(con, refetch, "toolsets", {});')
    ctx.eval('NV_createdRow(con, refetch, "toolsets", null);')

    assert _js(ctx, "refetched") == 2
    assert _js(ctx, "overlays") == [], "an overlay for the id 'undefined' is a broken page"


def test_the_platform_page_hosts_the_existing_dialogs() -> None:
    assert "window.TS_NewToolsetModal = TS_NewToolsetModal;" in TOOLSETS, "the toolset dialog must be reachable from the Platform page"
    assert "window.TR_CreateTriggerDialog = TR_CreateTriggerDialog;" in TRIGGERS
    assert "window.SV_ServiceModal = SV_ServiceModal;" in SERVICES
    # The host components render the entity page's own dialog, unchanged.
    assert re.search(r"function NV_ToolsetCreateHost[\s\S]{0,300}window\.TS_NewToolsetModal", PLAT)
    assert re.search(r"function NV_TriggerCreateHost[\s\S]{0,200}window\.TR_CreateTriggerDialog", PLAT)
    assert re.search(r"function NV_ServiceCreateHost[\s\S]{0,300}window\.SV_ServiceModal", PLAT)
    # The page shows the host for its modal kind, closes it on Cancel and on a created row, and hands the row to the tested helper.
    for kind, host, nav in (
        ("toolset", "NV_ToolsetCreateHost", "toolsets"),
        ("trigger", "NV_TriggerCreateHost", "triggers"),
        ("service", "NV_ServiceCreateHost", "services"),
    ):
        shown = re.search(r"modal\.kind === \"" + kind + r"\"[\s\S]{0,600}", PLAT)
        assert shown and f"<{host}" in shown.group(0), kind
        block = shown.group(0)
        assert block.count("setModal(null)") >= 2, f"{kind}: the dialog must close on Cancel and on a created row"
        assert re.search(r"NV_createdRow\(con, [\s\S]{0,60}\"" + nav + r"\", row\)", block), (
            f"{kind}: a created row must go through the tested hand-off, with the page's own nav"
        )

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
    "collections": ("modal", "collection"),
    "channels": ("modal", "channel"),
    "harnesses": ("modal", "harness"),
    "agents": ("modal", "agent"),
    "graphs": ("modal", "graph"),
    "approvals": ("modal", "policy"),
    # the entity's own create overlay (workspaces)
    "workspaces": ("overlay", "new-workspace", None),
    # still the legacy list first (their own change each)
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
    # Each host is bounded at its own closing brace and each mount at its own `) : null}`: a fixed-size window ran from one mount into the next, so the
    # next mount's lines could satisfy this one's assertions (found on the harness mounts in review of #528).
    for host_name, dialog in (
        ("NV_ToolsetCreateHost", "window.TS_NewToolsetModal"),
        ("NV_TriggerCreateHost", "window.TR_CreateTriggerDialog"),
        ("NV_ServiceCreateHost", "window.SV_ServiceModal"),
    ):
        host = re.search(r"function " + host_name + r"\([\s\S]*?\n\}\n", PLAT)
        assert host and dialog in host.group(0), f"{host_name} must render the entity page's own dialog, unchanged"
    # The page shows the host for its modal kind, closes it on Cancel and on a created row, and hands the row to the tested helper.
    for kind, host, nav in (
        ("toolset", "NV_ToolsetCreateHost", "toolsets"),
        ("trigger", "NV_TriggerCreateHost", "triggers"),
        ("service", "NV_ServiceCreateHost", "services"),
    ):
        shown = re.search(r"modal\.kind === \"" + kind + r"\" \? \([\s\S]*?\) : null\}", PLAT)
        assert shown and f"<{host}" in shown.group(0), kind
        block = shown.group(0)
        assert re.search(r"onClose=\{function \(\) \{ setModal\(null\); \}\}", block), f"{kind}: the dialog must close on Cancel"
        assert re.search(
            r"onCreated=\{function \(row\) \{\s*setModal\(null\);\s*NV_createdRow\(con, function \(\) \{ res\.refetch\(\); \}, \"" + nav + r"\", row\);\s*\}\}", block
        ), f"{kind}: a created row must close the dialog and go through the tested hand-off, with the page's own nav"


# ---- harnesses: two hosted forms (register from git, build outbound) ---------------------------------------------------------------

HARNESSES = (UI / "components" / "harnesses.jsx").read_text(encoding="utf-8")
OUTBOUND_BUILDER = (UI / "components" / "harness_outbound_builder.jsx").read_text(encoding="utf-8")


def test_a_created_harness_refreshes_the_cards_and_opens_its_detail() -> None:
    ctx = _ctx()
    ctx.eval("function refetch() { refetched++; }")

    ctx.eval('NV_createdRow(con, refetch, "harnesses", { id: "h-new" });')

    assert _js(ctx, "refetched") == 1
    assert _js(ctx, "overlays") == [["harnesses", None, "h-new"]]


def test_build_outbound_is_the_harnesses_page_second_hosted_form() -> None:
    """The legacy list offered "Register from git" and "Build outbound" side by side; one press of each is one form here too."""
    ctx = _ctx()

    ctx.eval('NV_PLAT_PAGES.harnesses.extraNav.run(con, setModal);')

    assert _js(ctx, "NV_PLAT_PAGES.harnesses.extraNav.label") == "Build outbound"
    assert _js(ctx, "modals") == [{"kind": "harness-outbound"}]
    assert _js(ctx, "overlays") == [], "a hosted form must not also open an overlay"


def test_the_channels_rules_button_still_opens_its_overlay() -> None:
    """The secondary button now receives setModal too; the one page that uses it for an overlay must not change."""
    ctx = _ctx()

    ctx.eval('NV_PLAT_PAGES.channels.extraNav.run(con, setModal);')

    assert _js(ctx, "overlays") == [["channels", "rules", None]]
    assert _js(ctx, "modals") == []


def test_the_page_hands_setmodal_to_its_secondary_button() -> None:
    assert re.search(r"page\.extraNav\.run\(con, setModal\)", PLAT), "the secondary button must be able to host a form"


def test_the_platform_page_hosts_both_harness_dialogs() -> None:
    assert "window.HarnessRegisterDialog = HarnessRegisterDialog;" in HARNESSES
    assert "window.HarnessOutboundBuilder = HarnessOutboundBuilder;" in OUTBOUND_BUILDER
    host = re.search(r"function NV_HarnessCreateHost\([\s\S]*?\n\}\n", PLAT)  # bounded at the function's own closing brace
    assert host, "the page needs a host for the harness dialogs"
    assert "window.HarnessRegisterDialog" in host.group(0) and "window.HarnessOutboundBuilder" in host.group(0)
    for kind in ("harness", "harness-outbound"):
        # Each mount is bounded to ITS OWN block (up to its `) : null}`): a wider window ran from the register mount into the outbound one, so the
        # outbound mount's lines satisfied the register mount's assertions.
        shown = re.search(r"modal\.kind === \"" + kind + r"\" \? \([\s\S]*?\) : null\}", PLAT)
        assert shown and "<NV_HarnessCreateHost" in shown.group(0), kind
        block = shown.group(0)
        # Closing the dialog (Cancel, or the X) refetches the cards: the register wizard creates its DRAFT row at step 1 and a cancelled wizard leaves it
        # behind, invisible until a reload, and a builder that failed after its create leaves one too.
        assert re.search(r"onClose=\{function \(\) \{ setModal\(null\); res\.refetch\(\); \}\}", block), (
            f"{kind}: closing the dialog must close it AND refetch the cards"
        )
        # A created row closes the dialog, then goes through the tested hand-off with the page's own nav.
        assert re.search(
            r"onCreated=\{function \(row\) \{\s*setModal\(null\);\s*NV_createdRow\(con, function \(\) \{ res\.refetch\(\); \}, \"harnesses\", row\);\s*\}\}", block
        ), f"{kind}: a created row must close the dialog and go through NV_createdRow with the page's own nav"


def test_only_the_build_outbound_mount_asks_the_host_for_the_outbound_builder() -> None:
    """Swapping the flag would open the register dialog for "Build outbound" and the builder for "New harness"; only the journeys would notice."""
    mounts = {
        kind: re.search(r"modal\.kind === \"" + kind + r"\" \? \([\s\S]*?\) : null\}", PLAT)
        for kind in ("harness", "harness-outbound")
    }
    assert all(mounts.values()), mounts
    assert "<NV_HarnessCreateHost outbound" in mounts["harness-outbound"].group(0)
    assert "outbound" not in mounts["harness"].group(0).replace("onCreated", "")
    host = re.search(r"function NV_HarnessCreateHost\([\s\S]*?\n\}\n", PLAT).group(0)
    assert re.search(r"props\.outbound\s*\?\s*window\.HarnessOutboundBuilder\s*:\s*window\.HarnessRegisterDialog", host), host

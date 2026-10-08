"""Platform "New agent", "New graph" and "New policy" host their dialog on the page, not on top of the legacy list (ADM-06 of the 2026-10-08 admin review).

These three were the "form stacked on a table" group: the page's create opened the legacy list overlay with ``section = "new"`` so the list's own ``startCreate``
effect opened the dialog on top of it. The operator got a modal with the full legacy table (and its own "New ..." button) visible behind it, two surfaces deep for
one action. The page now hosts the same dialogs (``AG_NewAgentModal``, ``GR_NewGraphModal``, ``AP_NewPolicyModal``) the way toolsets, triggers, services,
collections, channels and harnesses are hosted (``test_platform_create_opens_the_form.py`` tabulates what every page's create does).

Differences between the three, all pinned here:

* agents and graphs report the created row (``onCreate(row)``), so a created row refreshes the cards and opens its own detail overlay (``NV_createdRow``). The agent
  dialog does not confirm itself (the legacy list toasts "Agent created" in its own ``onCreate``), so the agent host does;
* the approval-policy dialog reports a created policy through ``onClose`` alone and passes no row, so there is nothing to open: closing it, for any reason,
  refetches the cards.

The page table and the hand-off are plain JS that runs here in MiniRacer; the hosts and mounts are JSX, so they are source checks (this checkout has no render
harness for them), and the real journeys are ``tests/ui_e2e/test_platform_stacked_forms_journey.py``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
PLAT = (UI / "components" / "console" / "nv-platform.jsx").read_text(encoding="utf-8")
AGENTS = (UI / "components" / "agents.jsx").read_text(encoding="utf-8")
GRAPHS = (UI / "components" / "graphs.jsx").read_text(encoding="utf-8")
APPROVALS = (UI / "components" / "approvals.jsx").read_text(encoding="utf-8")

# nav -> (modal kind, host component, the dialog global the host renders)
SURFACES = {
    "agents": ("agent", "NV_AgentCreateHost", "window.AG_NewAgentModal"),
    "graphs": ("graph", "NV_GraphCreateHost", "window.GR_NewGraphModal"),
    "approvals": ("policy", "NV_PolicyCreateHost", "window.AP_NewPolicyModal"),
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
        "var modals = [], overlays = [];"
        "var con = { openOverlay: function (a, b, c) { overlays.push([a, b === undefined ? null : b, c === undefined ? null : c]); } };"
        "function setModal(m) { modals.push(m); }"
    )
    return ctx


def _js(ctx, expr: str):
    return json.loads(ctx.eval(f"JSON.stringify({expr})"))


@pytest.mark.parametrize("nav", sorted(SURFACES))
def test_the_create_sets_a_modal_and_opens_no_overlay_not_even_the_list_with_a_new_section(nav: str) -> None:
    ctx = _ctx()

    ctx.eval(f"NV_PLAT_PAGES[{json.dumps(nav)}].create(con, setModal);")

    assert _js(ctx, "modals") == [{"kind": SURFACES[nav][0]}]
    assert _js(ctx, "overlays") == [], "a hosted dialog must not open the legacy list with a dialog stacked on it"


def test_the_three_dialogs_are_reachable_from_the_platform_page() -> None:
    assert "window.AG_NewAgentModal = AG_NewAgentModal;" in AGENTS
    assert re.search(r"Object\.assign\(window, \{[^}]*\bGR_NewGraphModal\b[^}]*\}\)", GRAPHS)
    assert "window.AP_NewPolicyModal = AP_NewPolicyModal;" in APPROVALS


@pytest.mark.parametrize("nav", sorted(SURFACES))
def test_each_host_renders_its_dialog_with_the_toast_function(nav: str) -> None:
    _, host_name, dialog = SURFACES[nav]
    host = re.search(r"function " + host_name + r"\([\s\S]*?\n\}\n", PLAT)  # bounded at the function's own closing brace
    assert host, f"the page needs {host_name}"
    assert dialog in host.group(0)
    assert re.search(r"pushToast=\{window\.primerApi\.toastPush\}", host.group(0)), "the dialogs report errors through the pushToast they are given"


def test_the_agent_host_confirms_the_create_itself_and_hands_the_row_on() -> None:
    """The legacy agents list toasts "Agent created" in its own onCreate; the dialog does not, so a host that only handed the row on would be silent."""
    host = re.search(r"function NV_AgentCreateHost\([\s\S]*?\n\}\n", PLAT).group(0)

    assert "Agent created" in host
    assert re.search(r"onCreate=\{function \(row\)", host)
    assert host.index("Agent created") < host.index("props.onCreated(row)"), "the toast comes before the hand-off"


def test_the_graph_host_hands_the_row_on() -> None:
    host = re.search(r"function NV_GraphCreateHost\([\s\S]*?\n\}\n", PLAT).group(0)

    assert "onCreate={props.onCreated}" in host, "the graph dialog reports the created graph through onCreate"
    assert "onClose={props.onClose}" in host


def test_the_policy_host_passes_no_row_because_the_dialog_reports_through_onclose() -> None:
    host = re.search(r"function NV_PolicyCreateHost\([\s\S]*?\n\}\n", PLAT).group(0)

    assert "onClose={props.onClose}" in host
    assert "onCreate" not in host, "AP_NewPolicyModal has no onCreate: a created policy closes the dialog"


@pytest.mark.parametrize("nav", ["agents", "graphs"])
def test_a_created_agent_or_graph_closes_the_dialog_and_opens_its_detail(nav: str) -> None:
    kind, host_name, _ = SURFACES[nav]
    shown = re.search(r"modal\.kind === \"" + kind + r"\" \? \([\s\S]*?\) : null\}", PLAT)  # this mount's own block, up to its `) : null}`
    assert shown and f"<{host_name}" in shown.group(0), kind
    block = shown.group(0)
    assert re.search(r"onClose=\{function \(\) \{ setModal\(null\); \}\}", block), f"{kind}: the dialog must close on Cancel"
    assert re.search(
        r"onCreated=\{function \(row\) \{\s*setModal\(null\);\s*NV_createdRow\(con, function \(\) \{ res\.refetch\(\); \}, \"" + nav + r"\", row\);\s*\}\}", block
    ), f"{kind}: a created row must close the dialog and go through NV_createdRow with the page's own nav"


def test_closing_the_policy_dialog_for_any_reason_refetches_the_cards() -> None:
    """A created policy closes the dialog with no row to report, so the close is the only signal there is to refresh the cards."""
    shown = re.search(r"modal\.kind === \"policy\" \? \([\s\S]*?\) : null\}", PLAT)
    assert shown and "<NV_PolicyCreateHost" in shown.group(0)
    block = shown.group(0)

    assert re.search(r"onClose=\{function \(\) \{ setModal\(null\); res\.refetch\(\); \}\}", block)
    assert "NV_createdRow(" not in block and "openOverlay(" not in block, "no row is reported, so nothing opens"


def test_a_created_row_of_each_kind_refreshes_the_cards_and_opens_its_detail() -> None:
    ctx = _ctx()
    ctx.eval("var refetched = 0; function refetch() { refetched++; }")

    ctx.eval('NV_createdRow(con, refetch, "agents", { id: "a-1" }); NV_createdRow(con, refetch, "graphs", { id: "g-1" });')

    assert _js(ctx, "refetched") == 2
    assert _js(ctx, "overlays") == [["agents", None, "a-1"], ["graphs", None, "g-1"]]

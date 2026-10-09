"""The standing guard for unnamed controls (console review C-003): one browser, one sequential sweep over the console's main surfaces.

A control with no name is an unlabelled edit field or an anonymous button to a screen reader. The label ratchet (``tests/ui/test_field_label_ratchet.py``) counts only labels drawn with the
``field-label`` classes, so it cannot see a control drawn any other way; this test looks at the REAL page. It visits, in one browser and one after the other, the Studio shell, a session
document, the create overlays, every Platform page (and the create form each opens), the graph builder (every kind of step, the palette, the JSON import), and the four phone tabs, and fails
on any visible ``input``, ``select``, ``textarea`` or button with no accessible name that is not on ``ALLOWLIST``.

A name is, in this order: ``aria-labelledby``, ``aria-label``, a ``<label>`` pointing at the control; a button (or an element with a button-like role) may also be named by its text, its
``title``, an image's ``alt`` or an SVG ``<title>``. A ``placeholder`` is NOT a name: it vanishes when the user types and is not read as the field's name by every screen reader.

``ALLOWLIST`` is small on purpose and every entry carries a reason (a ticket, or why the control cannot be named). An entry that matches nothing in a run FAILS the test, so the list can
only shrink: when a control is fixed, its entry has to go.
"""

from __future__ import annotations

import json
import re
import uuid

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._support.smk import smk
from tests.ui_e2e import _delegation_seed as seed
from tests.ui_e2e import _graph_builder_helpers as gb
from tests.ui_e2e._a11y import ALLOWLIST, UNNAMED_JS, classify
from tests.ui_e2e._shell_helpers import open_legacy_route, open_overlay, open_shell
from tests.ui_e2e._studio_helpers import open_session_in_studio
from tests.ui_e2e.test_delegated_run_nests_in_its_call_journey import _seed_session

pytestmark = smk("SMK-UI-06", status="partial")

_PLATFORM_ROUTES = [
    "agents", "graphs", "triggers", "toolsets", "approvals", "workers", "harnesses", "services", "workspaces", "workspaces/templates", "workspaces/providers",
    "channels", "channels/rules", "channels/providers", "knowledge/collections", "subsystems/internal-collections",
    "providers/llm", "providers/embedding", "providers/cross_encoder", "providers/stt", "providers/tts", "providers/web_search", "providers/web_fetch", "providers/artifact_storage",
]
PHONE = {"width": 390, "height": 844}
_OVERLAY = '[data-testid^="nv-overlay:"]'
OWN_FAILURE = "the session's own turn: the model fell over"


class _Sweep:
    def __init__(self, page: Page) -> None:
        self.page = page
        self.found: dict[str, list[str]] = {}
        self.visited: list[str] = []

    def at(self, surface: str, root: str | None = None) -> None:
        """Record the unnamed controls under ``root`` (a CSS selector; the whole page when none) as the page is now, under ``surface``."""
        self.visited.append(surface)
        self.page.wait_for_timeout(250)  # let a just-opened surface finish painting
        for item in self.page.evaluate(UNNAMED_JS, root):
            self.found.setdefault(item["html"], []).append(surface)


def _seed_graph(base_url: str, graph_id: str, agent_id: str) -> None:
    nodes = [
        {"kind": "begin", "id": "begin", "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}}},
        {"kind": "agent", "id": "decider", "agent_id": agent_id, "description": "Decide"},
        {"kind": "tool_call", "id": "tool", "tool_id": "workspaces__list_files", "arguments": {"path": "."}},
        {"kind": "fan_out", "id": "split", "specs": [{"kind": "broadcast", "target_node_id": "worker", "count": 3, "on_failure": "fail_fast"}]},
        {"kind": "agent", "id": "worker", "agent_id": agent_id, "description": "Work"},
        {"kind": "fan_in", "id": "merge", "aggregate_template": "{{ results }}"},
        {"kind": "end", "id": "end", "output_template": ""},
    ]
    edges = [
        {"kind": "static", "from_node": "begin", "to_node": "decider"},
        {"kind": "conditional", "from_node": "decider", "router": {"kind": "json_path", "branches": [
            {"conditions": [{"path": "done", "op": "eq", "value": True}], "to_node": "tool"}], "default_to": "end"}},
        {"kind": "static", "from_node": "tool", "to_node": "split"},
        {"kind": "static", "from_node": "worker", "to_node": "merge"},
        {"kind": "static", "from_node": "merge", "to_node": "end"},
    ]
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/graphs", json={"id": graph_id, "description": "controls sweep probe", "max_iterations": 5, "nodes": nodes, "edges": edges})
        assert r.status_code == 201, r.text


def _session_log(tmp_path, wid: str, sid: str) -> None:
    """A transcript with a user instruction, a delegated run that failed and the session's own failure (so its Retry and trace buttons are on the page)."""
    seeded = seed.build(failures=True)
    records = [r for r in seeded.records if r["seq"] <= seeded.delegated_failure_seq]
    records.append({"seq": records[-1]["seq"] + 1, "kind": "error", "created_at": "2026-10-06T12:00:06Z", "node_id": None,
                    "payload": {"message": OWN_FAILURE, "code": "server_error", "fatal": True}})
    log = tmp_path / wid / ".state" / "sessions" / sid / "messages.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def _open_create_form(page: Page, sweep: _Sweep, surface: str) -> None:
    """Click the page's own ``New ...`` button, when it has one, sweep the form it opens and close it."""
    new = page.locator(_OVERLAY).get_by_role("button", name=re.compile(r"^(\+ )?New\b")).first
    if new.count() == 0 or not new.is_enabled():
        return
    new.click()
    page.wait_for_timeout(400)
    sweep.at(f"{surface} / create form", ".modal")
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)


@pytest.mark.ui_e2e
@pytest.mark.timeout(600)
def test_no_visible_control_of_the_consoles_main_surfaces_is_without_a_name(base_url: str, console_url: str, page: Page, tmp_path) -> None:
    suffix = uuid.uuid4().hex[:8]
    wid, sid = _seed_session(base_url, tmp_path, suffix)
    agent_id = f"dn-agent-{suffix}"
    graph_id = f"sweep-graph-{suffix}"
    _seed_graph(base_url, graph_id, agent_id)
    _session_log(tmp_path, wid, sid)
    sweep = _Sweep(page)
    try:
        # the Studio shell, a session document, the create overlays
        open_shell(page, console_url, wid)
        sweep.at("studio")
        open_session_in_studio(page, console_url, wid, sid, kind="agent")
        expect(page.locator(".nv-turn-error").first).to_be_visible(timeout=20_000)
        sweep.at("session document")
        for name in ("new-session", "new-workspace", "activity"):
            open_overlay(page, console_url, wid, name)
            sweep.at(f"overlay {name}", f'[data-testid="nv-overlay:{name}"]')

        # every Platform page, and the create form each opens
        for route in _PLATFORM_ROUTES:
            open_legacy_route(page, console_url, route)
            sweep.at(f"platform {route}", _OVERLAY)
            _open_create_form(page, sweep, f"platform {route}")

        # the graph builder: every kind of step, the palette, the JSON import
        open_legacy_route(page, console_url, f"graphs/{graph_id}")
        gb.wait_for_builder(page)
        sweep.at("graph builder", gb.BUILDER)
        rows = page.locator('[data-testid="gb-outline-row"]')
        for i in range(rows.count()):
            rows.nth(i).click()
            sweep.at(f"graph builder / step {i + 1}", gb.BUILDER)
        page.locator('[data-testid="gb-json-tab"]').click()
        expect(page.locator(".modal").first).to_be_visible(timeout=5_000)
        sweep.at("graph builder / JSON import", ".modal")
        page.locator(".modal").get_by_role("button", name="Cancel", exact=True).click()
        page.locator(gb.OUTLINE_ADD).first.click()
        expect(page.locator(gb.PALETTE)).to_be_visible(timeout=5_000)
        sweep.at("graph builder / palette", gb.BUILDER)

        # the phone: its four tabs
        # (the open session document would fill the phone screen, so the stored tabs are cleared and the console is loaded afresh at its root)
        page.set_viewport_size(PHONE)
        page.evaluate("() => { localStorage.clear(); sessionStorage.clear(); }")
        page.goto(console_url)
        expect(page.get_by_test_id("nv-mobile-shell")).to_be_visible(timeout=20_000)
        for tab in ("Inbox", "Spaces", "Files", "More"):
            page.get_by_role("tab", name=tab).click()
            sweep.at(f"phone / {tab}", '[data-testid="nv-mobile-shell"]')
    finally:
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            c.delete(f"/v1/graphs/{graph_id}")

    unnamed, stale = classify(sweep.found, ALLOWLIST)
    report = "\n".join(f"  {', '.join(surfaces)}\n      {html}" for html, surfaces in unnamed.items())
    assert not unnamed, f"{len(unnamed)} control(s) with no name, by surface (swept: {len(sweep.visited)} pages):\n{report}"
    assert not stale, f"allowlist entries that match nothing in this run (remove them): {stale}"


# ---------------------------------------------------------------------------
# the definition itself, on a page whose answer is known: the sweep above can only pass if this can fail
# ---------------------------------------------------------------------------

_NAMED = """
<label for="n1">Name</label><input id="n1" data-testid="named-label-for">
<label>Wrapped <input data-testid="named-label-wrapping"></label>
<input aria-label="Search" data-testid="named-aria-label">
<span id="n2">Described by</span><input aria-labelledby="n2" data-testid="named-labelledby">
<select aria-label="Pick" data-testid="named-select"><option>a</option></select>
<textarea aria-label="Notes" data-testid="named-textarea"></textarea>
<button data-testid="named-text">Save</button>
<button aria-label="Close" data-testid="named-button-aria"><svg width="8" height="8"></svg></button>
<button title="Refresh" data-testid="named-button-title"><svg width="8" height="8"></svg></button>
<button data-testid="named-button-svg-title"><svg width="8" height="8"><title>Settings</title></svg></button>
<button data-testid="named-button-img-alt"><img alt="Logo" width="8" height="8" src="data:image/gif;base64,R0lGODlhAQABAAAAACw="></button>
<div role="button" data-testid="named-role-button">Open</div>
<label>Agree <input type="checkbox" data-testid="named-checkbox"></label>
"""
_UNNAMED = """
<input placeholder="Filter things" data-testid="unnamed-placeholder-only">
<select data-testid="unnamed-select"><option value="">all kinds</option></select>
<textarea placeholder="Say something" data-testid="unnamed-textarea"></textarea>
<button data-testid="unnamed-icon-button"><svg width="8" height="8"></svg></button>
<label for="u1"></label><input id="u1" data-testid="unnamed-empty-label">
<button aria-label="" data-testid="unnamed-empty-aria-label"></button>
<div role="textbox" style="min-height: 12px" data-testid="unnamed-role-textbox"></div>
<input type="checkbox" data-testid="unnamed-checkbox">
"""
_NOT_ON_SCREEN = """
<input type="hidden" data-testid="skipped-hidden-input">
<input placeholder="x" style="display:none" data-testid="skipped-display-none">
<input placeholder="x" style="visibility:hidden" data-testid="skipped-visibility-hidden">
<div aria-hidden="true"><input placeholder="x" data-testid="skipped-aria-hidden"></div>
"""


@pytest.mark.ui_e2e
def test_the_definition_of_unnamed_flags_exactly_the_unnamed_visible_controls(page: Page) -> None:
    page.set_content(f"<main>{_NAMED}{_UNNAMED}{_NOT_ON_SCREEN}</main>")
    flagged = {item["testid"] for item in page.evaluate(UNNAMED_JS, "main")}
    expected = set(re.findall(r'data-testid="(unnamed-[a-z-]+)"', _UNNAMED))
    assert flagged == expected, (sorted(flagged - expected), sorted(expected - flagged))

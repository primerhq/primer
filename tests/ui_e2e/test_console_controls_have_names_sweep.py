"""The standing guard for unnamed controls (console review C-003): one browser, one sequential sweep over the console's main surfaces.

A control with no name is an unlabelled edit field or an anonymous button to a screen reader. The label ratchet (``tests/ui/test_field_label_ratchet.py``) counts only labels drawn with the
``field-label`` classes, so it cannot see a control drawn any other way; this test looks at the REAL page, and the NAME comes from Chromium's accessibility tree (``tests/ui_e2e/_a11y.py``:
the page only enumerates the candidate controls). It visits, in one browser and one after the other (``tests/ui_e2e/_a11y_surfaces.py`` is the list):

* the Studio shell, a session document with a failed turn (Retry and trace on the page), and the New session, New workspace and Activity overlays;
* every Platform page as an overlay (the legacy routes, section by section) AND as a VIEW (``view=platform:<id>``, what every Platform nav row opens), and every System view (``view=system:<id>``);
* for each of them, every create form it has (an explicit table of the button that opens it, and for a provider the menu item picked after it; a form that does not open FAILS, it is not skipped);
* the graph builder on a seeded graph with a step of each of its kinds, the JSON import and the palette;
* the four phone tabs.

and fails on any control with no accessible name that is not on ``ALLOWLIST``, on a visit that did not happen, on a page error, and on an allowlist entry that excused nothing. A surface that passes
by being empty fails too: each is looked at only after its OWN marker is on screen (``nv-plat-page:<id>``, ``nv-sys-page:<id>``, the overlay's title and its provider body, ``nv-mobile-panel:<tab>``,
the inspector showing the clicked step, the form's first field) and nothing under it is still loading, a loading or error banner under it is a failure, and the BODY of every surface (not its fixed
chrome) holds at least the floor set for it. The numbers are printed on every run. NOT covered (listed in ``docs/dev/subsystems/ui-pages.md``): detail pages, edit forms, the confirm host, menus
(the provider register menu is only opened to pick from it), the command palette, the Files sidebar and terminal, the phone's drill-downs and sheets, 422 states, the row actions of a populated
list, the Platform and System nav rows, and what Chromium cannot be asked about: controls inside iframes and shadow roots, and a ``div``, ``span`` or ``<a>`` without ``href`` that has only a click
handler, a ``[tabindex]`` element without a role, or a dialog's own name.

``ALLOWLIST`` is empty on purpose and may only shrink: an entry that matches nothing in a run FAILS the test.
"""

from __future__ import annotations

import json
import re
import time
import uuid

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._support.smk import smk
from tests.ui_e2e import _delegation_seed as seed
from tests.ui_e2e import _graph_builder_helpers as gb
from tests.ui_e2e._a11y import ALLOWLIST, AxProbe, Look, counts_table, evaluate_sweep, page_state
from tests.ui_e2e._a11y_surfaces import (
    CHROME,
    DEAD_MENU_TEXT,
    DEAD_REGISTER_MENUS,
    FLOORS,
    LEGACY_FORMS,
    MODAL,
    OVERLAY,
    OVERLAYS,
    PHONE_TABS,
    PLATFORM,
    PLATFORM_FORMS,
    SYSTEM,
    SYSTEM_FORMS,
    SYSTEM_VIEWS,
    Form,
    expected_surfaces,
    form_surface,
    ready_selectors,
)
from tests.ui_e2e._session_seed import delete_paths, delete_seeded, seed_session
from tests.ui_e2e._shell_helpers import open_legacy_route, open_overlay, open_shell, open_view
from tests.ui_e2e._studio_helpers import open_session_in_studio

pytestmark = smk("SMK-UI-06", status="partial")

PHONE = {"width": 390, "height": 844}
OWN_FAILURE = "the session's own turn: the model fell over"
READY_TIMEOUT = 15_000      # ms a surface has to show its own marker
LOADED_TIMEOUT = 10         # s a surface has to stop saying it is loading
FIRST_FIELD = ":is(input:not([type=hidden]), select, textarea, [contenteditable=true])"


class _Sweep:
    def __init__(self, page: Page) -> None:
        self.page = page
        self.probe = AxProbe(page)
        self.found: dict[str, list[str]] = {}
        self.visited: list[str] = []
        self.looks: dict[str, Look] = {}
        self.notes: list[str] = []

    def arrive(self, kind: str, name: str) -> None:
        """Wait for the page itself (not the one that was on screen before it, not the first row of a nav that a view falls back to) and for its list or body to have loaded."""
        for marker in ready_selectors(kind, name):
            expect(self.page.locator(marker).first).to_be_visible(timeout=READY_TIMEOUT)

    def at(self, surface: str, root: str | None = None, *, ready: list[str] | None = None, check_page: bool = True) -> None:
        """Record the unnamed controls under ``root`` (a CSS selector that must match a visible element; the whole page when none) as the page is now, under ``surface``.

        Before it looks, the surface must show every marker of ``ready`` and, with ``check_page``, stop saying it is loading; an error banner under it, or a marker that never came, is a note
        in the report (the look is still taken, so one broken page does not hide the rest)."""
        self.visited.append(surface)
        for marker in ready or []:
            try:
                expect(self.page.locator(marker).first).to_be_visible(timeout=READY_TIMEOUT)
            except AssertionError:
                self.notes.append(f"{surface}: never showed {marker}")
        state = page_state(self.page, root)
        if check_page:
            deadline = time.monotonic() + LOADED_TIMEOUT
            while state.loading and time.monotonic() < deadline:
                self.page.wait_for_timeout(250)
                state = page_state(self.page, root)
            if state.loading:
                self.notes.append(f"{surface}: still loading after {LOADED_TIMEOUT} s: {state.loading}")
            if state.errors:
                self.notes.append(f"{surface}: an error banner under it: {state.errors}")
        self.page.wait_for_timeout(250)  # let a just-opened surface finish painting
        result = self.probe.examine(root, chrome=CHROME)
        self.looks[surface] = Look(examined=result.examined, body=result.body, skipped=result.skipped)
        for item in result.unnamed:
            self.found.setdefault(item["html"], []).append(surface)


def _seed_graph(base_url: str, graph_id: str, agent_id: str) -> int:
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
    return len(nodes)


def _session_log(tmp_path, wid: str, sid: str) -> None:
    """A transcript with a user instruction, a delegated run that failed and the session's own failure (so its Retry and trace buttons are on the page)."""
    seeded = seed.build(failures=True)
    records = [r for r in seeded.records if r["seq"] <= seeded.delegated_failure_seq]
    records.append({"seq": records[-1]["seq"] + 1, "kind": "error", "created_at": "2026-10-06T12:00:06Z", "node_id": None,
                    "payload": {"message": OWN_FAILURE, "code": "server_error", "fatal": True}})
    log = tmp_path / wid / ".state" / "sessions" / sid / "messages.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def _sweep_forms(page: Page, sweep: _Sweep, kind: str, name: str, forms: list[Form], reopen, scope: str) -> None:
    """For each ``Form`` of a page: open the page afresh and wait for IT, press the button (it must be there and enabled), pick the menu item that comes after it (a provider's kind), wait for what it
    opens (its root must NOT have been on screen before), sweep it, close it."""
    for form in forms:
        reopen()
        sweep.arrive(kind, name)
        new = page.locator(scope).get_by_role("button", name=re.compile(rf"^(\+ )?{re.escape(form.button)}\b")).first
        expect(new).to_be_visible(timeout=15_000)
        expect(new).to_be_enabled(timeout=5_000)
        expect(page.locator(form.root)).to_have_count(0, timeout=5_000)   # a form root that is already there would be the page recorded as the form
        new.click()
        if form.then:
            item = page.locator(f"{scope} {form.then}").first
            expect(item).to_be_visible(timeout=15_000)
            item.click()
        expect(page.locator(form.root).first).to_be_visible(timeout=10_000)
        sweep.at(form_surface(kind, name, form.button), form.root, ready=[f"{form.root} {FIRST_FIELD}:visible"])
        page.keyboard.press("Escape")
        if form.root == MODAL:
            expect(page.locator(MODAL)).to_have_count(0, timeout=5_000)


def _assert_register_menu_is_dead(page: Page, reopen, route: str) -> None:
    """The page is excused from a form because its register menu lists no kinds (ticket 01a1214c): say so again here, so that fixing the page fails the sweep until it is swept through its form."""
    reopen()
    page.locator(OVERLAY).get_by_role("button", name=re.compile(rf"^{re.escape(DEAD_REGISTER_MENUS[route])}\b")).first.click()
    panel = page.locator(f'{OVERLAY} [data-testid="provider-register-panel"]')
    try:
        expect(panel).to_contain_text(DEAD_MENU_TEXT, timeout=15_000)
    except AssertionError as exc:
        raise AssertionError(f"{route}: its register menu no longer says {DEAD_MENU_TEXT!r}: move the page back into LEGACY_FORMS with its menu item and drop it from DEAD_REGISTER_MENUS and NO_FORM") from exc


@pytest.mark.ui_e2e
@pytest.mark.timeout(900)
def test_no_visible_control_of_the_consoles_main_surfaces_is_without_a_name(base_url: str, console_url: str, page: Page, tmp_path) -> None:
    suffix = uuid.uuid4().hex[:8]
    agent_id = f"dn-agent-{suffix}"
    graph_id = f"sweep-graph-{suffix}"
    provider_id = f"sweep-cp-{suffix}"
    page_errors: list[str] = []
    page.on("pageerror", lambda exc: page_errors.append(str(exc)))
    sweep = _Sweep(page)
    died: Exception | None = None
    steps = 0
    seeded = None
    made: list[str] = []
    try:
        seeded = seed_session(base_url, tmp_path, suffix, description="controls sweep probe")
        wid, sid = seeded.wid, seeded.sid
        steps = _seed_graph(base_url, graph_id, agent_id)
        made.append(f"/v1/graphs/{graph_id}")
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            made.append(f"/v1/channel_providers/{provider_id}")
            r = c.post("/v1/channel_providers", json={"id": provider_id, "provider": "discord", "config": {"bot_token": "x" * 60}})   # New channel and the rules toolbar need one
            assert r.status_code in (200, 201), r.text
        _session_log(tmp_path, wid, sid)

        # the Studio shell, a session document, the create overlays
        open_shell(page, console_url, wid)
        sweep.at("studio", ready=['[data-testid="nv-root"]'])
        open_session_in_studio(page, console_url, wid, sid, kind="agent")
        expect(page.locator(".nv-turn-error").first).to_be_visible(timeout=20_000)
        sweep.at("session document", ready=[".nv-turn-error"], check_page=False)   # its failed turn is the point of the page
        for name in OVERLAYS:
            open_overlay(page, console_url, wid, name)
            sweep.at(f"overlay {name}", f'[data-testid="nv-overlay:{name}"]', ready=[f'[data-testid="nv-overlay:{name}"] [data-testid="nv-overlay-body"]'])

        # every Platform page as an overlay (its legacy route), and the forms it opens
        for route, forms in LEGACY_FORMS.items():
            def reopen(route=route):
                open_legacy_route(page, console_url, route)
            reopen()
            sweep.arrive("overlay-page", route)
            sweep.at(f"overlay-page {route}", OVERLAY, ready=ready_selectors("overlay-page", route))
            _sweep_forms(page, sweep, "overlay-page", route, forms, reopen, OVERLAY)
            if route in DEAD_REGISTER_MENUS:
                _assert_register_menu_is_dead(page, reopen, route)

        # every Platform VIEW (what every nav row of the Platform view opens), and the forms it opens
        for nav, forms in PLATFORM_FORMS.items():
            def reopen(nav=nav):
                open_view(page, console_url, wid, f"platform:{nav}")
            reopen()
            sweep.arrive("platform-view", nav)
            sweep.at(f"platform-view {nav}", PLATFORM, ready=ready_selectors("platform-view", nav))
            _sweep_forms(page, sweep, "platform-view", nav, forms, reopen, PLATFORM)

        # every System view, and the forms it opens
        for nav in SYSTEM_VIEWS:
            def reopen(nav=nav):
                open_view(page, console_url, wid, f"system:{nav}")
            reopen()
            sweep.arrive("system-view", nav)
            sweep.at(f"system-view {nav}", SYSTEM, ready=ready_selectors("system-view", nav))
            _sweep_forms(page, sweep, "system-view", nav, SYSTEM_FORMS.get(nav, []), reopen, SYSTEM)

        # the graph builder: a step of each kind, the JSON import, the palette
        open_legacy_route(page, console_url, f"graphs/{graph_id}")
        gb.wait_for_builder(page)
        rows = page.locator('[data-testid="gb-outline-row"]')
        expect(rows).to_have_count(steps, timeout=15_000)
        sweep.at("graph builder", gb.BUILDER, ready=[gb.OUTLINE, gb.CANVAS])
        for i in range(steps):
            node_id = rows.nth(i).get_attribute("data-node-id")
            rows.nth(i).click()
            expect(page.locator(gb.INSPECTOR)).to_contain_text(node_id or "", timeout=10_000)   # the inspector shows the step that was clicked
            sweep.at(f"graph builder / step {i + 1}", gb.BUILDER, ready=[gb.INSPECTOR])
        page.locator('[data-testid="gb-json-tab"]').click()
        expect(page.locator(MODAL).first).to_be_visible(timeout=5_000)
        sweep.at("graph builder / JSON import", MODAL, ready=[f"{MODAL} {FIRST_FIELD}:visible"])
        page.locator(MODAL).get_by_role("button", name="Cancel", exact=True).click()
        page.locator(gb.OUTLINE_ADD).first.click()
        expect(page.locator(gb.PALETTE)).to_be_visible(timeout=5_000)
        sweep.at("graph builder / palette", gb.BUILDER, ready=[gb.PALETTE])

        # the phone: its four tabs
        # (the open session document would fill the phone screen, so the stored tabs are cleared and the console is loaded afresh at its root)
        page.set_viewport_size(PHONE)
        page.evaluate("() => { localStorage.clear(); sessionStorage.clear(); }")
        page.goto(console_url)
        expect(page.get_by_test_id("nv-mobile-shell")).to_be_visible(timeout=20_000)
        for tab in PHONE_TABS:
            page.get_by_role("tab", name=tab).click()
            sweep.arrive("phone", tab)
            sweep.at(f"phone / {tab}", '[data-testid="nv-mobile-shell"]', ready=ready_selectors("phone", tab))
    except Exception as exc:  # noqa: BLE001 - reported below with everything found before it died
        died = exc
    finally:
        try:
            sweep.probe.close()
        except Exception as exc:  # noqa: BLE001 - the findings below matter more than a detached session
            sweep.notes.append(f"the CDP session could not be closed: {exc!r}")
        left = delete_paths(base_url, made) + (delete_seeded(base_url, seeded) if seeded else [])

    # Everything is judged AFTER the sweep, by one pure function, so that a sweep that died part-way still reports what it had found, and every kind of failure is reported together.
    counts = counts_table(sweep.looks, FLOORS)
    print("\n" + counts)   # on a pass it shows with -rP; on a failure it is in the message, where the floors are read from
    problems = evaluate_sweep(found=sweep.found, allowlist=ALLOWLIST, visited=sweep.visited, expected=expected_surfaces(steps), looks=sweep.looks, floors=FLOORS, notes=sweep.notes,
                              page_errors=page_errors, left=left, completed=died is None)
    where = f"swept {len(sweep.visited)} surface(s), the last one {sweep.visited[-1]!r}" if sweep.visited else "swept nothing"
    if died is not None:
        raise AssertionError(f"the sweep died ({where}): {died!r}\n" + "\n\n".join(problems) + f"\n\n{counts}") from died
    assert not problems, f"({where})\n" + "\n\n".join(problems) + f"\n\n{counts}"

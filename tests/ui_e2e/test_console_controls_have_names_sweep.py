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
by being empty or broken fails too, in three layers (``tests/ui_e2e/_a11y_sweep.py``). Each is looked at only after its OWN marker is on screen (``nv-plat-page:<id>`` and a loaded list,
``nv-sys-page:<id>``, the overlay's section title and, for a provider class, its loaded cards or its empty state, ``nv-mobile-panel:<tab>``, the step's id in the inspector, the form's first field).
Then the NETWORK: every ``/v1`` response of 500 or more and every request that failed (not one the console aborts, not its event streams) since the previous look is a note for the surface, and a
look waits until the ``/v1`` requests in flight have answered, so a form whose fields come from a request is looked at after they arrived. Last, the SELECTORS, the second line: what is still
loading (a spinner, ``aria-busy``, text that starts Loading, Checking or Reading in any case) and the banners the console draws (``.nv-form-error``, ``.banner-error``, ``.nv-doc-problem``,
``[role=alert]``, ``.sh-file-conflict``, ``.field-help.warn``, a red ``.nv-bind-empty``); a red span in a table row, a stuck title and an empty list that is really a failure have no shape a selector
can know and are left to the network. The BODY of every surface (not its fixed chrome) holds at least its floor, and the numbers are printed on every run. Every wait is bounded and the bound is
shared (``Budget``): once enough looks have used their wait up, the rest wait briefly and are noted, and the sweep has a wall-clock deadline of its own (600 s from the start of the test, seeding included: past it no wait is handed out, the next one raises an ordinary exception, so the findings are reported and the seeds deleted with a live browser); pytest-timeout stays only as the thread-method last resort above it (a signal timeout hangs Playwright's sync API). A broken install is reported, not waited out.
Only the FIRST kind of a provider class is swept (the first item of its register menu). NOT covered (listed in ``docs/dev/subsystems/ui-pages.md``): detail pages, edit forms, the confirm host,
menus (the provider register menu is only opened to pick from it), the command palette, the Files sidebar and terminal, the phone's drill-downs and sheets, 422 states, the row actions of a
populated list, the Platform and System nav rows, the setup wizard, the controls inside a closed ``<details>``, and what Chromium cannot be asked about: controls inside iframes and shadow
roots, and a ``div``, ``span`` or ``<a>`` without ``href`` that has only a click handler, a ``[tabindex]`` element without a role, or a dialog's own name.

``ALLOWLIST`` is empty on purpose and may only shrink: an entry that matches nothing in a run FAILS the test.
"""

from __future__ import annotations

import json
import re
import uuid

import httpx
import pytest
from playwright.sync_api import Error as BrowserError
from playwright.sync_api import Page, expect

from tests._support.smk import smk
from tests.ui_e2e import _delegation_seed as seed
from tests.ui_e2e import _graph_builder_helpers as gb
from tests.ui_e2e._a11y import ALLOWLIST, Budget, counts_table, evaluate_sweep
from tests.ui_e2e._a11y_surfaces import (
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
)
from tests.ui_e2e._a11y_sweep import Sweep
from tests.ui_e2e._session_seed import delete_paths, delete_seeded, seed_session
from tests.ui_e2e._shell_helpers import open_legacy_route, open_overlay, open_shell, open_view
from tests.ui_e2e._studio_helpers import open_session_in_studio

pytestmark = smk("SMK-UI-06", status="partial")

PHONE = {"width": 390, "height": 844}
OWN_FAILURE = "the session's own turn: the model fell over"
FIRST_FIELD = ":is(input:not([type=hidden]), select, textarea, [contenteditable=true])"


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


def _sweep_forms(page: Page, sweep: Sweep, kind: str, name: str, forms: list[Form], reopen, scope: str) -> None:
    """For each ``Form`` of a page: go to the page (``reopen``: the hash is assigned again, so when the console is already there nothing reloads) and wait for IT, press the button (it must be there and
    enabled), pick the menu item that comes after it (a provider's kind), wait for what it opens (its root must NOT have been on screen before), sweep it, close it. A form that cannot be opened is
    a note and an empty look (the floor reports it too), and the next form is tried."""
    for form in forms:
        surface = form_surface(kind, name, form.button)
        if not sweep.reach(surface, reopen):
            continue
        if not sweep.arrive(kind, name):
            sweep.skip(surface, "its page never showed")
            continue
        try:
            new = page.locator(scope).get_by_role("button", name=re.compile(rf"^(\+ )?{re.escape(form.button)}\b")).first
            expect(new).to_be_visible(timeout=sweep.budget.wait_ms(15_000))
            expect(new).to_be_enabled(timeout=sweep.budget.wait_ms(5_000))
            expect(page.locator(form.root)).to_have_count(0, timeout=sweep.budget.wait_ms(5_000))   # a form root that is already there would be the page recorded as the form
            new.click(timeout=sweep.budget.wait_ms(10_000))
            if form.then:
                item = page.locator(f"{scope} {form.then}").first
                expect(item).to_be_visible(timeout=sweep.budget.wait_ms(15_000))
                item.click(timeout=sweep.budget.wait_ms(10_000))
            expect(page.locator(form.root).first).to_be_visible(timeout=sweep.budget.wait_ms(10_000))
        except (AssertionError, BrowserError) as exc:
            sweep.budget.spent()
            sweep.skip(surface, f"the form did not open ({type(exc).__name__})")
            continue
        sweep.at(surface, form.root, ready=[f"{form.root} {FIRST_FIELD}:visible"])
        page.keyboard.press("Escape")
        if form.root == MODAL:
            try:
                expect(page.locator(MODAL)).to_have_count(0, timeout=sweep.budget.wait_ms(5_000))
            except AssertionError:
                sweep.budget.spent()
                sweep.notes.append(f"{surface}: Escape did not close its modal")


def _sweep_page(page: Page, sweep: Sweep, kind: str, name: str, root: str, forms: list[Form], reopen, scope: str) -> None:
    """Go to a page, wait for ITS marker, look at it, and sweep the forms it has. A page that never shows is one note and an empty look for it and for each of its forms (the waits are not repeated)."""
    if not sweep.reach(f"{kind} {name}", reopen):
        for form in forms:
            sweep.skip(form_surface(kind, name, form.button), "its page could not be opened")
        return
    if not sweep.arrive(kind, name):
        sweep.skip(f"{kind} {name}", "its page never showed")
        for form in forms:
            sweep.skip(form_surface(kind, name, form.button), "its page never showed")
        return
    sweep.at(f"{kind} {name}", root)
    _sweep_forms(page, sweep, kind, name, forms, reopen, scope)


def _assert_register_menu_is_dead(page: Page, reopen, route: str, sweep: Sweep) -> None:
    """The page is excused from a form because its register menu lists no kinds (ticket 01a1214c): say so again here, so that fixing the page fails the sweep until it is swept through its form. The
    failure is a note (the sweep goes on to the other surfaces), and the test fails on it at the end."""
    try:
        reopen()
        page.locator(OVERLAY).get_by_role("button", name=re.compile(rf"^{re.escape(DEAD_REGISTER_MENUS[route])}\b")).first.click(timeout=sweep.budget.wait_ms(10_000))
        panel = page.locator(f'{OVERLAY} [data-testid="provider-register-panel"]')
        expect(panel).to_contain_text(DEAD_MENU_TEXT, timeout=sweep.budget.wait_ms(15_000))
    except (AssertionError, BrowserError) as exc:
        sweep.budget.spent()
        sweep.notes.append(f"{route}: its register menu does not say {DEAD_MENU_TEXT!r} ({type(exc).__name__}): move the page back into LEGACY_FORMS with its menu item and drop it from DEAD_REGISTER_MENUS and NO_FORM")
        page.keyboard.press("Escape")    # the menu may be open: the next surface starts from a page without one
        return
    page.keyboard.press("Escape")    # the menu is open: the next surface starts from a page without one


@pytest.mark.ui_e2e
@pytest.mark.timeout(900, method="thread")     # the last resort only: it kills the whole lane (no other journey reported, no seed deleted). The sweep stops itself, from inside, after 600 s (Budget deadline_s): the next surface raises SweepDeadlineExceeded and the test reports what it found and deletes its seeds
def test_no_visible_control_of_the_consoles_main_surfaces_is_without_a_name(base_url: str, console_url: str, page: Page, tmp_path) -> None:
    suffix = uuid.uuid4().hex[:8]
    agent_id = f"dn-agent-{suffix}"
    graph_id = f"sweep-graph-{suffix}"
    provider_id = f"sweep-cp-{suffix}"
    ssp_id = f"sweep-ssp-{suffix}"
    page_errors: list[str] = []
    page.on("pageerror", lambda exc: page_errors.append(str(exc)))
    sweep = Sweep(page, budget=Budget(deadline_s=600))
    died: Exception | None = None
    steps = 0
    seeded = None
    made: list[str] = []
    try:
        sweep.budget.check("seeding")    # the deadline is counted from here: the seeds and the page loads take time the 900 s thread timeout counts too
        seeded = seed_session(base_url, tmp_path, suffix, description="controls sweep probe")
        wid, sid = seeded.wid, seeded.sid
        steps = _seed_graph(base_url, graph_id, agent_id)
        made.append(f"/v1/graphs/{graph_id}")
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            made.append(f"/v1/ssp/{ssp_id}")
            r = c.post("/v1/ssp", json={"id": ssp_id, "provider": "lance", "config": {"path": f"/tmp/sweep-lance-{suffix}"}})   # the Semantic search toolbar (backend filter, register menu) draws only when one exists
            assert r.status_code == 201, r.text
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            made.append(f"/v1/channel_providers/{provider_id}")
            r = c.post("/v1/channel_providers", json={"id": provider_id, "provider": "discord", "config": {"bot_token": "x" * 60}})   # New channel and the rules toolbar need one
            assert r.status_code in (200, 201), r.text
        _session_log(tmp_path, wid, sid)

        # the Studio shell, a session document, the create overlays
        open_shell(page, console_url, wid, timeout=sweep.budget.wait_ms(20_000))
        sweep.at("studio", ready=['[data-testid="nv-root"]'])
        open_session_in_studio(page, console_url, wid, sid, kind="agent", timeout=sweep.budget.wait_ms(20_000))
        expect(page.locator(".nv-turn-error").first).to_be_visible(timeout=sweep.budget.wait_ms(20_000))
        sweep.at("session document", ready=[".nv-turn-error"], check_page=False)   # its failed turn is the point of the page
        for name in OVERLAYS:
            open_overlay(page, console_url, wid, name, timeout=sweep.budget.wait_ms(20_000))
            sweep.at(f"overlay {name}", f'[data-testid="nv-overlay:{name}"]', ready=[f'[data-testid="nv-overlay:{name}"] [data-testid="nv-overlay-body"]'])

        # every Platform page as an overlay (its legacy route), and the forms it opens
        for route, forms in LEGACY_FORMS.items():
            def reopen(route=route):
                open_legacy_route(page, console_url, route, timeout=sweep.budget.wait_ms(45_000))
            _sweep_page(page, sweep, "overlay-page", route, OVERLAY, forms, reopen, OVERLAY)
            if route in DEAD_REGISTER_MENUS:
                _assert_register_menu_is_dead(page, reopen, route, sweep)

        # every Platform VIEW (what every nav row of the Platform view opens), and the forms it opens
        for nav, forms in PLATFORM_FORMS.items():
            def reopen(nav=nav):
                open_view(page, console_url, wid, f"platform:{nav}", timeout=sweep.budget.wait_ms(20_000))
            _sweep_page(page, sweep, "platform-view", nav, PLATFORM, forms, reopen, PLATFORM)

        # every System view, and the forms it opens
        for nav in SYSTEM_VIEWS:
            def reopen(nav=nav):
                open_view(page, console_url, wid, f"system:{nav}", timeout=sweep.budget.wait_ms(20_000))
            _sweep_page(page, sweep, "system-view", nav, SYSTEM, SYSTEM_FORMS.get(nav, []), reopen, SYSTEM)

        # the graph builder: a step of each kind, the JSON import, the palette
        open_legacy_route(page, console_url, f"graphs/{graph_id}", timeout=sweep.budget.wait_ms(45_000))
        gb.wait_for_builder(page, timeout=sweep.budget.wait_ms(20_000))
        rows = page.locator('[data-testid="gb-outline-row"]')
        expect(rows).to_have_count(steps, timeout=sweep.budget.wait_ms(15_000))
        sweep.at("graph builder", gb.BUILDER, ready=[gb.OUTLINE, gb.CANVAS])
        for i in range(steps):
            node_id = rows.nth(i).get_attribute("data-node-id")
            rows.nth(i).click(timeout=sweep.budget.wait_ms(10_000))
            # the inspector shows the step that was clicked: its id is the mono text beside the step's name (not a word of the whole panel, whose option labels can name another step)
            expect(page.get_by_test_id("gb-inspector-title").locator("xpath=..").locator(".mono")).to_have_text(node_id or "", timeout=sweep.budget.wait_ms(10_000))
            sweep.at(f"graph builder / step {i + 1}", gb.BUILDER, ready=[gb.INSPECTOR])
        page.locator('[data-testid="gb-json-tab"]').click(timeout=sweep.budget.wait_ms(10_000))
        expect(page.locator(MODAL).first).to_be_visible(timeout=sweep.budget.wait_ms(5_000))
        sweep.at("graph builder / JSON import", MODAL, ready=[f"{MODAL} {FIRST_FIELD}:visible"])
        page.locator(MODAL).get_by_role("button", name="Cancel", exact=True).click(timeout=sweep.budget.wait_ms(10_000))
        page.locator(gb.OUTLINE_ADD).first.click(timeout=sweep.budget.wait_ms(10_000))
        expect(page.locator(gb.PALETTE)).to_be_visible(timeout=sweep.budget.wait_ms(5_000))
        sweep.at("graph builder / palette", gb.BUILDER, ready=[gb.PALETTE])

        # the phone: its four tabs
        # (the open session document would fill the phone screen, so the stored tabs are cleared and the console is loaded afresh at its root)
        page.set_viewport_size(PHONE)
        page.evaluate("() => { localStorage.clear(); sessionStorage.clear(); }")
        page.goto(console_url, timeout=sweep.budget.wait_ms(30_000))
        expect(page.get_by_test_id("nv-mobile-shell")).to_be_visible(timeout=sweep.budget.wait_ms(20_000))
        for tab in PHONE_TABS:
            page.get_by_role("tab", name=tab).click(timeout=sweep.budget.wait_ms(10_000))
            if sweep.arrive("phone", tab, surface=f"phone / {tab}"):
                sweep.at(f"phone / {tab}", '[data-testid="nv-mobile-shell"]')
            else:
                sweep.skip(f"phone / {tab}", "its panel never showed")
    except Exception as exc:  # noqa: BLE001 - reported below with everything found before it died
        died = exc
    finally:
        try:
            sweep.close()
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

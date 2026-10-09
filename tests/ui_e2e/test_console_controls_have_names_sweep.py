"""The standing guard for unnamed controls (console review C-003): one browser, one sequential sweep over the console's main surfaces.

A control with no name is an unlabelled edit field or an anonymous button to a screen reader. The label ratchet (``tests/ui/test_field_label_ratchet.py``) counts only labels drawn with the
``field-label`` classes, so it cannot see a control drawn any other way; this test looks at the REAL page, and the NAME comes from Chromium's accessibility tree (``tests/ui_e2e/_a11y.py``:
the page only enumerates the candidate controls). It visits, in one browser and one after the other (``tests/ui_e2e/_a11y_surfaces.py`` is the list):

* the Studio shell, a session document with a failed turn (Retry and trace on the page), and the New session, New workspace and Activity overlays;
* every Platform page as an overlay (the legacy routes) AND as a VIEW (``view=platform:<id>``, what every Platform nav row opens), and every System view (``view=system:<id>``);
* for each of them, every create form it has (an explicit table of the button that opens it; a form that does not open FAILS, it is not skipped);
* the graph builder on a seeded graph with a step of each of its kinds, the JSON import and the palette;
* the four phone tabs.

and fails on any control with no accessible name that is not on ``ALLOWLIST``, on a surface that examined fewer controls than its floor (a sweep of nothing proves nothing), on a visit that
did not happen, on a page error, and on an allowlist entry that excused nothing. NOT covered (listed in ``docs/dev/subsystems/ui-pages.md``): detail pages, edit forms, the confirm host,
menus, the command palette, the Files sidebar and terminal, the phone's drill-downs and sheets, 422 states, the row actions of a populated list, and controls that are a ``div`` or a
``span`` with only a click handler (they are not exposed as controls at all).

``ALLOWLIST`` is empty on purpose and may only shrink: an entry that matches nothing in a run FAILS the test.
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
from tests.ui_e2e._a11y import ALLOWLIST, AxProbe, classify
from tests.ui_e2e._a11y_surfaces import (
    LEGACY_FORMS,
    MODAL,
    OVERLAY,
    OVERLAYS,
    PHONE_TABS,
    PLATFORM,
    PLATFORM_FORMS,
    SYSTEM,
    SYSTEM_VIEWS,
    expected_surfaces,
    form_surface,
)
from tests.ui_e2e._session_seed import delete_seeded, seed_session
from tests.ui_e2e._shell_helpers import open_legacy_route, open_overlay, open_shell, open_view
from tests.ui_e2e._studio_helpers import open_session_in_studio

pytestmark = smk("SMK-UI-06", status="partial")

PHONE = {"width": 390, "height": 844}
OWN_FAILURE = "the session's own turn: the model fell over"

# The fewest controls a surface may examine before the sweep calls it empty. Observed counts are larger; these are floors that a page which failed to draw (a spinner, an error card) falls under.
FLOOR = {"page": 1, "form": 2, "builder": 2, "phone": 1}


class _Sweep:
    def __init__(self, page: Page) -> None:
        self.page = page
        self.probe = AxProbe(page)
        self.found: dict[str, list[str]] = {}
        self.visited: list[str] = []
        self.examined: dict[str, int] = {}
        self.thin: list[str] = []

    def at(self, surface: str, root: str | None = None, *, floor: str = "page") -> None:
        """Record the unnamed controls under ``root`` (a CSS selector that must match a visible element; the whole page when none) as the page is now, under ``surface``."""
        self.visited.append(surface)
        self.page.wait_for_timeout(250)  # let a just-opened surface finish painting
        result = self.probe.examine(root)
        self.examined[surface] = result.examined
        if result.examined < FLOOR[floor]:
            self.thin.append(f"{surface}: examined {result.examined} control(s), at least {FLOOR[floor]} expected")
        for item in result.unnamed:
            self.found.setdefault(item["html"], []).append(surface)

    def report(self) -> str:
        unnamed, _stale = classify(self.found, ALLOWLIST)
        return "\n".join(f"  {', '.join(surfaces)}\n      {html}" for html, surfaces in unnamed.items())


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


def _sweep_forms(page: Page, sweep: _Sweep, kind: str, name: str, forms: list[tuple[str, str]], reopen, scope: str) -> None:
    """For each ``(button, root)`` of a page: open the page afresh, press the button (it must be there and enabled), wait for what it opens, sweep it, close it."""
    for button, root in forms:
        reopen()
        new = page.locator(scope).get_by_role("button", name=re.compile(rf"^(\+ )?{re.escape(button)}\b")).first
        expect(new).to_be_visible(timeout=15_000)
        expect(new).to_be_enabled(timeout=5_000)
        new.click()
        expect(page.locator(root).first).to_be_visible(timeout=10_000)
        sweep.at(form_surface(kind, name, button), root, floor="form")
        page.keyboard.press("Escape")
        if root == MODAL:
            expect(page.locator(MODAL)).to_have_count(0, timeout=5_000)


@pytest.mark.ui_e2e
@pytest.mark.timeout(900)
def test_no_visible_control_of_the_consoles_main_surfaces_is_without_a_name(base_url: str, console_url: str, page: Page, tmp_path) -> None:
    suffix = uuid.uuid4().hex[:8]
    seeded = seed_session(base_url, tmp_path, suffix, description="controls sweep probe")
    wid, sid = seeded.wid, seeded.sid
    agent_id = f"dn-agent-{suffix}"
    graph_id = f"sweep-graph-{suffix}"
    provider_id = f"sweep-cp-{suffix}"
    page_errors: list[str] = []
    page.on("pageerror", lambda exc: page_errors.append(str(exc)))
    sweep = _Sweep(page)
    died: Exception | None = None
    steps = 0
    try:
        steps = _seed_graph(base_url, graph_id, agent_id)
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            r = c.post("/v1/channel_providers", json={"id": provider_id, "provider": "discord", "config": {"bot_token": "x" * 60}})   # New channel and the rules toolbar need one
            assert r.status_code in (200, 201), r.text
        _session_log(tmp_path, wid, sid)

        # the Studio shell, a session document, the create overlays
        open_shell(page, console_url, wid)
        sweep.at("studio")
        open_session_in_studio(page, console_url, wid, sid, kind="agent")
        expect(page.locator(".nv-turn-error").first).to_be_visible(timeout=20_000)
        sweep.at("session document")
        for name in OVERLAYS:
            open_overlay(page, console_url, wid, name)
            sweep.at(f"overlay {name}", f'[data-testid="nv-overlay:{name}"]')

        # every Platform page as an overlay (its legacy route), and the forms it opens
        for route, forms in LEGACY_FORMS.items():
            def reopen(route=route):
                open_legacy_route(page, console_url, route)
            reopen()
            sweep.at(f"overlay-page {route}", OVERLAY)
            _sweep_forms(page, sweep, "overlay-page", route, forms, reopen, OVERLAY)

        # every Platform VIEW (what every nav row of the Platform view opens), and the forms it opens
        for nav, forms in PLATFORM_FORMS.items():
            def reopen(nav=nav):
                open_view(page, console_url, wid, f"platform:{nav}")
            reopen()
            sweep.at(f"platform-view {nav}", PLATFORM)
            _sweep_forms(page, sweep, "platform-view", nav, forms, reopen, PLATFORM)

        # every System view
        for nav in SYSTEM_VIEWS:
            open_view(page, console_url, wid, f"system:{nav}")
            sweep.at(f"system-view {nav}", SYSTEM)

        # the graph builder: a step of each kind, the JSON import, the palette
        open_legacy_route(page, console_url, f"graphs/{graph_id}")
        gb.wait_for_builder(page)
        rows = page.locator('[data-testid="gb-outline-row"]')
        expect(rows).to_have_count(steps, timeout=15_000)
        sweep.at("graph builder", gb.BUILDER, floor="builder")
        for i in range(steps):
            rows.nth(i).click()
            sweep.at(f"graph builder / step {i + 1}", gb.BUILDER, floor="builder")
        page.locator('[data-testid="gb-json-tab"]').click()
        expect(page.locator(MODAL).first).to_be_visible(timeout=5_000)
        sweep.at("graph builder / JSON import", MODAL, floor="form")
        page.locator(MODAL).get_by_role("button", name="Cancel", exact=True).click()
        page.locator(gb.OUTLINE_ADD).first.click()
        expect(page.locator(gb.PALETTE)).to_be_visible(timeout=5_000)
        sweep.at("graph builder / palette", gb.BUILDER, floor="builder")

        # the phone: its four tabs
        # (the open session document would fill the phone screen, so the stored tabs are cleared and the console is loaded afresh at its root)
        page.set_viewport_size(PHONE)
        page.evaluate("() => { localStorage.clear(); sessionStorage.clear(); }")
        page.goto(console_url)
        expect(page.get_by_test_id("nv-mobile-shell")).to_be_visible(timeout=20_000)
        for tab in PHONE_TABS:
            page.get_by_role("tab", name=tab).click()
            sweep.at(f"phone / {tab}", '[data-testid="nv-mobile-shell"]', floor="phone")
    except Exception as exc:  # noqa: BLE001 - reported below with everything found before it died
        died = exc
    finally:
        sweep.probe.close()
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            c.delete(f"/v1/channel_providers/{provider_id}")
            c.delete(f"/v1/graphs/{graph_id}")
        left = delete_seeded(base_url, seeded)

    # Everything is classified AFTER the sweep, so that a sweep that died part-way still reports what it had found, and every kind of failure is reported together.
    unnamed, stale = classify(sweep.found, ALLOWLIST)
    where = f"swept {len(sweep.visited)} surface(s), the last one {sweep.visited[-1]!r}" if sweep.visited else "swept nothing"
    if died is not None:
        raise AssertionError(f"the sweep died ({where}): {died!r}\n{len(unnamed)} unnamed control(s) found before it died:\n{sweep.report()}") from died
    problems: list[str] = []
    if unnamed:
        problems.append(f"{len(unnamed)} control(s) with no name, by surface ({where}):\n{sweep.report()}")
    expected = expected_surfaces(steps)
    if sweep.visited != expected:
        problems.append("the sweep did not visit exactly the surfaces it lists, difference: " + str(sorted(set(expected) ^ set(sweep.visited))) + f" ({len(sweep.visited)} visited, {len(expected)} listed)")
    if sweep.thin:
        problems.append("surfaces that examined too few controls:\n  " + "\n  ".join(sweep.thin))
    if page_errors:
        problems.append(f"page errors during the sweep: {page_errors}")
    if stale:
        problems.append(f"allowlist entries that match nothing in this run (remove them): {stale}")
    if left:
        problems.append(f"seeded rows that could not be deleted: {left}")
    assert not problems, "\n\n".join(problems)

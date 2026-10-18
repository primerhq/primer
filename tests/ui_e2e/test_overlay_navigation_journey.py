"""Journeys: Delete and Back on a detail overlay close it and land on the Platform page, never on a second list (revamp design 10 and D1).

A detail page's Delete and Back called ``navigate("/<plural>")``, which the router shim turned into "open that overlay with no record": the overlay rendered the old LIST
page, a second copy of the Platform page underneath it (for a trigger, with its own "Create trigger" button). Now the overlay closes and the user is on the entity's Platform
page, with the toast. The unit tests (``tests/ui/test_overlay_navigation.py``) run the shim, the shell's navigations, the host and the crumb in V8; these drive the real pages.

Every test that leaves a detail ends with ``_assert_on_the_platform_page``: no ``nv-overlay:<name>`` in the DOM, no ``overlay=`` in the address, ``view=platform:<nav>`` in it,
and the page's ``nv-plat-page:<nav>`` visible. A record is opened by setting the hash to the address a card click writes (``#/w/primer?view=platform:<nav>&overlay=<nav>::<id>``),
so the journeys do not depend on the card's test ids (the card is being rebuilt by the kit).

A session row on a detail page is the other half: it navigated to ``/sessions/<id>``, which the shim read as an overlay path and answered by reopening the current overlay with the
session id in its tab slot. It opens the session in the studio now.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager

import httpx
from playwright.sync_api import Page, expect

from tests._support.model_profiles import agent_model, seed_llm_provider_with
from tests._support.smk import smk
from tests.ui_e2e._session_seed import delete_seeded, seed_session
from tests.ui_e2e._shell_helpers import open_view

pytestmark = smk("SMK-UI-03", status="partial")

OVERLAY_ANYWHERE = '[data-testid^="nv-overlay:"]'


def _plain_url(page: Page) -> str:
    return page.url.replace("%3A", ":")


def _open_platform_page(page: Page, console_url: str, nav: str) -> None:
    """The shell must be mounted before the view hash is assigned (a hash set while it mounts can be replaced by the shell's own normalisation to the Studio view), so
    wait for it and navigate once more if the page is not the Platform one after a short wait."""
    expect(page.get_by_test_id("nv-root")).to_be_visible(timeout=20_000)
    marker = page.get_by_test_id(f"nv-plat-page:{nav}")
    for attempt in (1, 2):
        open_view(page, console_url, "primer", f"platform:{nav}")
        try:
            expect(marker).to_be_visible(timeout=10_000 if attempt == 1 else 20_000)
            return
        except AssertionError:
            if attempt == 2:
                raise


def _assign_hash(page: Page, console_url: str, fragment: str) -> None:
    current_origin = page.url.split("#", 1)[0].rstrip("/")
    if current_origin == console_url.rstrip("/"):
        page.evaluate("(h) => { window.location.hash = h; }", fragment)
    else:
        page.goto(f"{console_url}{fragment}")


def _open_record(page: Page, console_url: str, nav: str, record_id: str) -> None:
    """Open ``record_id`` the way a card's Open writes it: the Platform page underneath, the record's overlay over it."""
    expect(page.get_by_test_id("nv-root")).to_be_visible(timeout=20_000)
    overlay = page.get_by_test_id(f"nv-overlay:{nav}")
    for attempt in (1, 2):
        _assign_hash(page, console_url, f"#/w/primer?view=platform:{nav}&overlay={nav}::{record_id}")
        try:
            expect(overlay).to_be_visible(timeout=20_000 if attempt == 1 else 45_000)
            return
        except AssertionError:
            if attempt == 2:
                raise


def _paste_record_link(page: Page, console_url: str, nav: str, record_id: str) -> None:
    """A pasted link, or the palette: the overlay opens over the STUDIO, not over the entity's Platform page."""
    expect(page.get_by_test_id("nv-root")).to_be_visible(timeout=20_000)
    _assign_hash(page, console_url, f"#/?overlay={nav}::{record_id}")
    expect(page.get_by_test_id(f"nv-overlay:{nav}")).to_be_visible(timeout=45_000)
    assert "view=platform" not in _plain_url(page), "a pasted link names no Platform page"


def _assert_on_the_platform_page(page: Page, nav: str) -> None:
    expect(page.get_by_test_id(f"nv-plat-page:{nav}")).to_be_visible(timeout=15_000)
    expect(page.locator(OVERLAY_ANYWHERE)).to_have_count(0)
    expect(page).not_to_have_url(re.compile(r"overlay="), timeout=5_000)
    expect(page).to_have_url(re.compile(rf"view=platform(:|%3A){nav}(?![\w-])"), timeout=5_000)


def _delete(base_url: str, path: str) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        try:
            c.delete(path)
        except Exception:  # noqa: BLE001 - best-effort cleanup
            pass


@contextmanager
def _seeded_llm_provider(base_url: str, suffix: str) -> Iterator[str]:
    """A placeholder ollama LLM provider (and the model profile it brings) so an agent can be seeded; deleted on exit."""
    pid = f"llm-ovnav-{suffix}"
    body = {
        "id": pid,
        "provider": "ollama",
        "config": {"url": "http://127.0.0.1:9999"},
        "models": [{"name": "fake-model", "context_length": 4096}],
        "limits": {"max_concurrency": 1},
    }
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        assert seed_llm_provider_with(c, body).status_code == 201
    try:
        yield pid
    finally:
        _delete(base_url, f"/v1/llm_providers/{pid}")


def _seed_agent(base_url: str, agent_id: str, provider_id: str) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post(
            "/v1/agents",
            json={
                "id": agent_id, "description": "overlay navigation journey", "model": agent_model(provider_id, "fake-model"),
                "tools": [], "system_prompt": ["test"],
            },
        )
        assert r.status_code == 201, r.text


def _seed_trigger(base_url: str, slug: str) -> str:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.post("/v1/triggers", json={"slug": slug, "name": slug, "description": None, "config": {"kind": "webhook"}, "enabled": True})
        assert r.status_code == 201, r.text
        return r.json()["id"]


# ---------------------------------------------------------------------------
# Graphs (design 10: the defect the user saw)
# ---------------------------------------------------------------------------


def test_a_graph_created_opened_and_deleted_ends_on_the_graphs_page(page: Page, base_url: str, console_url: str, unique_suffix: str) -> None:
    agent_id = f"ag-ovnav-g-{unique_suffix}"
    graph_id = f"graph-ovnav-{unique_suffix}"
    with _seeded_llm_provider(base_url, unique_suffix) as provider_id:
        _seed_agent(base_url, agent_id, provider_id)
        try:
            _open_platform_page(page, console_url, "graphs")

            page.get_by_test_id("nv-plat-create").click()
            modal = page.locator(".modal").first
            modal.wait_for(state="visible", timeout=10_000)
            modal.locator("input.input").first.fill(graph_id)
            modal.locator("select.select").first.select_option(value=agent_id)
            modal.get_by_role("button", name="Create").first.click()

            # The created graph's detail overlay opens over the page.
            expect(page.get_by_test_id("nv-overlay:graphs")).to_be_visible(timeout=15_000)
            expect(page).to_have_url(re.compile(rf"overlay=graphs(:|%3A){{2}}{graph_id}"), timeout=10_000)

            page.get_by_test_id("nv-overlay-body").get_by_role("button", name="Delete", exact=True).first.click()
            confirm = page.locator(".modal", has_text="Delete graph?")
            expect(confirm).to_be_visible(timeout=5_000)
            confirm.get_by_role("button", name="Delete", exact=True).click()

            _assert_on_the_platform_page(page, "graphs")
            expect(page.get_by_text("Graph deleted", exact=False).first).to_be_visible(timeout=5_000)
        finally:
            _delete(base_url, f"/v1/graphs/{graph_id}")
            _delete(base_url, f"/v1/agents/{agent_id}")


def test_back_from_a_graph_that_cannot_load_ends_on_the_graphs_page(page: Page, console_url: str, unique_suffix: str) -> None:
    _open_platform_page(page, console_url, "graphs")
    _open_record(page, console_url, "graphs", f"missing-{unique_suffix}")

    page.get_by_role("button", name="Back to list").click()

    _assert_on_the_platform_page(page, "graphs")


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------


def test_an_agent_opened_and_deleted_ends_on_the_agents_page(page: Page, base_url: str, console_url: str, unique_suffix: str) -> None:
    agent_id = f"ag-ovnav-d-{unique_suffix}"
    with _seeded_llm_provider(base_url, unique_suffix) as provider_id:
        _seed_agent(base_url, agent_id, provider_id)
        try:
            _open_platform_page(page, console_url, "agents")
            _open_record(page, console_url, "agents", agent_id)
            expect(page.locator("#na-id")).to_have_value(agent_id, timeout=15_000)

            # The editor's own footer Delete (scoped to the editor: the Platform page under it has a Delete on every card), then the confirm dialog stacked
            # over the editor (the second .modal).
            page.locator(".modal").first.get_by_role("button", name="Delete", exact=True).click()
            confirm = page.locator(".modal").last
            expect(confirm).to_contain_text(agent_id, timeout=5_000)
            confirm.get_by_role("button", name="Delete", exact=True).click()

            _assert_on_the_platform_page(page, "agents")
            expect(page.get_by_text("Agent deleted", exact=False).first).to_be_visible(timeout=5_000)
        finally:
            _delete(base_url, f"/v1/agents/{agent_id}")


def test_closing_an_agent_ends_on_the_agents_page(page: Page, base_url: str, console_url: str, unique_suffix: str) -> None:
    agent_id = f"ag-ovnav-c-{unique_suffix}"
    with _seeded_llm_provider(base_url, unique_suffix) as provider_id:
        _seed_agent(base_url, agent_id, provider_id)
        try:
            _open_platform_page(page, console_url, "agents")
            _open_record(page, console_url, "agents", agent_id)
            expect(page.locator("#na-id")).to_have_value(agent_id, timeout=15_000)

            page.locator(".modal").first.get_by_role("button", name="Close", exact=True).click()

            _assert_on_the_platform_page(page, "agents")
        finally:
            _delete(base_url, f"/v1/agents/{agent_id}")


def test_a_session_row_in_an_agents_panel_opens_the_session_in_the_studio(page: Page, base_url: str, console_url: str, unique_suffix: str, tmp_path) -> None:
    """The agent form lists the agent's sessions; a row navigated to ``/sessions/<id>``, which the shim answered by reopening the agent editor: the user never saw the session.
    The row carries the session's own workspace_id, and the console opens the session in THAT workspace (the seeded one, not the one the page was on) without waiting for its rail to learn it."""
    seeded = seed_session(base_url, tmp_path, unique_suffix, description="overlay navigation journey")
    agent_path = next(path for path in seeded.delete_paths if path.startswith("/v1/agents/"))
    agent_id = agent_path.rsplit("/", 1)[1]
    try:
        _open_platform_page(page, console_url, "agents")
        _open_record(page, console_url, "agents", agent_id)
        expect(page.locator("#na-id")).to_have_value(agent_id, timeout=15_000)

        panel = page.get_by_test_id("agent-sessions-panel")
        panel.get_by_role("button", name=re.compile(r"^Sessions")).click()
        panel.get_by_text(seeded.sid, exact=True).click()

        # The studio, with the session open as a tab: no overlay, and no Platform view left in the address.
        expect(page).to_have_url(re.compile(rf"doc=session(:|%3A){seeded.sid}"), timeout=20_000)
        expect(page).to_have_url(re.compile(rf"#/w/{re.escape(seeded.wid)}(\?|$)"), timeout=10_000)
        expect(page.get_by_test_id(f"nv-tg-tab:session:{seeded.sid}")).to_be_visible(timeout=20_000)
        expect(page.locator(OVERLAY_ANYWHERE)).to_have_count(0)
        assert "overlay=" not in _plain_url(page) and "view=" not in _plain_url(page), page.url
        expect(page.get_by_test_id("nv-studio")).to_be_visible(timeout=10_000)
    finally:
        delete_seeded(base_url, seeded)


# ---------------------------------------------------------------------------
# Triggers (D1: a deleted trigger left a "Create trigger" overlay behind)
# ---------------------------------------------------------------------------


def test_a_trigger_deleted_from_its_card_leaves_no_create_trigger_overlay(page: Page, base_url: str, console_url: str, unique_suffix: str) -> None:
    tid = _seed_trigger(base_url, f"ovnav-del-{unique_suffix}")
    try:
        _open_platform_page(page, console_url, "triggers")
        _open_record(page, console_url, "triggers", tid)

        page.get_by_test_id("trigger-detail").get_by_role("button", name="Delete trigger", exact=True).click()
        page.locator(".modal").get_by_role("button", name="Delete trigger", exact=True).click()

        _assert_on_the_platform_page(page, "triggers")
        expect(page.get_by_role("button", name="Create trigger")).to_have_count(0)
        expect(page.get_by_text("Trigger deleted", exact=False).first).to_be_visible(timeout=5_000)
    finally:
        _delete(base_url, f"/v1/triggers/{tid}")


def test_a_trigger_opened_by_a_pasted_link_and_deleted_ends_on_the_triggers_page(page: Page, base_url: str, console_url: str, unique_suffix: str) -> None:
    """Review Focus 2: the overlay was not opened from its Platform page, so there is no page to fall back to: the user lands on the entity's page all the same."""
    tid = _seed_trigger(base_url, f"ovnav-link-{unique_suffix}")
    try:
        _paste_record_link(page, console_url, "triggers", tid)

        page.get_by_test_id("trigger-detail").get_by_role("button", name="Delete trigger", exact=True).click()
        page.locator(".modal").get_by_role("button", name="Delete trigger", exact=True).click()

        _assert_on_the_platform_page(page, "triggers")
        expect(page.get_by_role("button", name="Create trigger")).to_have_count(0)
        expect(page.get_by_text("Trigger deleted", exact=False).first).to_be_visible(timeout=5_000)
    finally:
        _delete(base_url, f"/v1/triggers/{tid}")


def test_back_from_a_trigger_ends_on_the_triggers_page(page: Page, base_url: str, console_url: str, unique_suffix: str) -> None:
    tid = _seed_trigger(base_url, f"ovnav-back-{unique_suffix}")
    try:
        _open_platform_page(page, console_url, "triggers")
        _open_record(page, console_url, "triggers", tid)

        page.get_by_test_id("trigger-detail").get_by_role("button", name="Back", exact=True).click()

        _assert_on_the_platform_page(page, "triggers")
        expect(page.get_by_role("button", name="Create trigger")).to_have_count(0)
    finally:
        _delete(base_url, f"/v1/triggers/{tid}")

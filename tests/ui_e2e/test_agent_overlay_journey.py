"""Journeys for the rebuilt agent overlay (platform revamp plan 05, Task 6; design section 9, layout A).

* Editing one field of an agent that has every field set changes that field and nothing else (master Review Focus 1). The overlay hides two sections (Voice,
  Advanced) and never mounts the controls of a closed one, so this is the proof that what it does not show it still sends back.
* The system prompt parts are tabs over one editor: "+ Part" selects the part it adds, a tab shows its part, Full screen covers the window, and Escape leaves it
  before it closes the overlay (Review Focus 4). Full screen is a focus trap: Tab and Shift+Tab stay inside it however often they are pressed (review of #779).
  A two-part prompt is saved as two parts.
* A response format that does not parse reopens the folded Advanced section and sends nothing (Review Focus 5).
* The Chat button makes its session in the default workspace and opens it in the studio. It used to post a session route that does not exist. Opening the
  session in the studio goes through the router shim's session route (platform revamp plan 01, task 13), so that half needs that change in the build under test.
  The default workspace is the ONE row ``GET /v1/workspaces`` flags ``is_default`` (lead ruling, review of #779: no fallback to the first workspace), so this
  journey needs #770, which adds that response-only flag, on main: without it no row is flagged and the journey FAILS (it does not skip), as the button would.

Each test seeds its own provider and agent, with a fake ``ollama`` provider, and removes them in ``finally``.
"""

from __future__ import annotations

import re
import time

import httpx
from playwright.sync_api import Page, expect

from tests._support.model_profiles import agent_model, profile_id_for, seed_llm_provider_with
from tests._support.smk import smk
from tests.ui_e2e._shell_helpers import open_legacy_route

pytestmark = smk("SMK-UI-03", status="partial")

EVERY_FIELD = {
    "description": "before the journey", "temperature": 0.7, "max_tool_turns": 25, "max_output_tokens": 1024,
    "tools": ["system__invoke_agent", "web__web_search"], "system_prompt": ["You triage refunds.", "Be brief."],
    "compaction_prompt": ["Keep the cited sources."], "compaction_tool_access": True, "allow_external_tools": True,
    "response_format": {"type": "object", "properties": {"verdict": {"type": "string"}}, "required": ["verdict"]},
    "tts_voice": "alloy",
}


def _seed_provider(c: httpx.Client, provider_id: str) -> None:
    r = seed_llm_provider_with(c, {
        "id": provider_id, "provider": "ollama", "config": {"url": "http://127.0.0.1:9999"},
        "models": [{"name": "fake-model", "context_length": 4096}], "limits": {"max_concurrency": 1},
    })
    assert r.status_code == 201, r.text


def _remove(base_url: str, *paths: str) -> None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        for path in paths:
            try:
                c.delete(path)
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass


def _default_workspace_id(c: httpx.Client) -> str:
    """The id of the ONE workspace ``GET /v1/workspaces`` flags ``is_default``, reading the list a page at a time as the button does (the shared e2e database can
    hold more workspaces than one page). FAILS when no row, or more than one, is flagged: that is #770 missing from the build under test, and the journey must not
    pick the first workspace instead, as the old button did."""
    page_size, rows = 200, []
    while True:
        body = c.get("/v1/workspaces", params={"limit": page_size, "offset": len(rows)}).json()
        rows.extend(body["items"])
        if len(body["items"]) < page_size or (body.get("total") is not None and len(rows) >= body["total"]):
            break
    flagged = [w["id"] for w in rows if w.get("is_default") is True]
    assert len(flagged) == 1, f"{len(flagged)} workspaces are flagged is_default ({flagged}) among {[w['id'] for w in rows]}: this journey needs #770 on main"
    return flagged[0]


def _get_agent(base_url: str, agent_id: str) -> dict | None:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        r = c.get(f"/v1/agents/{agent_id}")
    return r.json() if r.status_code == 200 else None


def _wait_for_agent(page: Page, base_url: str, agent_id: str, ready, *, seconds: int = 15) -> dict | None:
    """Poll the row until ``ready(row)`` holds; the last row read (or None) is returned either way, so the caller's assertion shows what was there."""
    deadline = time.time() + seconds
    row = None
    while time.time() < deadline:
        row = _get_agent(base_url, agent_id)
        if row is not None and ready(row):
            return row
        page.wait_for_timeout(300)
    return row


def test_editing_one_field_through_the_console_keeps_every_other_field(page: Page, base_url: str, console_url: str, unique_suffix: str) -> None:  # Review Focus 1
    provider_id = f"llm-ago-{unique_suffix}"
    agent_id = f"ag-ago-{unique_suffix}"
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        _seed_provider(c, provider_id)
        r = c.post("/v1/agents", json={**EVERY_FIELD, "id": agent_id, "model": agent_model(provider_id, "fake-model")})
        assert r.status_code == 201, r.text
        before = c.get(f"/v1/agents/{agent_id}").json()
    try:
        open_legacy_route(page, console_url, f"agents/{agent_id}")
        expect(page.locator("#na-id")).to_have_value(agent_id, timeout=15_000)
        page.locator("#na-description").fill("edited by the journey")
        page.get_by_test_id("agent-form-submit").click()

        after = _wait_for_agent(page, base_url, agent_id, lambda row: row["description"] == "edited by the journey")
        assert after is not None and after["description"] == "edited by the journey", after
        assert {k: v for k, v in after.items() if k != "description"} == {k: v for k, v in before.items() if k != "description"}
    finally:
        _remove(base_url, f"/v1/agents/{agent_id}", f"/v1/llm_providers/{provider_id}")


def test_the_prompt_tabs_full_screen_and_a_two_part_save(page: Page, base_url: str, console_url: str, unique_suffix: str) -> None:  # Review Focus 4
    provider_id = f"llm-agt-{unique_suffix}"
    agent_id = f"ag-agt-{unique_suffix}"
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        _seed_provider(c, provider_id)
    try:
        open_legacy_route(page, console_url, "agents")
        page.get_by_role("button", name="New agent").first.click()
        modal = page.locator(".modal").first
        expect(modal.get_by_text("New agent", exact=True)).to_be_visible(timeout=5_000)
        expect(modal.get_by_text("Autonomous")).to_have_count(0)

        modal.locator("#na-id").fill(agent_id)
        modal.locator("#na-description").fill("tabs journey")
        modal.locator("#na-model-profile").select_option(value=profile_id_for(provider_id, "fake-model"))

        editor = modal.get_by_test_id("agent-system-prompt-editor")
        editor.fill("You triage refunds.")
        modal.get_by_test_id("agent-system-prompt-add").click()
        expect(modal.get_by_role("tab", name="Part 2")).to_have_attribute("aria-selected", "true")
        editor.fill("Be brief.")
        modal.get_by_role("tab", name="Part 1").click()
        expect(editor).to_have_value("You triage refunds.")

        modal.get_by_test_id("agent-system-prompt-fullscreen").click()
        box, viewport = modal.get_by_test_id("agent-system-prompt").bounding_box(), page.viewport_size
        assert box is not None and viewport is not None
        # The full-screen editor covers the window. A smaller box means an ancestor of the editor has a transform, which turns position: fixed into
        # "fixed inside that ancestor": report it, never loosen this.
        assert box["width"] >= viewport["width"] - 2 and box["height"] >= viewport["height"] - 2, (box, viewport)

        # The layer is a focus trap (review of #779, required fix 2). Tab used to walk from the editor on to the controls the layer covers: 23 presses reached the
        # covered Cancel button, and the Enter that followed closed the overlay and lost the draft. Press Tab, then Shift+Tab, more times than the layer has stops
        # (two tabs, "+ Part", "Remove part", the toggle and the editor): focus must be inside the layer after EVERY press, and the walk must have reached all six.
        layer_stops = {
            "agent-system-prompt-tab-0", "agent-system-prompt-tab-1", "agent-system-prompt-add", "agent-system-prompt-remove",
            "agent-system-prompt-fullscreen", "agent-system-prompt-editor",
        }
        for key in ("Tab", "Shift+Tab"):
            visited: set[str | None] = set()
            for _ in range(2 * len(layer_stops) + 2):
                page.keyboard.press(key)
                held = page.evaluate(
                    """() => {
                        const el = document.activeElement;
                        const layer = document.querySelector('[data-testid="agent-system-prompt"]');
                        return { testid: el ? el.getAttribute('data-testid') : null, inside: !!(el && layer && layer.contains(el)) };
                    }"""
                )
                assert held["inside"], f"{key} left the full-screen layer: the focus is on {held['testid']!r}"
                visited.add(held["testid"])
            assert visited == layer_stops, f"{key} did not walk the whole layer: {sorted(visited, key=str)}"
        expect(modal).to_be_visible()

        page.keyboard.press("Escape")
        expect(modal.get_by_test_id("agent-system-prompt")).to_have_attribute("data-fullscreen", "false")
        expect(modal).to_be_visible()  # the first Escape left full screen and did not close the overlay with the draft in it
        expect(editor).to_have_value("You triage refunds.")

        modal.get_by_test_id("agent-form-submit").click()
        agent = _wait_for_agent(page, base_url, agent_id, lambda row: True)
        assert agent is not None, "the agent was not created"
        assert agent["system_prompt"] == ["You triage refunds.", "Be brief."]
    finally:
        _remove(base_url, f"/v1/agents/{agent_id}", f"/v1/llm_providers/{provider_id}")


def test_a_response_format_that_does_not_parse_reopens_advanced_and_sends_nothing(page: Page, base_url: str, console_url: str, unique_suffix: str) -> None:  # Review Focus 5
    provider_id = f"llm-agr-{unique_suffix}"
    agent_id = f"ag-agr-{unique_suffix}"
    posts: list[str] = []
    page.on("request", lambda r: posts.append(r.url) if r.method == "POST" and r.url.rstrip("/").endswith("/v1/agents") else None)
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        _seed_provider(c, provider_id)
    try:
        open_legacy_route(page, console_url, "agents")
        page.get_by_role("button", name="New agent").first.click()
        modal = page.locator(".modal").first
        expect(modal.get_by_text("New agent", exact=True)).to_be_visible(timeout=5_000)
        modal.locator("#na-id").fill(agent_id)
        modal.locator("#na-description").fill("response format journey")
        modal.locator("#na-model-profile").select_option(value=profile_id_for(provider_id, "fake-model"))

        toggle = modal.get_by_test_id("agent-advanced-fold:toggle")
        expect(toggle).to_have_attribute("aria-expanded", "false")
        expect(modal.locator("#na-response-format")).to_have_count(0)  # a closed fold does not mount its controls
        toggle.click()
        modal.locator("#na-response-format").fill("{")
        toggle.click()  # fold it away again, then press Create
        modal.get_by_test_id("agent-form-submit").click()

        expect(modal.get_by_test_id("agent-advanced-fold:toggle")).to_have_attribute("aria-expanded", "true", timeout=5_000)
        expect(modal.get_by_role("alert").filter(has_text="JSON parse:")).to_be_visible(timeout=5_000)
        assert posts == [], "an unparseable response format must not be sent"
    finally:
        _remove(base_url, f"/v1/agents/{agent_id}", f"/v1/llm_providers/{provider_id}")


def test_chat_makes_a_session_in_the_default_workspace_and_opens_it_in_the_studio(page: Page, base_url: str, console_url: str, unique_suffix: str) -> None:
    provider_id = f"llm-agc-{unique_suffix}"
    agent_id = f"ag-agc-{unique_suffix}"
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        _seed_provider(c, provider_id)
        r = c.post("/v1/agents", json={
            "id": agent_id, "description": "chat journey", "model": agent_model(provider_id, "fake-model"), "tools": [], "system_prompt": ["test"],
        })
        assert r.status_code == 201, r.text
    made: list[dict] = []
    try:
        with httpx.Client(base_url=base_url, timeout=30.0) as c:
            expected = _default_workspace_id(c)  # inside the try: a build without #770 fails here, and the seeded rows are still removed
        open_legacy_route(page, console_url, f"agents/{agent_id}")
        expect(page.locator("#na-id")).to_have_value(agent_id, timeout=15_000)
        page.get_by_test_id("agent-form-chat").click()

        deadline = time.time() + 15
        while time.time() < deadline and not made:
            with httpx.Client(base_url=base_url, timeout=30.0) as c:
                made = c.get("/v1/sessions", params={"agent_id": agent_id, "limit": 5}).json().get("items", [])
            if not made:
                page.wait_for_timeout(300)
        assert len(made) == 1, f"the button made {len(made)} sessions for the agent: {made}"
        session = made[0]
        session_id = session.get("session_id") or session["id"]
        assert session["workspace_id"] == expected, f"the session is in {session['workspace_id']!r}, the default workspace is {expected!r}"

        # And the studio shows it: the session is the open document of its own workspace.
        expect(page).to_have_url(re.compile(r"doc=session(:|%3A)" + re.escape(session_id)), timeout=15_000)
        assert re.search(r"#/w/" + re.escape(expected) + r"\b", page.url), page.url
    finally:
        for session in made:
            _remove(base_url, f"/v1/workspaces/{session['workspace_id']}/sessions/{session.get('session_id') or session['id']}")
        _remove(base_url, f"/v1/agents/{agent_id}", f"/v1/llm_providers/{provider_id}")

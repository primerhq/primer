"""W3 browser journey on the real console, with an explicit auth fixture boundary.

The UI lane is auth-disabled. Only auth status/welcome writes and the first
main-session ``created`` flag are simulated; workspace/session reads use the
real server. The final steer POST is recorded rather than launching an LLM.
Real authenticated persistence/restart is owned by the live W3 API journey.
"""

import re

import httpx
import pytest
from playwright.sync_api import expect

from tests.ui_e2e._a11y import unnamed_controls
from tests.ui_e2e._shell_helpers import boot_mobile_shell, default_wid, run_verb


@pytest.mark.ui_e2e
@pytest.mark.parametrize("mobile", [False, True])
@pytest.mark.parametrize("role", ["admin", "user"])
def test_first_visit_revisit_reopen_and_chip_preserve_the_existing_chat(page, base_url, console_url, role, mobile):
    wid = default_wid(console_url)
    with httpx.Client(base_url=base_url, timeout=60) as client:
        main = client.post(f"/v1/workspaces/{wid}/main_session")
        assert main.status_code in (200, 201), main.text
        sid = main.json()["id"]
    state = {"show": True, "acks": 0, "reopens": 0, "main_calls": 0}
    sends = []
    held_reopens = []

    def status(route):
        response = route.fetch()
        route.fulfill(response=response, json={**response.json(),
            "has_user": True, "authenticated": True, "username": "Welcome visitor", "role": role,
            "principal": {"type": "user", "id": "welcome-browser", "role": role, "display": "Welcome visitor"},
            "default_workspace_id": wid, "welcome": {"variant": role, "show": state["show"]},
        })

    def write(route):
        assert route.request.post_data_json == {"role": role}
        reopen = route.request.url.endswith("/reopen")
        state["show"] = reopen
        state["reopens" if reopen else "acks"] += 1
        if reopen and mobile:
            held_reopens.append(route)
            page.evaluate("window.__w3ReopenHeld = true")
        else:
            route.fulfill(status=200, json={"variant": role, "show": reopen})

    def first_login(route):
        response = route.fetch()
        state["main_calls"] += 1
        route.fulfill(response=response, json={**response.json(), "created": state["main_calls"] == 1})

    def steer(route):
        sends.append(route.request.post_data_json)
        route.fulfill(status=200, json={"session_id": sid, "accepted": True})

    page.route("**/v1/auth/status", status)
    page.route("**/v1/auth/welcome/*", write)
    page.route(re.compile(r".*/v1/workspaces/[^/?]+/main_session$"), first_login)
    page.route(f"**/v1/workspaces/{wid}/sessions/{sid}/steer", steer)
    page.goto("about:blank")
    if mobile:
        page.set_viewport_size({"width": 390, "height": 844})
        boot_mobile_shell(page, console_url)
    else:
        page.goto(console_url)
    card = page.get_by_test_id("nv-welcome")
    expect(card).to_be_visible(timeout=30_000)
    expect(card).to_have_attribute("data-variant", role)
    expect(card.get_by_role("heading", name="Welcome, Welcome visitor")).to_be_visible()
    expect(card.get_by_role("heading")).not_to_be_focused()
    own = "Enable internal collections" if role == "admin" else "Start a new project"
    other = "Start a new project" if role == "admin" else "Enable internal collections"
    expect(card.get_by_role("button", name=own, exact=True)).to_be_enabled()
    expect(card.get_by_role("button", name=other, exact=True)).to_have_count(0)
    assert unnamed_controls(page, '[data-testid="nv-welcome"]') == []
    assert card.evaluate("el => [...el.querySelectorAll('button')].every(b => b.getBoundingClientRect().height >= 44)")
    assert state["acks"] == 1
    original_url = page.url
    if not mobile:
        expect(page.locator('[data-testid^="nv-tg-tab:session:"]')).to_have_count(1)
    card.get_by_role("button", name="Dismiss welcome").focus()
    page.keyboard.press("Enter")
    expect(page.get_by_test_id("nv-composer-input")).to_be_focused()
    page.reload()
    expect(page.get_by_test_id("nv-welcome-reopen")).to_be_visible(timeout=30_000)
    expect(card).to_have_count(0)
    assert state["acks"] == 1
    if mobile:
        reopen_button = page.get_by_test_id("nv-welcome-reopen")
        reopen_button.focus()
        page.keyboard.press("Enter")
        page.wait_for_function("window.__w3ReopenHeld === true")
        expect(reopen_button).to_have_attribute("aria-busy", "true")
        expect(reopen_button).to_have_attribute("aria-disabled", "true")
        expect(reopen_button).to_be_focused()
        assert reopen_button.evaluate("el => el.disabled") is False
        page.keyboard.press("Enter")
        assert len(held_reopens) == state["reopens"] == 1
        held_reopens[0].fulfill(status=200, json={"variant": role, "show": True})
    else:
        run_verb(page, "Show Me What You Can Do", row_key="v:welcome.reopen")
    expect(card).to_be_visible()
    expect(card.get_by_role("heading")).to_be_focused()
    expect(card.get_by_role("button", name=own, exact=True)).to_be_enabled()
    assert page.url == original_url
    assert state["reopens"] == 1 and state["acks"] == 2
    composer = page.get_by_test_id("nv-composer-input")
    composer.fill("Keep my unfinished draft")
    card.get_by_role("button", name=own, exact=True).click()
    expect(card).to_have_count(0)
    expect(composer).to_have_value("Keep my unfinished draft")
    assert len(sends) == 1
    assert own.lower().split()[0] in sends[0]["instruction"].lower()
    if role == "user":
        assert "runbooks/moderate-a-request" in sends[0]["instruction"]
    assert page.url == original_url
    if not mobile:
        expect(page.locator('[data-testid^="nv-tg-tab:session:"]')).to_have_count(1)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")

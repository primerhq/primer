"""The agent form says what is wrong before it posts, and the status strip does not print a developer line (ADM-14 and ADM-15 of the 2026-10-08 admin review).

* ADM-14: ``Bad Name!`` as an agent id, and an empty form, used to go to the server and create agents (``Bad%20Name!`` in every URL; a nameless one). The form now refuses both with the
  reason under the field, and no ``POST /v1/agents`` leaves the page. Nothing is created by this journey: it stops at the refusals, which is where the finding was.
* ADM-15: opening an existing agent (the seeded ``operator``, which every bootstrapped instance has) showed ``GET /v1/agents/operator/status · last checked just now · polled every
  30s`` under "All references resolve" or the list of issues, whichever the instance has; either way the same line is there.
"""

from __future__ import annotations

import httpx
from playwright.sync_api import Page, expect

from tests._support.smk import smk
from tests.ui_e2e._shell_helpers import open_legacy_route

pytestmark = smk("SMK-UI-03", status="partial")


def _agent_ids(base_url: str) -> set[str]:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        return {a["id"] for a in c.get("/v1/agents", params={"limit": 200}).json().get("items", [])}


def _remove_agents_this_test_made(base_url: str, before: set[str]) -> None:
    """If the form ever posts again, the agents it makes must not stay on a shared instance: delete what appeared since ``before``."""
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        for agent_id in _agent_ids(base_url) - before:
            c.delete("/v1/agents/" + agent_id.replace(" ", "%20"))


def test_the_create_form_refuses_a_bad_name_and_an_empty_description_before_it_posts(page: Page, base_url: str, console_url: str) -> None:
    before = _agent_ids(base_url)
    try:
        _the_create_form_refuses(page, console_url)
    finally:
        _remove_agents_this_test_made(base_url, before)


def _the_create_form_refuses(page: Page, console_url: str) -> None:
    posts: list[str] = []
    page.on("request", lambda r: posts.append(r.url) if r.method == "POST" and r.url.rstrip("/").endswith("/v1/agents") else None)
    open_legacy_route(page, console_url, "agents")
    page.get_by_role("button", name="New agent").first.click()
    modal = page.locator(".modal").first
    modal.wait_for(state="visible", timeout=5_000)

    # (a) a name that would need escaping in every URL, with a description.
    modal.locator("#na-id").fill("Bad Name!")
    modal.locator("#na-description").fill("A typo that used to be permanent")
    modal.get_by_role("button", name="Create").click()
    expect(modal.get_by_test_id("na-id-error")).to_contain_text("lowercase letters, digits, hyphens and underscores", timeout=5_000)
    expect(modal.get_by_test_id("na-id-error")).to_contain_text("cannot change")
    assert posts == [], "a refused name must not be sent"

    # Typing in the field takes its message away.
    modal.locator("#na-id").fill("refund-triage")
    expect(modal.get_by_test_id("na-id-error")).to_have_count(0)

    # (b) an empty form: the name is optional, the description is not.
    modal.locator("#na-id").fill("")
    modal.locator("#na-description").fill("")
    modal.get_by_role("button", name="Create").click()
    expect(modal.get_by_test_id("na-description-error")).to_contain_text("Describe", timeout=5_000)
    expect(modal.get_by_test_id("na-id-error")).to_have_count(0)
    assert posts == [], "an empty form must not be sent"


def test_an_existing_agents_status_strip_does_not_print_the_endpoint_or_the_poll_interval(page: Page, console_url: str) -> None:
    open_legacy_route(page, console_url, "agents/operator")
    expect(page.locator("#na-id")).to_have_value("operator", timeout=15_000)

    note = page.get_by_test_id("agent-status-note")
    expect(note).to_contain_text("Checked automatically while this window is open", timeout=15_000)
    shown = page.locator(".modal, [data-testid='nv-overlay-body']").first.inner_text()
    for developer_text in ("GET /v1/agents", "polled every", "last checked just now"):
        assert developer_text not in shown, f"{developer_text!r} is still printed"
    assert "polled every 30s" in (note.get_attribute("title") or ""), "the technical detail is a tooltip"

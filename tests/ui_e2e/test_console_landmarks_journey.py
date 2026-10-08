"""Journey: the console has landmarks, a page heading, live regions and a labelled composer (console review 2026-10-08, C-029).

Before: on the studio, a session document and Platform > Agents there were no ``main``, ``nav``, ``header`` or ``aside`` landmarks, no
headings, and no live regions at all, so toasts, the status strip, approvals and errors were silent to a screen reader; the composer
``<textarea>`` had only a placeholder for a name (which changes with state) and the Platform filter input only a placeholder.
"""

from __future__ import annotations

import re

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.ui_e2e._shell_helpers import open_doc, open_mobile_shell, open_view

_AUDIT = """() => {
  const label = (e) => e.getAttribute('aria-label');
  return {
    main: document.querySelectorAll('[role=main], main').length,
    banner: document.querySelectorAll('[role=banner], header').length,
    nav: [...document.querySelectorAll('[role=navigation], nav')].map(label),
    aside: [...document.querySelectorAll('[role=complementary], aside')].map(label),
    h1: [...document.querySelectorAll('h1')].map((e) => e.textContent.trim()),
  };
}"""


def _first_workspace_and_session(base_url: str) -> tuple[str, str]:
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        wid = c.get("/v1/workspaces").json()["items"][0]["id"]
        agent_id = c.get("/v1/agents", params={"limit": 1}).json()["items"][0]["id"]
        r = c.post(f"/v1/workspaces/{wid}/sessions", json={"binding": {"kind": "agent", "agent_id": agent_id}, "auto_start": False})
        assert r.status_code == 201, r.text
        return wid, r.json()["id"]


@pytest.mark.ui_e2e
def test_the_studio_has_landmarks_a_heading_and_a_labelled_composer(page: Page, base_url: str, console_url: str) -> None:
    wid, sid = _first_workspace_and_session(base_url)
    open_doc(page, console_url, wid, "session", sid)
    expect(page.get_by_test_id(f"nv-session-doc:{sid}")).to_be_visible(timeout=20_000)

    audit = page.evaluate(_AUDIT)
    assert audit["main"] == 1, audit
    assert audit["banner"] == 1, audit
    assert "Views" in audit["nav"] and "Sessions and workspaces" in audit["nav"], audit
    assert "Files" in audit["aside"], audit
    assert "Studio" in audit["h1"], audit

    expect(page.get_by_test_id("nv-composer-input")).to_have_accessible_name(re.compile(r"^Message"))


@pytest.mark.ui_e2e
def test_toasts_are_announced_and_an_error_toast_interrupts(page: Page, base_url: str, console_url: str) -> None:
    wid, _sid = _first_workspace_and_session(base_url)
    open_view(page, console_url, wid, "studio")
    stack = page.get_by_test_id("nv-toasts")
    expect(stack).to_have_attribute("role", "status")
    expect(stack).to_have_attribute("aria-live", "polite")

    page.evaluate("() => window.primerApi.toastPush({ kind: 'info', text: 'Saved the thing' })")
    expect(stack.locator(".toast", has_text="Saved the thing")).not_to_have_attribute("role", "alert")
    page.evaluate("() => window.primerApi.toastPush({ kind: 'error', text: 'Could not save' })")
    expect(stack.locator(".toast", has_text="Could not save")).to_have_attribute("role", "alert", timeout=10_000)


@pytest.mark.ui_e2e
def test_the_platform_filter_has_a_name(page: Page, base_url: str, console_url: str) -> None:
    wid, _sid = _first_workspace_and_session(base_url)
    open_view(page, console_url, wid, "platform")
    expect(page.get_by_test_id("nv-plat-filter")).to_have_accessible_name(re.compile(r"^Filter"), timeout=15_000)


@pytest.mark.ui_e2e
def test_the_phone_shell_has_a_main_landmark(page: Page, console_url: str) -> None:
    page.set_viewport_size({"width": 390, "height": 844})
    open_mobile_shell(page, console_url)
    expect(page.locator("[role=main]")).to_have_count(1)
    expect(page.get_by_test_id("nv-mobile-shell")).to_have_attribute("role", "main")

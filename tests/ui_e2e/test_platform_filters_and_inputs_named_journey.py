"""Journey: the Platform pages' list filters and the plain create-form inputs have names (console review C-003, found by the runtime sweep of the main surfaces).

A filter box with only a placeholder, and a filter ``<select>`` whose only text is its first option, are unnamed edit fields and combo boxes to a screen reader. This opens, in one browser and one
after the other, each Platform list page that has a filter, the create forms whose inputs had a placeholder and nothing else (workspaces, collections, the agent form's tool picker), the speech
settings, the New workspace overlay and the Activity overlay, and fails on any visible input, select, textarea or button without a name (``tests/ui_e2e/_a11y.py``). The create forms are swept
below their header, so the dialog's close button (its own PR) is not this journey's.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from tests._support.smk import smk
from tests.ui_e2e._a11y import unnamed_controls
from tests.ui_e2e._shell_helpers import open_legacy_route, open_overlay

pytestmark = smk("SMK-UI-06", status="partial")

_OVERLAY = '[data-testid^="nv-overlay:"]'
_LIST_PAGES = [
    "agents", "graphs", "toolsets", "services", "workspaces", "channels", "channels/providers", "channels/rules", "knowledge/collections",
    "providers/llm", "providers/stt", "providers/tts", "workspaces/providers",
]


def _first_workspace(base_url: str) -> str:
    import httpx

    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        items = c.get("/v1/workspaces").json()["items"]
    assert items, "the install has a default workspace"
    return items[0]["id"]


@pytest.mark.ui_e2e
@pytest.mark.timeout(300)
def test_the_platform_list_filters_and_plain_create_inputs_have_names(base_url: str, console_url: str, page: Page) -> None:
    unnamed: dict[str, list[str]] = {}

    def check(surface: str, root: str) -> None:
        page.wait_for_timeout(250)
        found = unnamed_controls(page, root)
        if found:
            unnamed[surface] = found

    for route in _LIST_PAGES:
        open_legacy_route(page, console_url, route)
        check(f"list page {route}", _OVERLAY)

    # the create forms whose inputs were placeholder-only (swept below the dialog's header)
    for route, button in (("workspaces", "New workspace"), ("knowledge/collections", "New collection"), ("agents", "New agent")):
        open_legacy_route(page, console_url, route)
        new = page.locator(_OVERLAY).get_by_role("button", name=re.compile(rf"^(\+ )?{button}\b")).first
        expect(new).to_be_visible(timeout=15_000)
        new.click()
        expect(page.locator(".modal").first).to_be_visible(timeout=10_000)
        if route == "agents":
            # the tool picker's group headers (a checkbox each) draw once the catalogue has loaded
            expect(page.locator('.modal [data-testid^="tool-picker-group-"]').first).to_be_visible(timeout=15_000)
        check(f"create form {route}", ".modal .modal-b")
        page.keyboard.press("Escape")

    wid = _first_workspace(base_url)
    for name in ("new-workspace", "activity"):
        open_overlay(page, console_url, wid, name)
        check(f"overlay {name}", f'[data-testid="nv-overlay:{name}"]')

    report = "\n".join(f"  {surface}\n" + "\n".join(f"      {html}" for html in found) for surface, found in unnamed.items())
    assert not unnamed, f"controls with no name, by surface:\n{report}"

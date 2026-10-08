"""Journey: every control of the New toolset form has a name (console review C-003, the toolsets surface).

The form drew its labels as siblings of the controls, and the environment / headers editors held pairs of inputs named only by their placeholders. In the real modal each visible
input, select and textarea has a label (or an ``aria-label``) for the MCP stdio form, the MCP http form and the Python form, and a click on a label focuses its control.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from tests._support.smk import smk
from tests.ui_e2e._shell_helpers import open_legacy_route

pytestmark = smk("SMK-UI-06", status="partial")

_UNNAMED = """els => els.filter(e => e.type !== 'hidden' && e.offsetParent !== null
    && e.labels.length === 0 && !e.getAttribute('aria-label') && !e.getAttribute('aria-labelledby'))
    .map(e => e.outerHTML.slice(0, 90))"""


def _open_form(page: Page, console_url: str):
    open_legacy_route(page, console_url, "toolsets")
    page.get_by_role("button", name="New toolset").first.click()
    modal = page.locator(".modal").first
    expect(modal).to_be_visible(timeout=10_000)
    return modal


def _add_a_pair_to_every_editor(modal) -> None:
    for button in modal.get_by_role("button", name="Add", exact=False).all():
        if button.is_visible():
            button.click()


def test_every_control_of_the_mcp_stdio_form_is_named(page: Page, console_url: str) -> None:
    modal = _open_form(page, console_url)
    expect(modal.get_by_placeholder("npx @modelcontextprotocol/server-github")).to_be_visible(timeout=5_000)
    _add_a_pair_to_every_editor(modal)
    assert modal.locator("input, select, textarea").evaluate_all(_UNNAMED) == []
    modal.locator("label.field-label", has_text="ID").first.click()
    expect(modal.get_by_placeholder("auto-generated")).to_be_focused()


def test_every_control_of_the_mcp_http_form_is_named(page: Page, console_url: str) -> None:
    modal = _open_form(page, console_url)
    modal.locator(".chip", has_text="http").first.click()
    expect(modal.get_by_placeholder("https://mcp.example.com/sse")).to_be_visible(timeout=5_000)
    _add_a_pair_to_every_editor(modal)
    assert modal.locator("input, select, textarea").evaluate_all(_UNNAMED) == []


@pytest.mark.parametrize("provider", ["python"])
def test_every_control_of_the_python_form_is_named(page: Page, console_url: str, provider: str) -> None:
    modal = _open_form(page, console_url)
    modal.locator("select").first.select_option(provider)
    expect(modal.get_by_test_id("toolset-python-admin-only")).to_be_visible(timeout=5_000)
    assert modal.locator("input, select, textarea").evaluate_all(_UNNAMED) == []

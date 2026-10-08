"""Journey: a Platform form's labels name their controls (console review C-003, beyond the auth forms).

``WS_FieldRow`` drew the label as a sibling of the input with no ``htmlFor``, so in the workspace-template form a screen reader met unnamed edit fields and clicking a
label focused nothing. In the real modal the fields are now found by their label (``get_by_label`` resolves through ``input.labels``, which was empty) and a click on the
visible label focuses its field.
"""

from __future__ import annotations

from playwright.sync_api import expect

from tests._support.smk import smk
from tests.ui_e2e._shell_helpers import open_legacy_route

pytestmark = smk("SMK-UI-06", status="partial")


def test_the_template_form_fields_are_found_by_their_label_and_a_label_click_focuses_its_field(page, console_url: str) -> None:
    page.wait_for_function("() => typeof window.WorkspaceTemplatesPage === 'function'", timeout=20_000)
    open_legacy_route(page, console_url, "workspaces/templates")
    new_btn = page.get_by_role("button", name="New workspace template").or_(page.get_by_role("button", name="New template")).first
    expect(new_btn).to_be_visible(timeout=20_000)
    new_btn.click()
    modal = page.locator(".modal").first
    expect(modal).to_be_visible(timeout=5_000)

    description = modal.get_by_label("description", exact=True)
    expect(description).to_have_count(1)
    description.fill("a template named by its label")
    expect(modal.get_by_test_id("ws-template-description")).to_have_value("a template named by its label")

    modal.locator("label.field-label", has_text="description").click()
    expect(modal.get_by_test_id("ws-template-description")).to_be_focused()

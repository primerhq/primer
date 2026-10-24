"""Journey: a Platform form's labels name their controls (console review C-003, beyond the auth forms).

``WS_FieldRow`` drew the label as a sibling of the input with no ``htmlFor``, so in the workspace-template form a screen reader met unnamed edit fields and clicking a
label focused nothing. The old template form is gone (platform revamp Plan 08, Task 15); the form that replaced it is the template overlay on Platform ›
Workspace templates, whose rows are the kit's ``PK_Field`` (``FormField`` underneath, as ``WS_FieldRow`` is now). In the real overlay a field is found by its
label (``get_by_label`` resolves through ``input.labels``, which was empty) and a click on the visible label focuses its field.

The Description label reads "Description *" (the kit marks a required field with an ``aria-hidden`` asterisk, which Playwright's label text includes), so the
label is matched from its start. Lead-scheduled: written with the change, run by the lead.
"""

from __future__ import annotations

import re

from playwright.sync_api import expect

from tests._support.smk import smk
from tests.ui_e2e._shell_helpers import default_wid, open_view

pytestmark = smk("SMK-UI-06", status="partial")


def test_the_template_form_fields_are_found_by_their_label_and_a_label_click_focuses_its_field(page, console_url: str) -> None:
    open_view(page, console_url, default_wid(console_url), "platform:templates")
    expect(page.get_by_test_id("wt-templates-page")).to_be_visible(timeout=20_000)
    page.get_by_test_id("wt-new").click()
    overlay = page.get_by_test_id("wt-overlay")
    expect(overlay).to_be_visible(timeout=10_000)
    # The form draws once the providers have loaded (every install has the reserved Local provider).
    expect(overlay.get_by_test_id("ws-template-description")).to_be_visible(timeout=10_000)

    description = overlay.get_by_label(re.compile(r"^Description\b"))
    expect(description).to_have_count(1)
    description.fill("a template named by its label")
    expect(overlay.get_by_test_id("ws-template-description")).to_have_value("a template named by its label")

    overlay.locator("label.field-label", has_text=re.compile(r"^Description\b")).click()
    expect(overlay.get_by_test_id("ws-template-description")).to_be_focused()

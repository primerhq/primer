"""The registration and sign-in forms can be driven through their accessible names, and a failed submit is announced (console review C-003).

In a real browser a field is reachable by its label only when the ``<label>`` points at the input, and a screen reader hears an error only
when it sits in a live region. This finds each password field with ``get_by_label`` (which fails on the old markup, where ``input.labels``
was empty), clicks a label to focus its field, names the two reveal buttons apart, and reads the errors through ``get_by_role("alert")``.

The shared ui_e2e install runs with auth off, so these screens never show on their own: the endpoint the auth gate reads is answered by
a small fake behind ``page.route`` (the technique of ``test_login_sso_error_journey``); the console's scripts come from the real server.
"""

from __future__ import annotations

import json
import re

from playwright.sync_api import expect

from tests._support.smk import smk
from tests.ui_e2e._shell_helpers import open_gate

pytestmark = smk("SMK-UI-06", status="partial")


def _answer_status(page, *, has_user: bool) -> None:
    page.route("**/v1/auth/status", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({"has_user": has_user, "authenticated": False, "setup_complete": has_user, "setup_missing": []}),
    ))
    page.route("**/v1/auth/sso/providers", lambda route: route.fulfill(status=200, content_type="application/json", body="[]"))


def test_the_register_fields_are_found_by_label_and_the_reveal_buttons_are_told_apart(page, console_url: str) -> None:
    _answer_status(page, has_user=False)
    open_gate(page, console_url)

    expect(page.get_by_role("heading", name="Create the operator account")).to_be_visible(timeout=20_000)
    password = page.get_by_label("Password", exact=True)
    confirm = page.get_by_label("Confirm password")
    expect(password).to_have_count(1)
    expect(confirm).to_have_count(1)

    page.get_by_text("Confirm password", exact=True).click()
    expect(confirm).to_be_focused()

    expect(page.get_by_role("button", name="Show password")).to_have_count(1)
    page.get_by_role("button", name="Show confirmation").click()
    expect(confirm).to_have_attribute("type", "text")
    expect(password).to_have_attribute("type", "password")
    expect(page.get_by_role("button", name="Hide confirmation")).to_have_count(1)


def test_a_failed_register_submit_is_announced_and_tied_to_the_field(page, console_url: str) -> None:
    _answer_status(page, has_user=False)
    open_gate(page, console_url)
    expect(page.get_by_role("heading", name="Create the operator account")).to_be_visible(timeout=20_000)

    page.get_by_label("Username").fill("reviewer")
    page.get_by_label("Password", exact=True).fill("long-enough-1")
    page.get_by_label("Confirm password").fill("something else")
    page.get_by_role("button", name="Create account").click()

    alert = page.get_by_role("alert")
    expect(alert).to_have_count(1)
    expect(alert).to_contain_text("passwords don't match")
    confirm = page.get_by_label("Confirm password")
    expect(confirm).to_have_attribute("aria-invalid", "true")
    expect(confirm).to_have_accessible_description(re.compile("passwords don't match"))


def test_a_rejected_sign_in_is_announced(page, console_url: str) -> None:
    _answer_status(page, has_user=True)
    page.route("**/v1/auth/login", lambda route: route.fulfill(
        status=401, content_type="application/json",
        body=json.dumps({"type": "/errors/authentication-failed", "title": "Authentication Failed", "status": 401, "detail": "invalid_credentials"}),
    ))
    open_gate(page, console_url)
    expect(page.get_by_role("heading", name="Sign in to your console")).to_be_visible(timeout=20_000)

    page.get_by_label("Username").fill("someone")
    page.get_by_label("Password", exact=True).fill("wrong-password")
    page.get_by_role("button", name=re.compile(r"^Sign in$")).click()

    expect(page.get_by_role("alert")).to_contain_text("Invalid username or password", timeout=10_000)
